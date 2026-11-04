"""Where downloaded files go on disk, and how their names are made safe on Windows and Linux.

The same rules apply on both, so a crawl can move between machines and the files still merge."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .urls import DOC_EXTS, decode_component

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_EXTRA_MIME = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/epub+zip": ".epub",
    "application/x-rar-compressed": ".rar",
    "application/vnd.rar": ".rar",
    "application/x-7z-compressed": ".7z",
}


def safe_component(name: str, max_len: int = 120, max_bytes: int = 240) -> str:
    # max_bytes: Linux allows 255 bytes per name, and a Thai character is 3 bytes in UTF-8.
    name = _INVALID.sub("_", name).strip().rstrip(". ")
    if not name:
        name = "_"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > max_len or len(name.encode("utf-8")) > max_bytes:
        stem, ext = os.path.splitext(name)
        if len(ext) > 10:
            stem, ext = name, ""
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        stem = stem[: max_len - len(ext) - 9]
        while len(stem.encode("utf-8")) > max_bytes - len(ext.encode("utf-8")) - 9:
            stem = stem[:-1]
        name = f"{stem}~{digest}{ext}"
    return name


def disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.I)
    if m:
        name = unquote(m.group(2).strip().strip('"'), encoding=m.group(1) or "utf-8", errors="replace")
    else:
        m = re.search(r'filename\s*=\s*(?:"([^"]*)"|([^;]+))', value, re.I)
        if not m:
            return None
        name = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        # HTTP headers arrive decoded as latin-1; servers usually sent UTF-8 or TIS-620 bytes.
        name = _fix_header_text(name)
        if "%" in name:
            # Form-style encoding ("+" for space), as pis.parliament.go.th sends it.
            name = decode_component(name.replace("+", " "))
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or None


def _fix_header_text(s: str) -> str:
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return s
    for enc in ("utf-8", "cp874"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return s


def guess_ext(content_type: str) -> str:
    return _EXTRA_MIME.get(content_type) or mimetypes.guess_extension(content_type or "") or ""


def target_path(final_url: str, content_type: str, disposition: str | None,
                doc_exts: frozenset[str] = DOC_EXTS) -> Path:
    """Mirror the URL's host and path under the output dir: host/dir/.../name.ext"""
    parts = urlsplit(final_url)
    segs = [decode_component(s) for s in parts.path.split("/") if s]
    from_header = disposition_filename(disposition)
    last_is_file = bool(segs) and "." in segs[-1]

    if from_header:
        name, dirs = from_header, (segs[:-1] if last_is_file else segs)
    elif last_is_file:
        name, dirs = segs[-1], segs[:-1]
    else:
        name, dirs = "download", segs

    stem, ext = os.path.splitext(name)
    if parts.query and not from_header:
        # Same path, different query (e.g. ?id=1 vs ?id=2) must not collide.
        stem += "__" + hashlib.sha1(parts.query.encode()).hexdigest()[:8]
    typed = guess_ext(content_type)
    if ext.lstrip(".").lower() not in doc_exts and typed and typed.lower() != ext.lower():
        # No extension, or one that isn't a document's ("..._purchase_file.50" is a PDF): add the
        # real one, so the file opens with the right program.
        stem, ext = stem + ext, typed
    host = parts.netloc.replace(":", "_")
    return Path(safe_component(host), *(safe_component(d) for d in dirs), safe_component(stem + ext))


def fs(path: Path) -> str:
    """Path string usable by the OS, even beyond Windows' 260-character limit."""
    s = str(path.resolve())
    if os.name == "nt" and len(s) >= 240 and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s
