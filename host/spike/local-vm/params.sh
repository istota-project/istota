#!/bin/bash
# local-vm-install Stage 1 (g): can a Lima template take `cpus`, `memory`, the
# port forward and the workspace mount from params? Creates (never starts)
# scratch instances named spike-param-*, reads back what Lima resolved, and
# deletes them. Run on the Mac.
set -uo pipefail
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Each case declares only the params it uses: Lima refuses a declared param
# that nothing reads.
try() {
    local name="$1" params="$2" body="$3"; shift 3
    {
        printf 'minimumLimaVersion: 2.0.0\nbase:\n  - template:_images/debian-13\nvmType: vz\n'
        printf 'param:\n%s\n%s\n' "$params" "$body"
    } > "$TMP/$name.yaml"
    echo "== $name (create args: $*)"
    if limactl create --tty=false --name "spike-param-$name" "$@" "$TMP/$name.yaml" >"$TMP/$name.log" 2>&1; then
        echo "created: $(limactl list --format '{{.Name}} cpus={{.CPUs}} memory={{.Memory}}' "spike-param-$name")"
        grep -nE '^(cpus|memory):|hostPort|guestPort|location|mountPoint|PARAM' "$HOME/.lima/spike-param-$name/lima.yaml" | grep -v '^\s*[0-9]*:\s*#'
        limactl delete --force "spike-param-$name" >/dev/null 2>&1
    else
        echo "create failed: $(grep -E 'level=(fatal|error)' "$TMP/$name.log" | head -1 | sed 's/\\n.*//')"
    fi
}

PROV='provision:
  - mode: system
    script: |
      #!/bin/bash
      echo "${PARAM_X:-}"'

try cpus-string '  CPUS: "4"' 'cpus: "{{.Param.CPUS}}"
'"${PROV//PARAM_X/PARAM_CPUS}" --param CPUS=2
try memory-string '  MEMORY: "6GiB"' 'memory: "{{.Param.MEMORY}}"
'"${PROV//PARAM_X/PARAM_MEMORY}" --param MEMORY=3GiB
try memory-unused-elsewhere '  MEMORY: "6GiB"' 'memory: "{{.Param.MEMORY}}"' --param MEMORY=3GiB
try port-string '  PORT: "8080"' 'portForwards:
  - guestIP: 127.0.0.1
    guestPort: "{{.Param.PORT}}"
    hostIP: 127.0.0.1
    hostPort: "{{.Param.PORT}}"' --param PORT=8181
try mount-only '  WORKSPACE: "/tmp"' 'mountType: virtiofs
mounts:
  - location: "{{.Param.WORKSPACE}}"
    mountPoint: /srv/mount/host-folder
    writable: true' --param WORKSPACE=/private/tmp
try mount-and-provision '  WORKSPACE: "/tmp"' 'mountType: virtiofs
mounts:
  - location: "{{.Param.WORKSPACE}}"
    mountPoint: /srv/mount/host-folder
    writable: true
'"${PROV//PARAM_X/PARAM_WORKSPACE}" --param WORKSPACE=/private/tmp
try flags '  UNUSED_GUARD: "x"' "${PROV//PARAM_X/PARAM_UNUSED_GUARD}" --cpus 2 --memory 3
try set-port '  UNUSED_GUARD: "x"' "${PROV//PARAM_X/PARAM_UNUSED_GUARD}
portForwards:
  - guestIP: 127.0.0.1
    guestPort: 8080
    hostIP: 127.0.0.1
    hostPort: 8080" --set '.portForwards[0].guestPort = 8181 | .portForwards[0].hostPort = 8181'
