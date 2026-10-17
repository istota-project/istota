#!/usr/bin/env python3
"""What a Mac app release may not contain, checked against the artifacts.

The Mac app ships an appliance disk and an images archive (the Mac app spec,
"Licences" and row M7). Neither may carry Claude Code or Google Chrome, whose
licences forbid redistribution; both are installed later inside the VM as
local layers. The disk may not carry its builder's identity either. And every
package the release ships is listed, with its licence where the ecosystem
declares one, and a licence that does not allow redistribution fails the
release.

    release-artifacts.py tree DIR            # a directory: the app bundle, a mounted DMG
    release-artifacts.py paths LISTING       # one path per line: the disk's file list
    release-artifacts.py disk RAW [--forbid LABEL=VALUE ...]
    release-artifacts.py images ARCHIVE [--packages OUT] [--disk-packages FILE]
                                        [--apt-snapshot ID] [--tag TAG]

`scripts/build-appliance.sh` runs all four. Exit 0 when clean, 1 with a finding
on each stdout line, 64 for a usage error. Standard library only: it runs with
the Mac's own python3.
"""

from __future__ import annotations

import argparse
import email.parser
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Claude Code's licence and launcher banner both carry this sentence. It is
# assembled here so that this file, which ships in the disk's checkout, is not
# itself a hit.
CLAUDE_MARKER = " ".join(["Anthropic PBC.", "All rights", "reserved."]).encode()

CLAUDE_PATH = re.compile(r"(^|/)node_modules/@anthropic-ai/claude-code(-[a-z0-9-]+)?(/|$)")
CHROME_PATH = re.compile(
    r"(^|/)opt/google/chrome(/|$)"
    # A launcher on PATH, not bash-completion's file of the same name, which
    # Debian ships for every system whether Chrome is installed or not.
    r"|(^|/)(usr/)?(local/)?s?bin/(google-chrome|google-chrome-stable)$"
    r"|(^|/)var/lib/dpkg/info/google-chrome[^/]*$"
)

CHUNK = 1 << 20

# A declared licence that does not allow the project to redistribute the
# package. Matched against short licence values only (an SPDX expression, a
# classifier, npm's field): a field holding a whole licence text says
# "commercial" in Apache-2.0's warranty clause.
DENIED_LICENCES = [
    (re.compile(r"\bSEE LICEN[CS]E IN\b", re.I), "a licence of its own (SEE LICENSE IN)"),
    (re.compile(r"\bUNLICENSED\b", re.I), "UNLICENSED, which grants nothing"),
    (re.compile(r"\bPROPRIETARY\b", re.I), "a proprietary licence"),
    (re.compile(r"\bBUSL\b|\bBUSINESS SOURCE\b", re.I), "the Business Source License"),
    (re.compile(r"\bSSPL\b|\bSERVER SIDE PUBLIC\b", re.I), "the Server Side Public License"),
    (re.compile(r"\bELASTIC-2\.0\b|\bELASTIC LICEN[CS]E\b", re.I), "the Elastic License"),
    (re.compile(r"\bCC-BY-NC|\bNON-?COMMERCIAL\b", re.I), "a non-commercial licence"),
]
SHORT_LICENCE = 200


def path_finding(path: str) -> str | None:
    """Why a path may not ship, or None."""
    path = path.removeprefix("./").lstrip("/")
    if CLAUDE_PATH.search(path):
        return "Claude Code's npm package"
    if CHROME_PATH.search(path):
        return "Google Chrome"
    return None


def licence_refusal(licence: str) -> str | None:
    if not licence or len(licence) > SHORT_LICENCE:
        return None
    for pattern, reason in DENIED_LICENCES:
        if pattern.search(licence):
            return reason
    return None


