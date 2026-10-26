"""The child that reads a PDF's text: stdlib and pypdf only, limits before input.

Run by `pdf_text.extract_text` as `python -I -B pdf_child.py MAX_PAGES MAX_CHARS
MEMORY_BYTES CPU_SECONDS`, with the document on stdin and one JSON object on
stdout, in UTF-8. Nothing here imports the browser service, so a parser that
misbehaves has nothing of it in its address space: no Patchright driver, no
session table, no credential values.

The order is the point. pypdf is imported and the limits are set before a byte
of the document is read, so everything the document can make the parser do is
bounded by them: address space (a decompression or object-graph bomb ends in
MemoryError), CPU time (a parsing loop ends in SIGXCPU), file size zero (the
parser has no reason to write a file) and a small descriptor count. On Linux a
limit that cannot be set refuses the run rather than parsing without it.

pypdf is pure Python, so a malformed file can raise, loop or allocate, and
cannot corrupt memory; that is why it is used here and poppler is not.
"""

import io
import json
import logging
import resource
import sys

import pypdf

PAGE_MARKER = "[page {number}]"


def _set_limits(memory_bytes, cpu_seconds):
    limits = (
        (resource.RLIMIT_AS, memory_bytes),
        (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_NOFILE, 32),
    )
    for which, value in limits:
        try:
            resource.setrlimit(which, (value, value))
        except (OSError, ValueError):
            # macOS refuses RLIMIT_AS; the container is Linux, where it holds.
            if sys.platform.startswith("linux"):
                return False
    return True


def _one_line(value, limit=300):
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(cleaned.split())[:limit]


def read(data, max_pages, max_chars):
    """The document's text as a result dict; never raises."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    return {"ok": False, "error": "pdf_encrypted"}
            except Exception:
                return {"ok": False, "error": "pdf_encrypted"}
        total = len(reader.pages)
        title = ""
        try:
            metadata = reader.metadata
            title = _one_line(metadata.title if metadata else "")
        except Exception:
            title = ""
        parts = []
        length = 0
        pages_read = 0
        unreadable = 0
        capped = False
        for index in range(total):
            if index >= max_pages or length >= max_chars:
                capped = True
                break
            try:
                text = reader.pages[index].extract_text() or ""
            except MemoryError:
                raise
            except Exception:
                text = ""
                unreadable += 1
            pages_read += 1
            chunk = PAGE_MARKER.format(number=index + 1) + "\n" + text.strip()
            parts.append(chunk)
            length += len(chunk) + 2
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars]
            capped = True
        return {
            "ok": True,
            "text": text,
            "title": title,
            "pages": total,
            "pages_read": pages_read,
            "pages_unreadable": unreadable,
            "capped": capped,
            "has_text": any(part.split("\n", 1)[-1].strip() for part in parts),
        }
    except MemoryError:
        return {"ok": False, "error": "pdf_too_complex"}
    except Exception:
        return {"ok": False, "error": "pdf_unreadable"}


def main(argv):
    try:
        max_pages, max_chars, memory_bytes, cpu_seconds = (int(value) for value in argv[1:5])
    except (TypeError, ValueError):
        return 2
    logging.disable(logging.CRITICAL)
    if not _set_limits(memory_bytes, cpu_seconds):
        result = {"ok": False, "error": "pdf_limits_unavailable"}
    else:
        try:
            data = sys.stdin.buffer.read()
        except MemoryError:
            data = None
        if data is None:
            result = {"ok": False, "error": "pdf_too_complex"}
        else:
            result = read(data, max_pages, max_chars)
    try:
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8", "replace")
    except MemoryError:
        encoded = b'{"ok": false, "error": "pdf_too_complex"}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
