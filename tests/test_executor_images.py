"""Executor-side image integration (image-attachment-vision, Stage 3).

Everything here runs through the real prompt-building seam: real skill
selection, real `build_prompt`, real `prepare_image_attachments`. The two
things stubbed are the ones that would otherwise spawn a process — the OCR
child and the brain — because those are the coarse boundaries, not
collaborators.

Four properties this file exists to hold, none of which the rest of the suite
can supply:

* one `effective_prompt` reaches selection, assembly and all three retrieval
  passes, so OCR context cannot be visible to one and invisible to another;
* the OCR block is regenerated per run and never accumulates on the task;
* the paths named to the model are the ones bwrap actually binds, including for
  an attachment that arrives from outside every bind;
* the Claude Code vision claim is audited against the recorded trace rather
  than trusted.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PIL", reason="Pillow not installed")
from PIL import Image  # noqa: E402

from istota import db, executor, image_attachments  # noqa: E402
from istota.brain._types import BrainResult  # noqa: E402
from istota.config import BrainConfig, Config, SecurityConfig, UserConfig  # noqa: E402
from istota.executor import execute_task  # noqa: E402


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _png(path: Path, size=(64, 48), color=(10, 20, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def _make_config(tmp_path, **kw) -> Config:
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(exist_ok=True)
    (skills_dir / "_index.toml").write_text("")
    config = Config(
        db_path=db_path,
        skills_dir=skills_dir,
        # None => the real bundled skills, so `transcribe` and
        # `untrusted_input` are the ones that ship.
        bundled_skills_dir=None,
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig()},
        security=SecurityConfig(skill_proxy_enabled=False, sandbox_enabled=False),
        **kw,
    )
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    return config


class _CaptureBrain:
    """Records every `BrainRequest` it is handed and answers with `result`."""

    model_namespace = "anthropic"
    supports_steering = False

    def __init__(self, result=None, kind="claude_code"):
        self.kind = kind
        self.reqs = []
        self.result = result or BrainResult(
            success=True, result_text="answer", stop_reason="completed",
        )

    def execute(self, req):
        self.reqs.append(req)
        return self.result

    def resolve_model_name(self, name):
        return (name or "").strip()

    def resolve_alias(self, alias):
        return None

    def list_aliases(self):
        return []

    def validate_alias_override(self, name, target):
        return []


@pytest.fixture
def ocr(monkeypatch):
    """Stub the OCR child boundary and record what it was handed."""
    calls = []

    def fake(path, timeout=60.0):
        calls.append(path)
        return {
            "status": "ok", "text": "INVOICE 4471", "confidence": 0.91,
            "word_count": 2,
        }

    monkeypatch.setattr(image_attachments, "ocr_image_out_of_process", fake)
    return calls


@pytest.fixture
def no_transcribe(monkeypatch):
    """Never spawn whisper: audio tests say what they want back."""
    monkeypatch.setattr(
        executor, "transcribe_audio_out_of_process",
        lambda path, timeout=None: {"status": "error", "error": "stubbed"},
    )


def _task(conn, prompt="what is this?", attachments=None, source_type="talk"):
    task_id = db.create_task(
        conn, prompt=prompt, user_id="alice", source_type=source_type,
        conversation_token="room1", attachments=attachments,
    )
    return db.get_task(conn, task_id)


def _run(config, task, brain, conn, **kw):
    with patch("istota.executor.make_brain", return_value=brain):
        return execute_task(task, config, [], conn=conn, **kw)


def _prompt_of(brain) -> str:
    assert brain.reqs, "the brain was never called"
    return brain.reqs[-1].prompt


# --------------------------------------------------------------------------
# effective_prompt
# --------------------------------------------------------------------------


class TestEffectivePrompt:
    def test_ocr_context_reaches_the_assembled_prompt(self, tmp_path, ocr):
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "invoice.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)

        prompt = _prompt_of(brain)
        assert image_attachments.OCR_SECTION_HEADER in prompt
        assert "INVOICE 4471" in prompt

    def test_the_typed_request_survives_ahead_of_the_ocr_block(self, tmp_path, ocr):
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "invoice.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, prompt="MARKER-REQUEST", attachments=[str(img)])
            _run(config, task, brain, conn)

        prompt = _prompt_of(brain)
        assert prompt.index("MARKER-REQUEST") < prompt.index(
            image_attachments.OCR_SECTION_HEADER
        )

    def test_ocr_text_never_lands_on_the_task_object(self, tmp_path, ocr):
        """A retry re-runs preparation; the stored prompt must not stack it."""
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "invoice.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, prompt="what is this?", attachments=[str(img)])
            _run(config, task, brain, conn)
            assert "INVOICE 4471" not in task.prompt
            assert db.get_task(conn, task.id).prompt == "what is this?"

    def test_a_rerun_renders_the_ocr_block_exactly_once(self, tmp_path, ocr):
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "invoice.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)
            _run(config, task, brain, conn)

        for req in brain.reqs:
            assert req.prompt.count(image_attachments.OCR_SECTION_HEADER) == 1

    def test_an_audio_only_send_still_reaches_the_prompt_with_its_transcript(
        self, tmp_path, monkeypatch
    ):
        """The `effective_prompt` refactor must not cost the shipped audio path."""
        config = _make_config(tmp_path)
        audio = tmp_path / "inbox" / "memo.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"not really audio")
        monkeypatch.setattr(
            executor, "transcribe_audio_out_of_process",
            lambda path, timeout=None: {"status": "ok", "text": "buy more milk"},
        )
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(
                conn, prompt="Process the attached file(s)",
                attachments=[str(audio)],
            )
            _run(config, task, brain, conn)

        assert "buy more milk" in _prompt_of(brain)

    def test_the_transcript_still_reaches_the_post_run_conversation_index(
        self, tmp_path, monkeypatch
    ):
        """`scheduler` indexes `task.prompt` after `execute_task` returns.

        It is the one prompt consumer outside this function, so the audio
        enrichment stays on the task object — indexing "Process the attached
        file(s)" instead of the voice memo would be a silent memory regression.
        OCR text is the half that must *not* go there.
        """
        config = _make_config(tmp_path)
        audio = tmp_path / "inbox" / "memo.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"not really audio")
        monkeypatch.setattr(
            executor, "transcribe_audio_out_of_process",
            lambda path, timeout=None: {"status": "ok", "text": "buy more milk"},
        )
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(
                conn, prompt="Process the attached file(s)",
                attachments=[str(audio)],
            )
            _run(config, task, brain, conn)

        assert "buy more milk" in task.prompt

    def test_every_consumer_receives_the_same_content(
        self, tmp_path, ocr, monkeypatch
    ):
        """Selection, assembly and the three retrieval passes, one content set.

        The spec asks for one string across all five. It is one string of
        *content* — the typed request plus the OCR text — but the two BM25
        passes get it unframed. `memory/search.search` joins every whitespace
        token of its query with an implicit AND and the recall path passes no
        `allow_or_fallback`, so handing it the rendered section's ~60-word
        untrusted-content preamble would add sixty-odd terms present in no
        stored chunk and return zero rows for every task with an image,
        silently — the exact inverse of the reason OCR reaches retrieval.
        """
        config = _make_config(tmp_path)
        config.memory_search.enabled = True
        config.memory_search.auto_recall = True
        config.playbooks.enabled = True
        img = _png(tmp_path / "inbox" / "invoice.png")
        brain = _CaptureBrain()
        seen: dict[str, str] = {}

        real_select = executor.build_prompt

        def fake_select_skills(**kw):
            seen["select_skills"] = kw["prompt"]
            return []

        def fake_recall_memories(config_, conn_, task_, prompt, **kw):
            seen["recall_memories"] = prompt
            return None

        def fake_recall_playbooks(config_, conn_, task_, prompt, **kw):
            seen["recall_playbooks"] = prompt
            return None

        def fake_select_relevant_facts(facts, prompt, user_id, max_facts=0):
            seen["knowledge_graph"] = prompt
            return []

        def fake_build_prompt(task_, *a, **kw):
            seen["build_prompt"] = kw.get("effective_prompt") or ""
            return real_select(task_, *a, **kw)

        monkeypatch.setattr("istota.skills._loader.select_skills", fake_select_skills)
        monkeypatch.setattr(executor, "_recall_memories", fake_recall_memories)
        monkeypatch.setattr(executor, "_recall_playbooks", fake_recall_playbooks)
        monkeypatch.setattr(executor, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(
            "istota.memory.knowledge_graph.get_current_facts",
            lambda conn_, user: [{"entity": "x", "attribute": "y", "value": "z"}],
        )
        monkeypatch.setattr(
            "istota.memory.knowledge_graph.select_relevant_facts",
            fake_select_relevant_facts,
        )

        with db.get_db(config.db_path) as conn:
            task = _task(conn, prompt="MARKER-REQUEST", attachments=[str(img)])
            _run(config, task, brain, conn)

        assert set(seen) == {
            "select_skills", "recall_memories", "recall_playbooks",
            "knowledge_graph", "build_prompt",
        }, f"a consumer was never reached: {sorted(seen)}"
        # Every consumer sees the typed request and the OCR text.
        for name, value in seen.items():
            assert "MARKER-REQUEST" in value, name
            assert "INVOICE 4471" in value, name
        # Selection and assembly get the framed section; retrieval does not.
        for name in ("select_skills", "build_prompt"):
            assert image_attachments.OCR_SECTION_HEADER in seen[name], name
        for name in ("recall_memories", "recall_playbooks", "knowledge_graph"):
            assert image_attachments.OCR_SECTION_HEADER not in seen[name], name
            assert "untrusted" not in seen[name].lower(), name

    def test_the_retrieval_query_is_not_the_rendered_ocr_section(
        self, tmp_path, ocr, monkeypatch
    ):
        """A no-text image must add nothing at all to the recall query.

        `render_ocr_context` emits the preamble and a heading for a `no text
        detected` block too, so the framing is what would otherwise turn every
        image task's AND query into a guaranteed miss — including the ones with
        no OCR content to contribute in the first place.
        """
        config = _make_config(tmp_path)
        config.memory_search.enabled = True
        config.memory_search.auto_recall = True
        img = _png(tmp_path / "inbox" / "photo.png")
        monkeypatch.setattr(
            image_attachments, "ocr_image_out_of_process",
            lambda path, timeout=60.0: {
                "status": "ok", "text": "", "confidence": 0, "word_count": 0,
            },
        )
        seen: dict[str, str] = {}
        monkeypatch.setattr(
            executor, "_recall_memories",
            lambda c, n, t, prompt, **kw: seen.setdefault("q", prompt),
        )
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, prompt="what is this?", attachments=[str(img)])
            _run(config, task, brain, conn)

        assert seen["q"] == "what is this?"


# --------------------------------------------------------------------------
# attachment status lines
# --------------------------------------------------------------------------


class TestAttachmentStatus:
    def _prompt(self, tmp_path, ocr, attachments, brain_kind="claude_code", **cfg):
        config = _make_config(tmp_path, **cfg)
        config.brain = BrainConfig(kind=brain_kind)
        brain = _CaptureBrain(kind=brain_kind)
        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=attachments)
            _run(config, task, brain, conn)
        return _prompt_of(brain)

    def test_a_prepared_image_is_marked_as_prepared(self, tmp_path, ocr):
        img = _png(tmp_path / "inbox" / "shot.png")
        prompt = self._prompt(tmp_path, ocr, [str(img)])
        assert executor.VISION_PREPARED in prompt

    def test_the_line_never_asserts_the_model_saw_the_image(self, tmp_path, ocr):
        """Delivery is stated by whoever delivers, not by prompt assembly.

        Assembly runs several hundred lines before the brain is built, so the
        only thing available is the *configured* kind — and a native model
        with no vision support, a breaker skip-primary and an in-attempt
        reroute each make that wrong in the one direction that matters. The
        `Read` directive and the native brain's per-image blocks or omissions
        say how the pixels arrive, from the layer that knows.
        """
        img = _png(tmp_path / "inbox" / "shot.png")
        for kind in ("claude_code", "native", "tmux_claude"):
            prompt = self._prompt(tmp_path, ocr, [str(img)], brain_kind=kind)
            line = [
                ln for ln in prompt.splitlines()
                if ln.startswith("  - ") and "shot.png" in ln
            ]
            assert line, prompt
            assert "supplied" not in line[0]
            assert "Read" not in line[0]

    def test_an_omitted_image_carries_its_reason_on_its_own_line(
        self, tmp_path, ocr, monkeypatch
    ):
        monkeypatch.setattr(image_attachments, "MAX_SOURCE_BYTES", 8)
        img = _png(tmp_path / "inbox" / "huge.png", size=(200, 200))

        prompt = self._prompt(tmp_path, ocr, [str(img)])

        line = [ln for ln in prompt.splitlines() if "huge.png" in ln and "  - " in ln]
        assert line, prompt
        assert "too large" in line[0]

    def test_the_location_label_is_per_line_not_per_section(self, tmp_path, ocr):
        """One absolute path must not relabel a workspace-relative sibling."""
        img = _png(tmp_path / "inbox" / "shot.png")

        prompt = self._prompt(tmp_path, ocr, [str(img), "notes/report.pdf"])

        lines = {
            ln.strip() for ln in prompt.splitlines()
            if ln.startswith("  - ")
        }
        pdf = [ln for ln in lines if "report.pdf" in ln]
        assert pdf, lines
        assert "local path" not in pdf[0]
        assert "Attached files (local paths):" not in prompt

    def test_a_non_image_attachment_gets_no_vision_status(self, tmp_path, ocr):
        prompt = self._prompt(tmp_path, ocr, ["notes/report.pdf"])
        pdf = [ln for ln in prompt.splitlines() if "report.pdf" in ln]
        assert pdf
        assert "vision" not in pdf[0]


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


class TestSelection:
    def test_untrusted_input_is_eager_when_images_are_present(self, tmp_path, ocr):
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "shot.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)
            selected = json.loads(db.get_task(conn, task.id).selected_skills or "[]")

        assert "untrusted_input" in selected

    def test_untrusted_input_is_eager_even_when_every_image_was_omitted(
        self, tmp_path, ocr, monkeypatch
    ):
        """The omission notices are model-facing text about attacker-named files."""
        monkeypatch.setattr(image_attachments, "MAX_SOURCE_BYTES", 8)
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "shot.png", size=(200, 200))
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)
            selected = json.loads(db.get_task(conn, task.id).selected_skills or "[]")

        assert "untrusted_input" in selected

    def test_a_text_only_task_does_not_gain_untrusted_input(self, tmp_path, ocr):
        config = _make_config(tmp_path)
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, prompt="what is 2 + 2")
            _run(config, task, brain, conn)
            selected = json.loads(db.get_task(conn, task.id).selected_skills or "[]")

        assert "untrusted_input" not in selected


# --------------------------------------------------------------------------
# the brain request
# --------------------------------------------------------------------------


class TestBrainRequestImages:
    def test_prepared_images_reach_the_request(self, tmp_path, ocr):
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "shot.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)

        images = brain.reqs[-1].images
        assert [i.display_name for i in images] == ["shot.png"]
        assert images[0].media_type == "image/png"
        assert images[0].path.is_absolute()

    def test_an_omitted_image_is_not_on_the_request(self, tmp_path, ocr, monkeypatch):
        monkeypatch.setattr(image_attachments, "MAX_SOURCE_BYTES", 8)
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "shot.png", size=(200, 200))
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)

        assert brain.reqs[-1].images == []

    def test_a_text_only_task_carries_no_images(self, tmp_path, ocr):
        config = _make_config(tmp_path)
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, prompt="hello")
            _run(config, task, brain, conn)

        assert brain.reqs[-1].images == []

    def test_the_request_that_carries_images_also_carries_the_read_tool(
        self, tmp_path, ocr
    ):
        """Istota's half of the Claude Code vision contract.

        The other half — that the CLI really advertises `Read` — belongs to the
        binary, and the `image` tier cannot ask it: `claude --version` is the
        whole of what that tier can get out of it offline, so an assertion there
        would re-run a version probe and prove nothing. What this side owes is
        that an image-bearing request is a tool-bearing one, since
        `build_image_prompt` renders the mandatory-`Read` directive only for a
        request with tools and a named omission otherwise. The behavioural half
        is `tests/live/test_claude_code_read_image.py`.
        """
        config = _make_config(tmp_path)
        img = _png(tmp_path / "inbox" / "shot.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)

        req = brain.reqs[-1]
        assert req.images, "no image on the request, so the assertion is vacuous"
        assert "Read" in req.allowed_tools


# --------------------------------------------------------------------------
# cancellation
# --------------------------------------------------------------------------


class TestCancellation:
    def test_preparation_stops_at_the_first_poll_after_cancellation(
        self, tmp_path, ocr, monkeypatch
    ):
        config = _make_config(tmp_path)
        one = _png(tmp_path / "inbox" / "one.png")
        two = _png(tmp_path / "inbox" / "two.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(one), str(two)])
            conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task.id,)
            )
            conn.commit()
            _run(config, task, brain, conn)

        assert brain.reqs[-1].images == []
        assert ocr == []

    def test_audio_pre_transcription_polls_the_same_channel(self, tmp_path):
        """`!stop` must not be inert for the whole 900 s audio budget."""
        calls = []

        def fake(path, timeout=None):
            calls.append(path)
            return {"status": "ok", "text": "hello"}

        with patch.object(executor, "transcribe_audio_out_of_process", fake):
            out = executor._pre_transcribe_attachments(
                ["a.m4a", "b.m4a"], "typed", cancel_check=lambda: True,
            )

        assert calls == []
        assert out == "typed"


# --------------------------------------------------------------------------
# paths and binds
# --------------------------------------------------------------------------


class TestPathsAndBinds:
    def test_the_paths_named_to_the_model_are_the_ones_bwrap_binds(
        self, tmp_path, ocr, monkeypatch
    ):
        """The path the model is told to open exists inside the namespace.

        Read off the argv `execute_task` itself built — `req.sandbox_wrap` is
        that closure — rather than off a command this test assembles, because
        a hand-built one can only assert the binds the test supplied. That is
        the difference between checking the executor and checking the
        fixture.

        Also covers `temp_dir` behind a symlink: an unresolved path names no
        file inside.
        """
        real = tmp_path / "real-temp"
        real.mkdir()
        link = tmp_path / "temp-link"
        link.symlink_to(real)
        config = _make_config(tmp_path)
        config.temp_dir = link
        config.security.sandbox_enabled = True
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setattr(executor, "_bwrap_supports_remount_ro", lambda: True)
        monkeypatch.setattr(executor, "_bwrap_supports_disable_userns", lambda: True)
        img = _png(tmp_path / "inbox" / "pano.png", size=(3000, 2000))
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)

        req = brain.reqs[-1]
        assert req.sandbox_wrap is not None, (
            "no namespace was built, so this asserts nothing about binds"
        )
        cmd = req.sandbox_wrap(["true"])
        prepared = req.images[0].path
        # Both bind flags. The model's own working directory is read-write and
        # the control directory can only ever be read-only, so a set built from
        # `--bind` alone would answer a narrower question than the one the
        # class is named for.
        binds = {
            cmd[i + 2] for i, tok in enumerate(cmd)
            if tok in ("--bind", "--ro-bind") and i + 2 < len(cmd)
        }
        assert any(
            prepared.is_relative_to(Path(dest)) for dest in binds
        ), f"{prepared} is under none of {sorted(binds)}"

    def test_the_rendition_lands_in_the_task_control_directory(
        self, tmp_path, ocr,
    ):
        """Where the prepared file goes, and that the prompt says so.

        Two halves of one claim. The rendition is written into the
        daemon-owned control directory rather than into the sandbox's own
        working directory, where the model could rewrite the picture it was
        about to be asked about and where the previous task's renditions were
        still readable. And the path interpolated into the `Attached files:`
        section is the path the file is actually at — a move that updated one
        and not the other would name the model a file that is not there.
        """
        config = _make_config(tmp_path)
        # Big enough that the area cap binds, so a rendition is written rather
        # than the source being named as-is.
        img = _png(tmp_path / "inbox" / "pano.png", size=(3000, 2000))
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)

        control = executor.get_task_control_dir(config, "alice", task.id)
        prepared = brain.reqs[-1].images[0].path
        assert prepared != img.resolve()
        assert prepared.is_relative_to(control / "attachments")
        assert prepared.is_file()
        assert not prepared.is_relative_to(
            executor.get_user_temp_dir(config, "alice").resolve()
        )
        # The rendered line, from the same run: one half of this without the
        # other passes on a run that named the model nothing at all.
        assert str(prepared) in _prompt_of(brain)

    def test_an_attachment_outside_every_bind_is_copied_in(
        self, tmp_path, ocr, monkeypatch
    ):
        """The nc-data fallback path is bound by nothing."""
        config = _make_config(tmp_path)
        config.security.sandbox_enabled = True
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setattr(executor, "_bwrap_supports_remount_ro", lambda: True)
        monkeypatch.setattr(executor, "_bwrap_supports_disable_userns", lambda: True)
        outside = tmp_path / "nc-data" / "alice" / "files" / "Talk"
        img = _png(outside / "shot.png")
        brain = _CaptureBrain()

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            _run(config, task, brain, conn)

        prepared = brain.reqs[-1].images[0].path
        assert prepared != img.resolve()
        assert prepared.is_relative_to(config.temp_dir.resolve())


# --------------------------------------------------------------------------
# the Read audit
# --------------------------------------------------------------------------


def _trace_read(path: Path) -> dict:
    return {"type": "tool", "text": f"📄 Reading {path.name}"}


class TestReadAudit:
    def _run_with_trace(self, tmp_path, ocr, trace, kind="claude_code", images=2):
        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind=kind)
        paths = [
            _png(tmp_path / "inbox" / f"shot{n}.png") for n in range(images)
        ]
        brain = _CaptureBrain(
            BrainResult(
                success=True, result_text="the answer",
                stop_reason="completed",
                execution_trace=json.dumps(trace) if trace is not None else None,
            ),
            kind=kind,
        )
        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(p) for p in paths])
            ok, result, _actions, _trace = _run(config, task, brain, conn)
        return brain, result

    def test_an_image_never_read_is_named_in_the_result(self, tmp_path, ocr):
        config_brain, result = self._run_with_trace(tmp_path, ocr, trace=[])
        assert "shot0.png" in result
        assert "shot1.png" in result

    def test_only_the_unread_images_are_named(self, tmp_path, ocr):
        brain, _ = self._run_with_trace(tmp_path, ocr, trace=[])
        read_me = brain.reqs[-1].images[0].path
        brain2, result = self._run_with_trace(
            tmp_path, ocr, trace=[_trace_read(read_me)],
        )
        assert brain2.reqs[-1].images[0].display_name not in result
        assert brain2.reqs[-1].images[1].display_name in result

    def test_a_fully_read_set_adds_no_note(self, tmp_path, ocr):
        brain, _ = self._run_with_trace(tmp_path, ocr, trace=[])
        paths = [i.path for i in brain.reqs[-1].images]
        _brain2, result = self._run_with_trace(
            tmp_path, ocr, trace=[_trace_read(p) for p in paths],
        )
        assert "never opened" not in result

    def test_an_unreadable_trace_accuses_nobody(self, tmp_path, ocr):
        """Silence is not evidence that an image went unopened."""
        _brain, result = self._run_with_trace(tmp_path, ocr, trace=None)
        assert "never opened" not in result

    def test_the_native_brain_is_not_audited(self, tmp_path, ocr):
        """Its images went as content blocks; there is no `Read` to expect."""
        _brain, result = self._run_with_trace(tmp_path, ocr, trace=[], kind="native")
        assert "never opened" not in result


# --------------------------------------------------------------------------
# the fallback vision note
# --------------------------------------------------------------------------


class TestVisionDroppedNote:
    def test_a_primary_native_model_with_no_vision_says_so_too(
        self, tmp_path, ocr, monkeypatch
    ):
        """No reroute needed. The fact that matters is that nothing was seen.

        The brain tells the *model* it is missing the images; only the user
        reads the result, and a primary on a non-vision model otherwise answers
        with nothing anywhere saying it was written blind.
        """
        from istota.llm.catalog import ModelInfo

        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="native")
        monkeypatch.setattr(
            "istota.llm.catalog.get_model_info",
            lambda mid: ModelInfo(
                id=mid, context_window=200_000, max_output_tokens=8192,
                supports_vision=False,
            ),
        )
        img = _png(tmp_path / "inbox" / "shot.png")
        brain = _CaptureBrain(
            BrainResult(
                success=True, result_text="the answer", stop_reason="completed",
                model_used="blind-model",
            ),
            kind="native",
        )
        brain.model_namespace = "openai_compat"

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            ok, result, _a, _t = _run(config, task, brain, conn)

        assert ok
        assert "shot.png" in result
        assert "rerouted" not in result.lower()
        assert "ran on" in result.lower()

    def _run_fallback(self, tmp_path, ocr, monkeypatch, *, supports_vision):
        from istota.brain._fallback import reset_availability_breaker
        from istota.llm.catalog import ModelInfo

        reset_availability_breaker()
        config = _make_config(tmp_path)
        config.brain = BrainConfig(
            kind="claude_code", fallback="native", fallback_cooldown_seconds=0,
        )
        img = _png(tmp_path / "inbox" / "shot.png")
        primary = _CaptureBrain(
            BrainResult(
                success=False, result_text="limit", stop_reason="usage_limit",
            ),
            kind="claude_code",
        )
        fb = _CaptureBrain(
            BrainResult(
                success=True, result_text="the answer", stop_reason="completed",
                model_used="fb-model",
            ),
            kind="native",
        )
        fb.model_namespace = "openai_compat"

        monkeypatch.setattr(
            "istota.llm.catalog.get_model_info",
            lambda mid: ModelInfo(
                id=mid, context_window=200_000, max_output_tokens=8192,
                supports_vision=supports_vision,
            ),
        )
        monkeypatch.setattr(
            executor, "_native_with_user_key", lambda nc, *a, **k: nc,
        )

        def fake_make_brain(bc):
            return primary if getattr(bc, "kind", "") == "claude_code" else fb

        with db.get_db(config.db_path) as conn:
            task = _task(conn, attachments=[str(img)])
            with patch("istota.executor.make_brain", side_effect=fake_make_brain), \
                 patch("istota.notifications.send_notification", lambda *a, **k: None):
                ok, result, _a, _t = execute_task(task, config, [], conn=conn)
        reset_availability_breaker()
        return ok, result

    def test_a_fallback_that_drops_vision_says_so_in_the_result(
        self, tmp_path, ocr, monkeypatch
    ):
        ok, result = self._run_fallback(
            tmp_path, ocr, monkeypatch, supports_vision=False,
        )
        assert ok
        assert "shot.png" in result
        assert "without" in result.lower()
        assert "rerouted" in result.lower()

    def test_a_fallback_that_keeps_vision_adds_no_note(
        self, tmp_path, ocr, monkeypatch
    ):
        ok, result = self._run_fallback(
            tmp_path, ocr, monkeypatch, supports_vision=True,
        )
        assert ok
        assert "shot.png" not in result