def stream_contains(stream, needle: bytes) -> bool:
    """Whether a byte stream holds `needle`, read in chunks with an overlap."""
    tail = b""
    while True:
        block = stream.read(CHUNK)
        if not block:
            return False
        window = tail + block
        if needle in window:
            return True
        tail = window[-(len(needle) - 1):] if len(needle) > 1 else b""


# --- a directory tree ---------------------------------------------------------


def scan_tree(root: Path) -> list[str]:
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            why = path_finding(rel)
            if why:
                findings.append(f"M7: {rel}: {why}")
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            with open(full, "rb") as f:
                if stream_contains(f, CLAUDE_MARKER):
                    findings.append(f"M7: {full.relative_to(root).as_posix()}: Claude Code's licence text")
    return findings


def scan_paths(listing: Path) -> list[str]:
    findings = []
    for line in listing.read_text(errors="replace").splitlines():
        why = path_finding(line.strip())
        if why:
            findings.append(f"M7: {line.strip()}: {why}")
    return findings


# --- a disk image -------------------------------------------------------------


def data_extents(f) -> list[tuple[int, int]]:
    """The (start, end) byte ranges of a sparse file that hold data."""
    fd = f.fileno()
    size = os.fstat(fd).st_size
    if not hasattr(os, "SEEK_DATA"):
        return [(0, size)]
    extents, offset = [], 0
    while offset < size:
        try:
            start = os.lseek(fd, offset, os.SEEK_DATA)
        except OSError:
            break
        end = os.lseek(fd, start, os.SEEK_HOLE)
        extents.append((start, end))
        offset = end
    return extents


def scan_disk(raw: Path, terms: dict[str, bytes]) -> list[str]:
    """Claude Code's licence text and each forbidden term, anywhere in the
    disk's data. Terms are reported by label: the value is private."""
    needles = {"Claude Code's licence text": CLAUDE_MARKER, **terms}
    overlap = max(len(n) for n in needles.values())
    hits: dict[str, list[int]] = {}
    with open(raw, "rb") as f:
        for start, end in data_extents(f):
            f.seek(start)
            offset, tail = start, b""
            while offset < end:
                block = f.read(min(CHUNK * 8, end - offset))
                if not block:
                    break
                window = tail + block
                base = offset - len(tail)
                for label, needle in needles.items():
                    at = window.find(needle)
                    while at != -1:
                        hits.setdefault(label, [])
                        if base + at not in hits[label]:
                            hits[label].append(base + at)
                        at = window.find(needle, at + 1)
                offset += len(block)
                tail = window[-(overlap - 1):] if overlap > 1 else b""
    findings = []
    for label, offsets in hits.items():
        shown = ", ".join(str(o) for o in offsets[:5])
        more = f" and {len(offsets) - 5} more" if len(offsets) > 5 else ""
        kind = "M7" if label == "Claude Code's licence text" else "identity"
        findings.append(f"{kind}: {raw.name}: {label} at byte {shown}{more}")
    return findings


# --- an images archive --------------------------------------------------------


@dataclass
class Layer:
    findings: list[str] = field(default_factory=list)
    dpkg_status: bytes | None = None
    apk_installed: bytes | None = None
    python: dict[tuple[str, str], str] = field(default_factory=dict)
    npm: dict[tuple[str, str], str] = field(default_factory=dict)


DIST_METADATA = re.compile(r"(^|/)[^/]+\.(dist-info/METADATA|egg-info/PKG-INFO)$")
NPM_MANIFEST = re.compile(r"(^|/)node_modules/((@[^/]+/)?[^/@][^/]*)/package\.json$")


def python_licence(text: str) -> tuple[str, str, str]:
    msg = email.parser.HeaderParser().parsestr(text)
    name, version = msg.get("Name", ""), msg.get("Version", "")
    expression = (msg.get("License-Expression") or "").strip()
    if expression:
        return name, version, expression
    classifiers = [c.split("::")[-1].strip() for c in msg.get_all("Classifier") or []
                   if c.startswith("License ::")]
    licence = (msg.get("License") or "").strip()
    first = licence.splitlines()[0].strip() if licence else ""
    if first and len(licence) <= SHORT_LICENCE:
        return name, version, first
    if classifiers:
        return name, version, "; ".join(classifiers)
    return name, version, f"{first} (text)" if first else ""


