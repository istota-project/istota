"""The two devbox service definitions, held against each other.

The devbox ships in two shapes from one Dockerfile. `docker/docker-compose.yml`
defines a single-user service for the standalone deploy;
`deploy/ansible/templates/docker-compose.devbox.yml.j2` renders one service per
user for production. A comment in the first says "keep this entry in sync with"
the second, and until ISSUE-282 nothing enforced it — which is how the compose
shape ended up with no credential socket, so every `gh`, `glab` and `git push`
inside that container failed at a path that does not exist, while `skill.md`
promised the capability unconditionally.

The repo already enforces this kind of pairing elsewhere: `tests/
test_forge_cli.py` asserts the vendored wrapper is byte-identical to its source,
and `scripts/sync-devbox-lib.sh` maintains it. This is the same obligation for a
pair that cannot be byte-identical, because one is a template and the other is
not.

**The allowlist is the point.** `INTENTIONAL_DIFFERENCES` names every key the
two shapes are permitted to disagree on and why. A new divergence fails this
file until someone adds an entry, which turns the next one into a decision
rather than an accident. Adding an entry is cheap and is meant to be; adding one
without a reason is the failure mode, so each carries prose.

What this does not check: that either file is *correct*. It checks that they
differ only where someone has said they may.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker" / "docker-compose.yml"
ANSIBLE = REPO / "deploy" / "ansible"
TEMPLATE = ANSIBLE / "templates" / "docker-compose.devbox.yml.j2"
DEFAULTS_FILE = ANSIBLE / "defaults" / "main.yml"

DEVBOX_USER = "alice"


# Every key the two shapes may differ on, and why. Read this before adding to
# it: an entry is a statement that the difference is deliberate and understood.
INTENTIONAL_DIFFERENCES = {
    "profiles": (
        "compose-only. The devbox sits behind a non-default profile there so a "
        "plain `docker compose up` does not start it; Ansible renders the file "
        "only for users who have one."
    ),
    "build": (
        "Both now root the context at docker/devbox and name Dockerfile inside "
        "it, which is what that Dockerfile's COPYs require; only the spelling "
        "differs, relative in one and rendered from istota_repo_dir in the "
        "other. Held by TestBothShapesBuildTheSameRecipe rather than left to "
        "this entry."
    ),
    "container_name": (
        "Both name the container after its user; compose interpolates "
        "${USER_NAME} at run time, Ansible renders the per-user loop variable."
    ),
    "hostname": (
        "Follows container_name on both sides, and diverges for the same "
        "reason: one is interpolated by compose, the other rendered by Ansible."
    ),
    "labels": (
        "Both set com.istota.user_id to the container's owner; only the source "
        "of that string differs, exactly as for container_name."
    ),
    "volumes": (
        "Allowlisted for the *names* only — the home volume is per-user under "
        "Ansible and singular under compose. Which mount points may differ is "
        "held separately by TestVolumeMountPointsAreHeldInLine, because "
        "excusing this key wholesale would permit any new bind mount, and the "
        "obvious one to add by accident is the docker socket."
    ),
    "group_add": (
        "Ansible-only, and only meaningful with the credential socket above: it "
        "grants the container's uid 1000 the host istota group so it can open a "
        "0660 socket. Nothing for the compose shape to join."
    ),
}

# Keys that must be present and identical on both sides. Two groups, and the
# second is the less obvious one.
#
# Behaviour: a divergence here changes what the container can do.
#
# Resource limits and the image tag: these are literals under compose and
# variables under Ansible, so they *could* have gone in the allowlist above —
# but at role defaults they resolve to the same values, and that agreement is
# exactly the "keep this entry in sync" obligation the compose comment states.
# Holding them here means bumping a default on one side and not the other is
# reported rather than absorbed. An operator overriding a variable on their own
# host is unaffected; this compares defaults.
MUST_MATCH = (
    "cap_add", "restart", "command", "networks",
    "image", "mem_limit", "cpus", "pids_limit", "tmpfs",
)


# The comparison itself, at module level and called by both the real tests and
# the negative control below. It was originally inline in the test body with the
# control carrying its own copy, which meant the control asserted against a
# duplicate: mutating the real comparison left every test green, including the
# one whose job is noticing that.
def unexplained_divergences(compose: dict, ansible: dict) -> set[str]:
    divergent = {
        key for key in set(compose) | set(ansible)
        if compose.get(key) != ansible.get(key)
    }
    return divergent - set(INTENTIONAL_DIFFERENCES)


def mismatched_must_match(compose: dict, ansible: dict) -> set[str]:
    return {
        key for key in MUST_MATCH
        if compose.get(key) != ansible.get(key)
    }


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS_FILE.read_text())


def _resolve(variables: dict, env: Environment) -> dict:
    """Expand `{{ other_var }}` inside the defaults, the way Ansible would.

    Iterates to a fixed point: `istota_repo_dir` is `{{ istota_home }}/istota`
    and `istota_home` is itself `/srv/app/{{ istota_namespace }}`, so one pass
    leaves a template in the output. Bounded, so a genuine cycle fails loudly
    rather than hanging the suite.
    """

    def expand(value):
        if isinstance(value, str) and "{{" in value:
            return env.from_string(value).render(**variables)
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    for _ in range(10):
        expanded = {k: expand(v) for k, v in variables.items()}
        if expanded == variables:
            return variables
        variables = expanded
    raise AssertionError("defaults/main.yml did not reach a fixed point in 10 passes")


@pytest.fixture(scope="module")
def ansible_service() -> dict:
    env = Environment(
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    variables = _resolve(
        {
            **_defaults(),
            # The one host fact the defaults read (istota_browser_cpu_limit).
            # Supplied rather than stubbed away, so a *second* fact appearing in
            # the defaults fails here loudly under StrictUndefined instead of
            # rendering as an empty string.
            "ansible_facts": {"processor_vcpus": 4},
            "istota_devbox_users": [DEVBOX_USER],
            "istota_devbox_proxy_group_gid": 1001,
        },
        env,
    )
    rendered = yaml.safe_load(env.from_string(TEMPLATE.read_text()).render(**variables))
    services = rendered["services"]
    assert len(services) == 1, f"expected one rendered service, got {list(services)}"
    return next(iter(services.values()))


@pytest.fixture(scope="module")
def compose_service() -> dict:
    # `${USER_NAME}` and friends are compose-time interpolation, not YAML, so
    # the document parses as-is.
    return yaml.safe_load(COMPOSE.read_text())["services"]["devbox"]


class TestTheTwoShapesDifferOnlyWhereAllowed:
    def test_neither_side_is_empty(self, compose_service, ansible_service):
        """A parity test comparing two empty dicts passes. Both fixtures resolve
        a real service, or the rest of this file means nothing."""
        assert len(compose_service) > 5
        assert len(ansible_service) > 5

    def test_every_divergent_key_is_accounted_for(self, compose_service, ansible_service):
        unexplained = sorted(unexplained_divergences(compose_service, ansible_service))
        assert not unexplained, (
            f"the two devbox service definitions differ on {unexplained} and "
            f"nothing says why. Either bring them into line, or add an entry to "
            f"INTENTIONAL_DIFFERENCES saying what the difference is for."
        )

    def test_the_allowlist_has_no_stale_entries(self, compose_service, ansible_service):
        """An entry for a key that no longer differs is a licence nobody is
        using, and it would silently permit a future divergence on that key.

        Present on *either* side, not just on compose: a key both files dropped
        would otherwise compare `None == None`, never read as stale, and leave
        its licence in place for a later one-sided reintroduction.
        """
        stale = sorted(
            key for key in INTENTIONAL_DIFFERENCES
            if (key in compose_service or key in ansible_service)
            and compose_service.get(key) == ansible_service.get(key)
        )
        assert not stale, (
            f"INTENTIONAL_DIFFERENCES still excuses {stale}, which now match. "
            f"Drop those entries so the keys are held in line again."
        )

    def test_the_allowlist_names_no_key_that_has_left_both_files(
        self, compose_service, ansible_service
    ):
        """The other half of the same hole: an entry naming a key neither file
        has any more is dead text, and it silently pre-approves whatever
        reintroduces it."""
        gone = sorted(
            key for key in INTENTIONAL_DIFFERENCES
            if key not in compose_service and key not in ansible_service
        )
        assert not gone, (
            f"INTENTIONAL_DIFFERENCES names {gone}, which neither file sets. "
            f"Drop those entries rather than leaving a standing permission."
        )

    def test_every_allowlist_entry_carries_a_reason(self):
        for key, reason in INTENTIONAL_DIFFERENCES.items():
            assert len(reason.split()) >= 8, (
                f"the entry for {key!r} does not say why the difference exists"
            )

    @pytest.mark.parametrize("key", MUST_MATCH)
    def test_the_load_bearing_keys_are_identical(self, key, compose_service, ansible_service):
        assert key in compose_service, f"compose devbox lost {key}"
        assert key in ansible_service, f"the Ansible devbox template lost {key}"
        assert compose_service[key] == ansible_service[key], (
            f"{key} differs between the two shapes: "
            f"{compose_service[key]!r} vs {ansible_service[key]!r}"
        )


class TestTheCredentialSocketIsAnAnsibleOnlyChoice:
    """ISSUE-282 was resolved by deciding the compose shape stays
    credential-free rather than by giving it a proxy. These pin that decision so
    it is visible, and so half-implementing it later fails here."""

    CRED_MOUNT = "/run/istota-cred"

    def test_ansible_mounts_the_credential_socket_directory(self, ansible_service):
        assert any(
            str(v).endswith(f":{self.CRED_MOUNT}") for v in ansible_service["volumes"]
        ), "the Ansible devbox no longer mounts the credential proxy socket dir"

    def test_ansible_joins_the_group_that_can_open_it(self, ansible_service):
        """The socket is mode 0660 owned by the istota group. Without the
        supplementary group the mount is present and unusable, which is a
        harder failure to read than no mount at all."""
        assert ansible_service.get("group_add"), (
            "the credential socket is mounted with no group_add — uid 1000 "
            "cannot open a 0660 socket it does not share a group with"
        )

    def test_compose_has_neither_half(self, compose_service):
        assert not any(
            str(v).endswith(f":{self.CRED_MOUNT}")
            for v in compose_service.get("volumes", [])
        ), (
            "the compose devbox now mounts a credential socket. If that is "
            "intended, ISSUE-282's decision has been reversed: something must "
            "also run `python -m istota.devbox_proxy` for that user, skill.md "
            "must stop saying the shape is credential-free, and the forge "
            "wrapper's exit-4 message must stop naming it."
        )
        assert not compose_service.get("group_add"), (
            "the compose devbox joins a host group but mounts no socket — "
            "half of the credential setup, which is worse than neither half"
        )

    def test_the_documented_socket_path_is_the_one_the_image_expects(self):
        """The mount point is not arbitrary: the image bakes
        `ISTOTA_CRED_SOCK=/run/istota-cred/sock`, and the wrapper and the git
        credential helper both read it from there."""
        dockerfile = (REPO / "docker" / "devbox" / "Dockerfile").read_text()
        assert f"ISTOTA_CRED_SOCK={self.CRED_MOUNT}/sock" in dockerfile


class TestTheSkillBodyMatchesTheDecision:
    """`skill.md` is the prompt. It promised the forge capability without
    qualification, which is the half of ISSUE-282 the model actually saw."""

    @pytest.fixture(scope="class")
    def body(self) -> str:
        return (REPO / "src" / "istota" / "skills" / "devbox" / "skill.md").read_text()

    def test_it_names_the_shape_that_has_no_credentials(self, body):
        assert "docker compose" in body.lower()
        assert "credential" in body.lower()

    def test_it_tells_the_model_what_exit_4_means(self, body):
        assert "exit 4" in body.lower()


class TestTheWrapperRefusalNamesTheShape:
    """The other half: a refusal that prints a socket path tells the reader
    nothing about why it is missing."""

    def test_the_no_proxy_message_names_both_deployments(self):
        from istota.forge_cli import NoProxyError, fetch_forge_credentials

        with pytest.raises(NoProxyError) as excinfo:
            fetch_forge_credentials("github", {}, {})
        message = str(excinfo.value).lower()
        assert "ansible" in message
        assert "docker-compose" in message or "docker compose" in message

    def test_it_does_not_lead_with_a_bare_socket_path(self):
        from istota.forge_cli import NoProxyError, fetch_forge_credentials

        with pytest.raises(NoProxyError) as excinfo:
            fetch_forge_credentials("gitlab", {}, {})
        assert not str(excinfo.value).startswith("/")


class TestTheComparisonCanFail:
    """The negative control. A parity test is a comparison, and a comparison
    with a too-permissive allowlist passes on everything — which is the exact
    failure this file exists to prevent in the artifacts it watches. So feed it
    divergences that would really matter and require it to object.
    """

    BASE = {"cap_add": ["NET_RAW"], "restart": "unless-stopped"}

    @pytest.mark.parametrize(
        "compose_extra, ansible_extra, caught_by",
        [
            ({}, {"privileged": True}, "allowlist"),
            ({"devices": ["/dev/kvm"]}, {}, "allowlist"),
            ({}, {"network_mode": "host"}, "allowlist"),
            ({}, {"cap_add": ["NET_RAW", "NET_ADMIN"]}, "both"),
            ({"mem_limit": "2g"}, {"mem_limit": "16g"}, "both"),
            ({"image": "istota-devbox:latest"},
             {"image": "istota-devbox:some-fork"}, "both"),
        ],
        ids=["privileged", "device_passthrough", "host_network", "widened_cap_add",
             "mem_limit_bumped_one_side", "image_repointed"],
    )
    def test_a_real_divergence_is_reported(self, compose_extra, ansible_extra, caught_by):
        compose = {**self.BASE, **compose_extra}
        ansible = {**self.BASE, **ansible_extra}
        by_allowlist = bool(unexplained_divergences(compose, ansible))
        by_must_match = bool(mismatched_must_match(compose, ansible))

        assert by_allowlist or by_must_match, "the divergence went unreported"
        # Assert *which* arm caught it, so a case that silently stops
        # exercising the arm it was written for fails rather than quietly
        # passing on the other one.
        if caught_by == "allowlist":
            assert by_allowlist
        else:
            assert by_allowlist and by_must_match

    def test_must_match_is_not_a_second_detector(self):
        """Worth stating, because the naming suggests otherwise: MUST_MATCH and
        INTENTIONAL_DIFFERENCES are disjoint, so any divergence on a MUST_MATCH
        key is *already* unexplained and caught by the allowlist arm. Keeping
        both is not redundancy for detection — it is a clearer failure message
        per key, plus the presence check the allowlist arm cannot make, since a
        key absent from both files diverges from nothing.
        """
        assert not (set(MUST_MATCH) & set(INTENTIONAL_DIFFERENCES))
        both_missing = unexplained_divergences({}, {})
        assert not both_missing, "nothing to compare should not read as divergence"

    def test_a_load_bearing_key_vanishing_from_both_sides_is_still_caught(
        self, compose_service, ansible_service
    ):
        """The one thing only MUST_MATCH sees. If `cap_add` were deleted from
        both files they would agree perfectly and the allowlist arm would be
        silent, while the container quietly lost NET_RAW."""
        stripped_compose = {k: v for k, v in compose_service.items() if k != "cap_add"}
        stripped_ansible = {k: v for k, v in ansible_service.items() if k != "cap_add"}
        assert not unexplained_divergences(stripped_compose, stripped_ansible)
        assert "cap_add" not in stripped_compose  # what the per-key test asserts

    def test_an_allowlisted_divergence_is_permitted(self):
        """The inverse. If this failed, the allowlist would be doing nothing and
        every legitimate difference would have to be argued again."""
        compose = {**self.BASE, "profiles": ["devbox"]}
        assert not unexplained_divergences(compose, self.BASE)

    def test_an_empty_service_would_not_pass_as_agreement(self, compose_service):
        """Two empty dicts agree perfectly. `test_neither_side_is_empty` is what
        stops that reading as parity, so prove it has something to hold."""
        assert unexplained_divergences({}, compose_service)

    def test_the_comparison_looks_at_both_sides(self):
        """The specific mutation that survived the first cut of this class: a
        comparison built on set *intersection* rather than union cannot see a
        key present on one side only, which is the whole ISSUE-282 shape. The
        control could not catch it because it carried its own copy."""
        assert unexplained_divergences({"privileged": True}, {}) == {"privileged"}
        assert unexplained_divergences({}, {"privileged": True}) == {"privileged"}


class TestVolumeMountPointsAreHeldInLine:
    """`volumes` is allowlisted for its names, so the destinations need holding
    separately. Excusing the whole key would let a new bind mount through
    unexamined, and on a container the model drives the one that matters is
    `/var/run/docker.sock` — root-equivalent, and one line to add.

    Compare the mount points rather than the whole entry: the source side is
    where the legitimate naming difference lives (`devbox_home` vs
    `devbox_home_<user>`), and the destination is what decides what the
    container can touch.
    """

    #: Mount points each shape may have that the other does not, and why.
    PERMITTED_EXTRA = {
        "ansible": {
            "/run/istota-cred": (
                "the per-user credential proxy socket directory. Ansible runs a "
                "host-side proxy; the compose shape deliberately does not "
                "(ISSUE-282)."
            ),
        },
        "compose": {},
    }

    @staticmethod
    def _mount_points(service: dict) -> set[str]:
        points = set()
        for entry in service.get("volumes", []):
            if isinstance(entry, dict):          # long form
                points.add(entry["target"])
            else:                                 # "source:target[:opts]"
                parts = str(entry).split(":")
                assert len(parts) >= 2, f"unparsable volume entry: {entry!r}"
                points.add(parts[1])
        return points

    def test_the_extraction_finds_something(self, compose_service, ansible_service):
        assert self._mount_points(compose_service)
        assert self._mount_points(ansible_service)

    def test_neither_shape_mounts_anything_unexplained(
        self, compose_service, ansible_service
    ):
        compose_points = self._mount_points(compose_service)
        ansible_points = self._mount_points(ansible_service)

        extra_ansible = ansible_points - compose_points - set(
            self.PERMITTED_EXTRA["ansible"]
        )
        extra_compose = compose_points - ansible_points - set(
            self.PERMITTED_EXTRA["compose"]
        )
        assert not extra_ansible, (
            f"the Ansible devbox mounts {sorted(extra_ansible)} and the compose "
            f"one does not, with nothing saying why"
        )
        assert not extra_compose, (
            f"the compose devbox mounts {sorted(extra_compose)} and the Ansible "
            f"one does not, with nothing saying why"
        )

    @pytest.mark.parametrize(
        "forbidden",
        ["/var/run/docker.sock", "/run/docker.sock", "/", "/etc", "/proc", "/sys"],
    )
    def test_neither_shape_mounts_the_host_in(
        self, forbidden, compose_service, ansible_service
    ):
        """Named rather than inferred. The docker socket is root-equivalent, and
        binding it into a container the model drives would hand it the daemon —
        the whole reason `docker_proxy.py` exists for the sandbox."""
        for name, service in (("compose", compose_service), ("ansible", ansible_service)):
            for entry in service.get("volumes", []):
                source = str(entry).split(":")[0] if not isinstance(entry, dict) else \
                    entry.get("source", "")
                assert source != forbidden, (
                    f"the {name} devbox binds {forbidden} into the container"
                )

    def test_the_permitted_extras_are_actually_present(self, ansible_service):
        """A permission for a mount that no longer exists is a standing licence
        for whatever reintroduces the path."""
        points = self._mount_points(ansible_service)
        for point in self.PERMITTED_EXTRA["ansible"]:
            assert point in points, (
                f"PERMITTED_EXTRA excuses {point} on the Ansible side and it is "
                f"not mounted there any more — drop the entry"
            )


class TestBothShapesBuildTheSameRecipe:
    """The `build` allowlist entry excuses the spelling of the context path. It
    must not excuse the two shapes rooting it in different places, which is what
    was actually wrong: compose set the context to the repo root while every
    COPY in the devbox Dockerfile is relative to `docker/devbox`, so that shape
    could not build at all. Nothing caught it — the smoke tier never builds the
    devbox image.
    """

    DEVBOX_DIR = REPO / "docker" / "devbox"

    @staticmethod
    def _context_and_file(service: dict) -> tuple[str, str]:
        build = service["build"]
        assert isinstance(build, dict), "the short-form build string is not handled"
        return str(build["context"]), str(build.get("dockerfile", "Dockerfile"))

    def test_both_resolve_to_the_same_dockerfile(self, compose_service, ansible_service):
        for name, service in (("compose", compose_service), ("ansible", ansible_service)):
            context, dockerfile = self._context_and_file(service)
            combined = f"{context.rstrip('/')}/{dockerfile}"
            # Not "docker/devbox/Dockerfile": the compose context is relative to
            # the compose file, which already lives in docker/, so it correctly
            # reads "devbox/Dockerfile" there and an absolute path under Ansible.
            assert combined.endswith("devbox/Dockerfile"), (
                f"the {name} devbox builds {combined}, not devbox/Dockerfile"
            )

    def test_the_context_is_rooted_where_the_copies_expect(
        self, compose_service, ansible_service
    ):
        """The real property. Every COPY in that Dockerfile is relative to
        docker/devbox, so a context rooted anywhere else fails on the first
        one."""
        for name, service in (("compose", compose_service), ("ansible", ansible_service)):
            context, _ = self._context_and_file(service)
            assert context.rstrip("/").endswith("devbox"), (
                f"the {name} devbox roots its build context at {context!r}; the "
                f"Dockerfile's COPY paths are relative to docker/devbox and "
                f"resolve against nothing else"
            )

    def test_every_copied_path_exists_under_that_context(self):
        """And the reason the above matters, checked directly: each COPY source
        is a real path inside docker/devbox. This is what a build would fail
        on."""
        sources = [
            line.split()[1]
            for line in (self.DEVBOX_DIR / "Dockerfile").read_text().splitlines()
            if line.startswith("COPY ")
        ]
        assert sources, "no COPY lines found in the devbox Dockerfile"
        missing = [s for s in sources if not (self.DEVBOX_DIR / s).exists()]
        assert not missing, (
            f"the devbox Dockerfile copies {missing}, which do not exist under "
            f"docker/devbox — the build would fail on the first one"
        )

    def test_none_of_them_resolve_from_the_repo_root(self):
        """The negative control for the two above: these paths must *not* exist
        at the repo root, or rooting the context there would have worked and
        the bug this class pins would not have been a bug."""
        sources = [
            line.split()[1]
            for line in (self.DEVBOX_DIR / "Dockerfile").read_text().splitlines()
            if line.startswith("COPY ")
        ]
        resolvable_from_root = [s for s in sources if (REPO / s).exists()]
        assert not resolvable_from_root, (
            f"{resolvable_from_root} resolve from the repo root as well as from "
            f"docker/devbox, so this class no longer distinguishes the two"
        )
