#!/usr/bin/env bash
# Write and sign the images manifest `istota-stack update --images` verifies.
#
#   scripts/sign-images-manifest.sh --tag vX.Y.Z --archive DIR/images-vX.Y.Z.tar \
#       --key ~/.ssh/release_key [--appliance FILE] [--claude-code-version V] \
#       REF=ID [REF=ID ...]
#
# Each REF=ID is one image in the archive, by the name compose runs it under and
# the ID `docker image inspect --format '{{.Id}}' REF` gives where it was saved
# (nginx by tag, nginx:alpine, never by digest). Writes images-<tag>.json beside
# the archive as canonical JSON (sorted keys, no whitespace, no trailing
# newline), the only form the stack accepts, and signs it with
# `ssh-keygen -Y sign -n istota-images` into images-<tag>.json.sig. The key is
# the release key the tags are signed with; nothing here reads git or Docker.
set -euo pipefail

tag="" archive="" key="" appliance="" claude_code_version=""
pairs=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --tag|--archive|--key|--appliance|--claude-code-version)
            [ "$#" -ge 2 ] || { echo "$1 needs a value" >&2; exit 64; }
            case "$1" in
                --tag) tag="$2" ;;
                --archive) archive="$2" ;;
                --key) key="$2" ;;
                --appliance) appliance="$2" ;;
                --claude-code-version) claude_code_version="$2" ;;
            esac
            shift ;;
        -*) echo "unknown option: $1" >&2; exit 64 ;;
        *) pairs+=("$1") ;;
    esac
    shift
done
[ -n "$tag" ] && [ -n "$archive" ] && [ -n "$key" ] \
    || { echo "--tag, --archive and --key are required" >&2; exit 64; }
[ "${#pairs[@]}" -gt 0 ] || { echo "name at least one image as REF=ID" >&2; exit 64; }
[ -f "$archive" ] || { echo "${archive} is not a file" >&2; exit 1; }
[ -z "$appliance" ] || [ -f "$appliance" ] || { echo "${appliance} is not a file" >&2; exit 1; }

manifest="$(dirname "$archive")/images-${tag}.json"
tmp="$(mktemp "${manifest}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

python3 -I - "$tmp" "$tag" "$archive" "$appliance" "$claude_code_version" "${pairs[@]}" <<'PY'
import hashlib, json, re, sys

out, tag, archive, appliance, claude_code_version, *pairs = sys.argv[1:]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


images = []
for pair in pairs:
    ref, sep, image_id = pair.partition("=")
    if not sep or not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]{1,128}", ref):
        sys.exit(f"not REF=ID with REF as name:tag: {pair!r}")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        sys.exit(f"{ref}: not a sha256 image ID: {image_id!r}")
    images.append({"ref": ref, "id": image_id})
doc = {"tag": tag, "archive_sha256": sha256(archive), "images": images}
if appliance:
    doc["appliance_sha256"] = sha256(appliance)
if claude_code_version:
    doc["claude_code_version"] = claude_code_version
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps(doc, sort_keys=True, separators=(",", ":")))
PY

mv "$tmp" "$manifest"
rm -f "${manifest}.sig"
ssh-keygen -q -Y sign -f "$key" -n istota-images "$manifest"
echo "$manifest"
echo "${manifest}.sig"