def npm_licence(text: str) -> tuple[str, str, str]:
    try:
        doc = json.loads(text)
    except ValueError:
        return "", "", ""
    if not isinstance(doc, dict):
        return "", "", ""
    licence = doc.get("license", "")
    if isinstance(licence, dict):
        licence = licence.get("type", "")
    if not licence and isinstance(doc.get("licenses"), list):
        licence = " OR ".join(str(item.get("type", "")) if isinstance(item, dict) else str(item)
                              for item in doc["licenses"])
    return str(doc.get("name", "")), str(doc.get("version", "")), str(licence or "")


def open_layer(blob):
    magic = blob.read(4)
    blob.seek(0)
    if magic[:2] == b"\x1f\x8b":
        return tarfile.open(fileobj=blob, mode="r|gz"), None
    if magic == b"\x28\xb5\x2f\xfd":
        proc = subprocess.Popen(["zstd", "-dc"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        def feed():
            while True:
                block = blob.read(CHUNK)
                if not block:
                    break
                proc.stdin.write(block)
            proc.stdin.close()

        threading.Thread(target=feed, daemon=True).start()
        return tarfile.open(fileobj=proc.stdout, mode="r|"), proc
    return tarfile.open(fileobj=blob, mode="r|"), None


def scan_layer(blob, where: str) -> Layer:
    layer = Layer()
    tar, proc = open_layer(blob)
    with tar:
        for member in tar:
            name = member.name.removeprefix("./").lstrip("/")
            why = path_finding(name)
            if why:
                layer.findings.append(f"M7: {where}: {name}: {why}")
            if not member.isfile():
                continue
            f = tar.extractfile(member)
            keep = (name == "var/lib/dpkg/status" or name == "lib/apk/db/installed"
                    or DIST_METADATA.search(name) or NPM_MANIFEST.search(name))
            if keep and member.size < 64 * CHUNK:
                data = f.read()
                if CLAUDE_MARKER in data:
                    layer.findings.append(f"M7: {where}: {name}: Claude Code's licence text")
                text = data.decode("utf-8", "replace")
                if name == "var/lib/dpkg/status":
                    layer.dpkg_status = data
                elif name == "lib/apk/db/installed":
                    layer.apk_installed = data
                elif DIST_METADATA.search(name):
                    pkg, version, licence = python_licence(text)
                    if pkg:
                        layer.python[(pkg, version)] = licence
                else:
                    pkg, version, licence = npm_licence(text)
                    if pkg:
                        layer.npm[(pkg, version)] = licence
            elif stream_contains(f, CLAUDE_MARKER):
                layer.findings.append(f"M7: {where}: {name}: Claude Code's licence text")
    if proc is not None:
        proc.wait()
    return layer


def dpkg_packages(status: bytes) -> list[tuple[str, str]]:
    packages = []
    for stanza in status.decode("utf-8", "replace").split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if fields.get("Package") and fields.get("Status", "").endswith(" installed"):
            packages.append((fields["Package"], fields.get("Version", "")))
    return sorted(packages)


def apk_packages(installed: bytes) -> list[tuple[str, str]]:
    packages, name = [], ""
    for line in installed.decode("utf-8", "replace").splitlines():
        if line.startswith("P:"):
            name = line[2:]
        elif line.startswith("V:") and name:
            packages.append((name, line[2:]))
            name = ""
    return sorted(packages)


@dataclass
class Image:
    ref: str
    os_packages: list[tuple[str, str]]
    os_kind: str
    python: dict[tuple[str, str], str]
    npm: dict[tuple[str, str], str]


def scan_images(archive: Path) -> tuple[list[str], list[Image]]:
    findings: list[str] = []
    images: list[Image] = []
    with tarfile.open(archive) as outer:
        manifest = json.load(outer.extractfile("manifest.json"))
        layers: dict[str, Layer] = {}
        for entry in manifest:
            for path in entry["Layers"]:
                if path not in layers:
                    blob = outer.extractfile(path)
                    layers[path] = scan_layer(blob, path)
                    findings.extend(layers[path].findings)
        for entry in manifest:
            ref = ", ".join(entry.get("RepoTags") or ["(untagged)"])
            status = apk = None
            python: dict[tuple[str, str], str] = {}
            npm: dict[tuple[str, str], str] = {}
            for path in entry["Layers"]:
                layer = layers[path]
                status = layer.dpkg_status if layer.dpkg_status is not None else status
                apk = layer.apk_installed if layer.apk_installed is not None else apk
                python.update(layer.python)
                npm.update(layer.npm)
            if status is not None:
                os_packages, kind = dpkg_packages(status), "dpkg"
            elif apk is not None:
                os_packages, kind = apk_packages(apk), "apk"
            else:
                os_packages, kind = [], "none"
            images.append(Image(ref, os_packages, kind, python, npm))
    for image in images:
        for ecosystem, packages in (("python", image.python), ("npm", image.npm)):
            for (name, version), licence in sorted(packages.items()):
                reason = licence_refusal(licence)
                if reason:
                    findings.append(f"licence: {image.ref}: {ecosystem} {name} {version}: {reason} ({licence})")
    return findings, images


def packages_report(images: list[Image], disk_packages: str, tag: str, snapshot: str) -> str:
    lines = [
        f"# Every package in the Istota {tag} release's appliance disk and images, with its version.",
        "# Python and npm packages carry the licence they declare; an empty one declares none.",
    ]
    if snapshot:
        lines.append(f"# The disk's Debian packages came from snapshot.debian.org at {snapshot}.")
    if disk_packages:
        lines += ["", f"## disk istota-appliance-{tag}.raw.zst (dpkg)"]
        lines += [line for line in disk_packages.splitlines() if line.strip()]
    for image in images:
        lines += ["", f"## image {image.ref} ({image.os_kind})"]
        lines += [f"{name} {version}" for name, version in image.os_packages]
        for ecosystem, packages in (("python", image.python), ("npm", image.npm)):
            if packages:
                lines += ["", f"## image {image.ref} ({ecosystem})"]
                lines += [f"{name} {version} {licence or '(none declared)'}"
                          for (name, version), licence in sorted(packages.items())]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tree").add_argument("dir", type=Path)
    sub.add_parser("paths").add_argument("listing", type=Path)
    disk = sub.add_parser("disk")
    disk.add_argument("raw", type=Path)
    disk.add_argument("--forbid", action="append", default=[], metavar="LABEL=VALUE")
    images = sub.add_parser("images")
    images.add_argument("archive", type=Path)
    images.add_argument("--packages", type=Path)
    images.add_argument("--disk-packages", type=Path)
    images.add_argument("--apt-snapshot", default="")
    images.add_argument("--tag", default="")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 64 if exc.code else 0

    if args.command == "tree":
        findings = scan_tree(args.dir)
    elif args.command == "paths":
        findings = scan_paths(args.listing)
    elif args.command == "disk":
        terms = {}
        for item in args.forbid:
            label, sep, value = item.partition("=")
            if not sep or not value:
                print(f"--forbid takes LABEL=VALUE, not {item!r}", file=sys.stderr)
                return 64
            terms[label] = value.encode()
        findings = scan_disk(args.raw, terms)
    else:
        findings, found = scan_images(args.archive)
        if args.packages:
            disk_packages = args.disk_packages.read_text() if args.disk_packages else ""
            args.packages.write_text(packages_report(found, disk_packages, args.tag, args.apt_snapshot))
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
