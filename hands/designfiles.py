"""Design files: checks, metadata stripping and the refs/design store (docs/design.md, "Design files").

Everything here is stdlib Python and never decodes a raster image: raster files are only verified (magic, PNG
chunks, JPEG APP segments, dimensions from headers). Re-encoding is the sanitizer's job (capture/sanitize-media.mjs);
`prepare` stages the files for it and `ingest` accepts only its verified output.

Stage layout:  <stage>/meta.json {name, kind, note, files}, <stage>/files/<rel> (text, fonts, cleaned SVG,
stripped video), <stage>/raw/<rel> (raster images and SVGs for the sanitizer), <stage>/clean/<rel> and
<stage>/clean/thumbs/<rel>.png (written by the sanitizer).
Store layout:  <store>/refs/design/<id>/<rel> and <store>/refs/index.json
               {"schema": 1, "last_id": N, "files": [{id, kind, file, files, note, sha256, size, uploaded_at}]}.

CLI: python3 -m hands.designfiles guess-kind|safe-name|check|prepare|ingest|remove|edit|group|list|metadata|clean-png
Exit codes: 0 ok, 1 refused or invalid (reason on stdout as JSON with --json, else stderr), 2 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from secrets import token_hex

from .projectenv import Redactor

MIB = 1048576
KINDS = ("design-md", "style-pack", "screenshot", "brand", "avoid", "font", "icon-pack", "video", "copy-tone",
         "feature-list")
EXT_CLASS = {".md": "text", ".json": "text", ".css": "text", ".txt": "text",
             ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".svg": "image",
             ".woff2": "font", ".ttf": "font", ".otf": "font", ".zip": "zip", ".mp4": "video", ".webm": "video"}
DEFAULT_CAPS = {"text": MIB, "image": 15 * MIB, "font": 5 * MIB, "zip": 25 * MIB, "video": 50 * MIB,
                "submit": 200 * MIB}
TYPE_HINTS = {".heic": " (HEIC: share the photo as JPEG)", ".heif": " (HEIC: share the photo as JPEG)",
              ".mov": " (MOV: export as MP4)"}
RASTER = (".png", ".jpg", ".jpeg", ".webp")
TEXT_EXT = (".md", ".json", ".css", ".txt")
MAX_NOTE = 500
MAX_NAME = 80
MAX_DIM = 2560
MAX_PNG_DIM = 16384
ZIP_MEMBERS = 200
ZIP_TOTAL = 100 * MIB
ZIP_RATIO = 100
ZIP_INNER = ("text", "image", "font")
PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_KEEP = (b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"sRGB", b"cHRM")
PNG_META = (b"tEXt", b"iTXt", b"zTXt", b"eXIf")
SECRET_PATTERNS = (("private key", re.compile(r"-----BEGIN")), ("sk-ant-", re.compile(r"sk-ant-")),
                   ("GitHub token", re.compile(r"ghp_|gho_|github_pat_")),
                   ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")), ("Slack token", re.compile(r"xox[abpr]-")),
                   ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")))
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
SVG_ELEMENTS = frozenset(
    "svg g defs symbol use path rect circle ellipse line polyline polygon text tspan textPath linearGradient "
    "radialGradient stop clipPath mask pattern style".split())
SVG_DROPPED = frozenset(("title", "desc", "metadata"))
SVG_ATTRS = frozenset(
    "id class style transform d x y x1 y1 x2 y2 cx cy r rx ry fx fy fr width height viewBox preserveAspectRatio points "
    "fill fill-opacity fill-rule stroke stroke-width stroke-linecap stroke-linejoin stroke-miterlimit "
    "stroke-dasharray stroke-dashoffset stroke-opacity opacity clip-path clip-rule mask offset stop-color "
    "stop-opacity gradientUnits gradientTransform spreadMethod font-family font-size font-weight font-style "
    "text-anchor dominant-baseline letter-spacing dx dy rotate textLength lengthAdjust patternUnits "
    "patternContentUnits patternTransform maskUnits maskContentUnits clipPathUnits color display visibility "
    "overflow vector-effect shape-rendering version baseProfile href".split())
URL_REF = re.compile(r"url\(\s*['\"]?\s*([^)'\"\s]*)", re.I)

ET.register_namespace("xlink", XLINK_NS)


class Refused(Exception):
    """A file or request was refused; the message is the reason shown to the user."""


# ------------------------------------------------------------------ names and kinds
def guess_kind(name: str) -> str:
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if "tone" in low or "voice" in low:
        return "copy-tone"
    if "feature" in low:
        return "feature-list"
    if "avoid" in low or "dont" in low:
        return "avoid"
    if ext == ".md":
        return "design-md"
    if ext in (".json", ".css"):
        return "style-pack"
    if ext == ".txt":
        return "copy-tone"
    if ext in RASTER:
        return "screenshot"
    if ext == ".svg":
        return "brand" if ("logo" in low or "brand" in low) else "icon-pack"
    if EXT_CLASS.get(ext) == "font":
        return "font"
    if ext == ".zip":
        return "icon-pack"
    if EXT_CLASS.get(ext) == "video":
        return "video"
    return "design-md"


def safe_name(name: str) -> str:
    base = re.split(r"[\\/]", unicodedata.normalize("NFKC", str(name)))[-1]
    base = re.sub(r"\s+", "-", base)
    base = re.sub(r"[^A-Za-z0-9._-]", "", base)
    stem, ext = os.path.splitext(base)
    if not ext and stem.startswith("."):
        stem, ext = "", ""
    stem = re.sub(r"-{2,}", "-", stem).strip(".-") or "file"
    ext = re.sub(r"[^a-z0-9.]", "", ext.lower())
    return stem[: max(1, MAX_NAME - len(ext))] + ext


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _caps(caps: dict | None) -> dict:
    out = dict(DEFAULT_CAPS)
    for k, v in (caps or {}).items():
        if k not in DEFAULT_CAPS or not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise Refused(f"invalid cap {k}")
        out[k] = v
    return out


# ------------------------------------------------------------------ secrets
def secret_hits(text: str, secrets: dict | None) -> list[str]:
    """Names of the patterns and project variables found in text; never the values."""
    hits = [label for label, rx in SECRET_PATTERNS if rx.search(text)]
    if secrets:
        hits += [f"project variable {k}" for k in Redactor(secrets).found(text.encode("utf-8"))]
    return hits


def load_secrets(path: str | Path | None) -> dict:
    out: dict[str, str] = {}
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v
    return out


# ------------------------------------------------------------------ raster headers
def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from(">I", b, o)[0]


def _png_chunks(data: bytes):
    """Yield (type, payload, crc_ok) for every chunk; raises Refused on truncation."""
    pos = 8
    while pos + 12 <= len(data):
        n = _u32(data, pos)
        end = pos + 12 + n
        if end > len(data):
            raise Refused("image could not be decoded")
        kind, payload = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        yield kind, payload, zlib.crc32(kind + payload) == _u32(data, pos + 8 + n)
        pos = end
        if kind == b"IEND":
            return


def _jpeg_segments(data: bytes):
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            pos += 2
            continue
        n = struct.unpack_from(">H", data, pos + 2)[0]
        yield marker, data[pos + 4:pos + 2 + n]
        if marker == 0xDA:
            return
        pos += 2 + n


def image_size(data: bytes, ext: str) -> tuple[int, int] | None:
    try:
        if ext == ".png":
            return _u32(data, 16), _u32(data, 20)
        if ext in (".jpg", ".jpeg"):
            for marker, body in _jpeg_segments(data):
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack_from(">HH", body, 1)
                    return w, h
            return None
        if ext == ".webp":
            kind = data[12:16]
            if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack_from("<HH", data, 26)
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L" and data[20] == 0x2F:
                bits = struct.unpack_from("<I", data, 21)[0]
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    except (struct.error, IndexError):
        return None
    return None


def metadata_markers(path: str | Path) -> list[str]:
    """exif, xmp, png:tEXt|iTXt|zTXt|eXIf, webp:EXIF|XMP found in a PNG, JPEG or WebP file."""
    data = Path(path).read_bytes()
    found: list[str] = []

    def add(m: str) -> None:
        if m not in found:
            found.append(m)
    if data.startswith(PNG_SIG):
        try:
            for kind, _, _ in _png_chunks(data):
                if kind in PNG_META:
                    add("png:" + kind.decode())
        except Refused:
            pass
    elif data.startswith(b"\xff\xd8\xff"):
        for marker, body in _jpeg_segments(data):
            if marker == 0xE1 and body.startswith(b"Exif\0"):
                add("exif")
            elif marker == 0xE1 and body.startswith(b"http://ns.adobe.com/xap/1.0/"):
                add("xmp")
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        pos = 12
        while pos + 8 <= len(data):
            kind, n = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
            if kind == b"EXIF":
                add("webp:EXIF")
            elif kind == b"XMP ":
                add("webp:XMP")
            pos += 8 + n + (n & 1)
    return found


def clean_png(data: bytes) -> bytes:
    """Keep only IHDR PLTE IDAT IEND tRNS gAMA sRGB cHRM; verifies the signature and every CRC."""
    if not data.startswith(PNG_SIG):
        raise Refused("does not match its extension")
    out = [PNG_SIG]
    seen_end = False
    for i, (kind, payload, crc_ok) in enumerate(_png_chunks(data)):
        if not crc_ok or (i == 0) != (kind == b"IHDR"):
            raise Refused("image could not be decoded")
        if kind == b"IHDR":
            w, h = struct.unpack(">II", payload[:8])
            if not (0 < w <= MAX_PNG_DIM and 0 < h <= MAX_PNG_DIM):
                raise Refused("image could not be decoded")
        if kind in PNG_KEEP:
            out.append(struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload)))
        seen_end = kind == b"IEND"
    if not seen_end:
        raise Refused("image could not be decoded")
    return b"".join(out)


# ------------------------------------------------------------------ SVG
def _svg_value(value: str) -> None:
    flat = re.sub(r"[\s\x00-\x1f]", "", value).lower()
    if "javascript:" in flat or "expression(" in flat:
        raise Refused("svg: script")
    if "@import" in flat:
        raise Refused("svg: external reference")
    for ref in URL_REF.findall(value):
        if not ref.startswith("#"):
            raise Refused("svg: external reference")


def _local(tag: str) -> tuple[str, str]:
    ns, _, name = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
    return ns, name


def _svg_copy(src: ET.Element, parent: ET.Element | None) -> ET.Element | None:
    ns, name = _local(src.tag)
    if ns not in ("", SVG_NS):
        return None
    for key, value in src.attrib.items():
        kns, kname = _local(key)
        if kname.lower().startswith("on"):
            raise Refused("svg: event handler")
        _svg_value(value)
        if kname == "href" and not value.strip().startswith("#"):
            raise Refused("svg: external reference")
    if name == "script":
        raise Refused("svg: script")
    if name == "foreignObject":
        raise Refused("svg: foreignObject")
    if name in SVG_DROPPED:
        return None
    if name not in SVG_ELEMENTS:
        raise Refused("svg: element not allowed")
    if name == "style" and src.text:
        _svg_value(src.text)
    attrs = {}
    for key, value in src.attrib.items():
        kns, kname = _local(key)
        if kname == "href" and kns in ("", XLINK_NS):
            attrs[f"{{{XLINK_NS}}}href" if kns else "href"] = value.strip()
        elif kns == "" and kname in SVG_ATTRS and kname != "href":
            attrs[kname] = value
    el = ET.Element(name, attrs)
    if name in ("text", "tspan", "textPath", "style"):
        el.text = src.text
    for child in src:
        made = _svg_copy(child, el)
        if made is not None:
            el.append(made)
            if name in ("text", "tspan", "textPath"):
                made.tail = child.tail
    return el


def sanitize_svg(data: bytes) -> bytes:
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise Refused("svg: doctype")
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        raise Refused("does not match its extension") from None
    if _local(root.tag) not in (("", "svg"), (SVG_NS, "svg")):
        raise Refused("does not match its extension")
    clean = _svg_copy(root, None)
    clean.set("xmlns", SVG_NS)
    return ET.tostring(clean, encoding="utf-8", xml_declaration=True)


# ------------------------------------------------------------------ video metadata
def _mp4_strip(buf: bytearray, start: int, end: int) -> None:
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", buf, pos)
        head = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, head = struct.unpack_from(">Q", buf, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            return
        if kind in (b"udta", b"meta"):
            buf[pos + head:pos + size] = bytes(size - head)
            buf[pos + 4:pos + 8] = b"free"
        elif kind in (b"moov", b"trak"):
            _mp4_strip(buf, pos + head, pos + size)
        pos += size


def _vint(buf: bytes, pos: int, keep_marker: bool) -> tuple[int, int, bool] | None:
    """(value, length, unknown_size) of the EBML variable-length integer at pos."""
    if pos >= len(buf) or buf[pos] == 0:
        return None
    n = 9 - buf[pos].bit_length()
    if pos + n > len(buf):
        return None
    raw = int.from_bytes(buf[pos:pos + n], "big")
    value = raw if keep_marker else raw & ((1 << (7 * n)) - 1)
    return value, n, (not keep_marker) and value == (1 << (7 * n)) - 1


def _void(total: int) -> bytes:
    for n in range(8, 0, -1):
        payload = total - 1 - n
        if payload >= 0 and payload < (1 << (7 * n)) - 1:
            return b"\xec" + ((1 << (7 * n)) | payload).to_bytes(n, "big") + bytes(payload)
    raise Refused("does not match its extension")


def _webm_strip(buf: bytearray) -> None:
    def walk(pos: int, end: int, into_segment: bool) -> None:
        while pos < end:
            ident = _vint(buf, pos, True)
            if ident is None:
                return
            size = _vint(buf, pos + ident[1], False)
            if size is None:
                return
            head = ident[1] + size[1]
            stop = end if size[2] else pos + head + size[0]
            if stop > end:
                return
            if ident[0] == 0x18538067:
                walk(pos + head, stop, True)
            elif ident[0] == 0x1254C367 and into_segment:
                buf[pos:stop] = _void(stop - pos)
            elif size[2]:
                return
            pos = stop
    walk(0, len(buf), False)


def _strip_video_bytes(data: bytes, ext: str) -> bytes:
    buf = bytearray(data)
    if ext == ".mp4":
        _mp4_strip(buf, 0, len(buf))
    else:
        _webm_strip(buf)
    return bytes(buf)


def strip_video(src, dst) -> None:
    """Copy src to dst with MP4 udta/meta boxes zeroed (renamed free) or WebM Tags replaced by a Void element."""
    Path(dst).write_bytes(_strip_video_bytes(Path(src).read_bytes(), _ext(str(src))))


# ------------------------------------------------------------------ checks
def _magic_ok(data: bytes, ext: str) -> bool:
    if ext == ".png":
        return data.startswith(PNG_SIG)
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return {".woff2": data[:4] == b"wOF2", ".ttf": data[:4] in (b"\0\1\0\0", b"true"), ".otf": data[:4] == b"OTTO",
            ".zip": data[:4] == b"PK\x03\x04", ".mp4": data[4:8] == b"ftyp",
            ".webm": data[:4] == b"\x1a\x45\xdf\xa3"}.get(ext, True)


def _validate(data: bytes, ext: str, secrets: dict | None) -> bytes | None:
    """Content checks for one file; returns the bytes to store when they differ (SVG re-serialised)."""
    cls = EXT_CLASS[ext]
    if not _magic_ok(data, ext):
        raise Refused("does not match its extension")
    if ext in RASTER:
        size = image_size(data, ext)
        if size is None or not (0 < size[0] <= MAX_PNG_DIM and 0 < size[1] <= MAX_PNG_DIM):
            raise Refused("image could not be decoded")
        return None
    if cls == "text" or ext == ".svg":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("does not match its extension") from None
        if "\0" in text:
            raise Refused("does not match its extension")
        if ext == ".json":
            try:
                json.loads(text)
            except ValueError:
                raise Refused("does not match its extension") from None
        hits = secret_hits(text, secrets)
        if hits:
            raise Refused(f"secret found ({', '.join(hits)})")
        if ext == ".svg":
            return sanitize_svg(data)
    return None


def _check_zip(path: Path, secrets: dict | None, caps: dict) -> list[tuple[str, str, bytes]]:
    """Validate a ZIP member by member; returns [(rel, ext, data)] with SVGs re-serialised."""
    def refuse(why: str):
        raise Refused(f"zip: {why}")
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise Refused("does not match its extension") from None
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(z.infolist()) > ZIP_MEMBERS:
            refuse(f"more than {ZIP_MEMBERS} members")
        if sum(i.file_size for i in infos) > ZIP_TOTAL:
            refuse("too large when unpacked")
        out, used = [], set()
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or ".." in re.split(r"[\\/]", name):
                refuse(f"unsafe path {safe_name(name)}")
            if stat.S_ISLNK(info.external_attr >> 16):
                refuse(f"symlink {safe_name(name)}")
            if info.flag_bits & 1:
                refuse("encrypted member")
            parts = [safe_name(p) for p in re.split(r"[\\/]", name) if p]
            rel = "/".join(parts)
            ext = _ext(rel)
            if ext == ".zip":
                refuse(f"nested zip {rel}")
            if EXT_CLASS.get(ext) not in ZIP_INNER:
                refuse(f"{rel} type not allowed")
            if info.compress_size and info.file_size / info.compress_size > ZIP_RATIO:
                refuse(f"{rel} compresses too well")
            limit = caps[EXT_CLASS[ext]]
            if info.file_size > limit:
                refuse(f"{rel} too large")
            with z.open(info) as f:
                data = f.read(limit + 1)
            if len(data) > limit:
                refuse(f"{rel} too large")
            n = 2
            while rel in used:
                stem, e = os.path.splitext("/".join(parts))
                rel, n = f"{stem}-{n}{e}", n + 1
            used.add(rel)
            try:
                cleaned = _validate(data, ext, secrets)
            except Refused as e:
                raise Refused(f"zip: {rel}: {e}") from None
            out.append((rel, ext, cleaned if cleaned is not None else data))
    if not out:
        refuse("no files")
    return out


def _inspect(path, name: str, kind: str | None, secrets: dict | None, caps: dict | None):
    caps = _caps(caps)
    safe = safe_name(name)
    ext = _ext(safe)
    if ext not in EXT_CLASS:
        raise Refused("type not allowed" + TYPE_HINTS.get(ext, ""))
    cls = EXT_CLASS[ext]
    size = Path(path).stat().st_size
    if size > caps[cls]:
        raise Refused("too large")
    kind = kind or guess_kind(safe)
    if kind not in KINDS:
        raise Refused(f"unknown kind {kind}")
    data = Path(path).read_bytes()
    members = None
    cleaned = None
    if ext == ".zip":
        if not _magic_ok(data, ext):
            raise Refused("does not match its extension")
        members = _check_zip(Path(path), secrets, caps)
    else:
        cleaned = _validate(data, ext, secrets)
    info = {"ok": True, "name": safe, "ext": ext, "class": cls, "kind": kind, "size": size}
    return info, data, cleaned, members


def check(path, name: str, kind: str | None = None, secrets: dict | None = None, caps: dict | None = None) -> dict:
    return _inspect(path, name, kind, secrets, caps)[0]


def _check_note(note: str) -> str:
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise Refused(f"note too long (max {MAX_NOTE} characters)")
    return note


def prepare(path, name: str, kind: str, note: str, stage, secrets: dict | None = None, caps: dict | None = None) -> dict:
    info, data, cleaned, members = _inspect(path, name, kind, secrets, caps)
    _check_note(note or "")
    stage = Path(stage)
    (stage / "files").mkdir(parents=True, exist_ok=True)
    (stage / "raw").mkdir(exist_ok=True)

    def put(sub: str, rel: str, payload: bytes) -> None:
        target = stage / sub / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    def place(rel: str, ext: str, payload: bytes) -> None:
        if ext in RASTER:
            put("raw", rel, payload)
        elif ext == ".svg":
            put("files", rel, payload)
            put("raw", rel, payload)
        elif ext in (".mp4", ".webm"):
            put("files", rel, _strip_video_bytes(payload, ext))
        else:
            put("files", rel, payload)
    if members is not None:
        files = [rel for rel, _, _ in members]
        for rel, ext, payload in members:
            place(rel, ext, payload)
    else:
        files = [info["name"]]
        place(info["name"], info["ext"], cleaned if cleaned is not None else data)
    meta = {"name": info["name"], "kind": info["kind"], "note": note or "", "files": files}
    (stage / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


# ------------------------------------------------------------------ store
def _index_path(store) -> Path:
    return Path(store) / "refs" / "index.json"


def _empty_index() -> dict:
    return {"schema": 1, "last_id": 0, "files": []}


def _read_index(path: Path) -> dict:
    try:
        idx = json.loads(path.read_text(encoding="utf-8"))
        idx["files"] = list(idx["files"])
        idx["last_id"] = max(int(idx.get("last_id", 0)), *(int(e["id"][1:]) for e in idx["files"]
                                                             if re.fullmatch(r"d[0-9]{3}", e["id"])), 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Refused("refs/index.json is not valid") from None
    idx["schema"] = 1
    return idx


def load_index(store) -> dict:
    path = _index_path(store)
    return _read_index(path) if path.is_file() else _empty_index()


def _save_index(store, idx: dict) -> None:
    path = _index_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".index.json.{os.getpid()}")
    tmp.write_text(json.dumps(idx, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _rel_ok(rel: str) -> bool:
    return bool(rel) and all(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", p) for p in rel.split("/"))


def _now(now) -> str:
    return now if isinstance(now, str) else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _entry(idx_id: str, kind: str, note: str, files: list[str], main: bytes, size: int, now) -> dict:
    return {"id": idx_id, "kind": kind, "file": files[0], "files": files, "note": note,
            "sha256": hashlib.sha256(main).hexdigest(), "size": size, "uploaded_at": _now(now)}


def _next_id(idx: dict) -> str:
    if idx["last_id"] >= 999:
        raise Refused("design file ids are used up")
    idx["last_id"] += 1
    return f"d{idx['last_id']:03d}"


def _install_dir(store: Path, tmp: Path, ident: str) -> None:
    dest = store / "refs" / "design" / ident
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dest)


def ingest(store, stage, thumbs=None, now=None, base_index=None) -> dict:
    store, stage = Path(store), Path(stage)
    try:
        meta = json.loads((stage / "meta.json").read_text(encoding="utf-8"))
        kind, note, files = meta["kind"], meta.get("note", ""), meta["files"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused("stage has no valid meta.json") from None
    if kind not in KINDS:
        raise Refused(f"unknown kind {kind}")
    _check_note(note)
    if not files or not all(isinstance(f, str) and _rel_ok(f) for f in files):
        raise Refused("stage lists an invalid file name")
    payloads: list[bytes] = []
    for rel in files:
        ext = _ext(rel)
        if ext in RASTER:
            clean = stage / "clean" / rel
            if not clean.is_file():
                raise Refused(f"image not sanitised: {rel}")
            data = clean.read_bytes()
            if not _magic_ok(data, ext):
                raise Refused(f"does not match its extension: {rel}")
            size = image_size(data, ext)
            if size is None or max(size) > MAX_DIM or min(size) < 1:
                raise Refused(f"image could not be decoded: {rel}")
            if metadata_markers(clean):
                raise Refused(f"metadata left in {rel}")
            payloads.append(clean_png(data) if ext == ".png" else data)
        else:
            src = stage / "files" / rel
            if not src.is_file():
                raise Refused(f"missing staged file: {rel}")
            payloads.append(src.read_bytes())
    thumb = next((t for t in (stage / "clean" / "thumbs" / f"{rel}.png" for rel in files) if t.is_file()), None)

    index = _index_path(store)
    if index.is_file():
        idx = _read_index(index)
    elif base_index and Path(base_index).is_file():
        idx = _read_index(Path(base_index))
    else:
        idx = _empty_index()
    ident = _next_id(idx)
    (store / "refs").mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=store / "refs"))
    try:
        for rel, payload in zip(files, payloads):
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        _install_dir(store, tmp, ident)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    entry = _entry(ident, kind, note, [f"refs/design/{ident}/{r}" for r in files], payloads[0],
                   sum(len(p) for p in payloads), now)
    if thumb is not None and thumbs:
        Path(thumbs).mkdir(parents=True, exist_ok=True)
        tt = Path(thumbs) / f".{ident}.png.tmp"
        shutil.copyfile(thumb, tt)
        os.replace(tt, Path(thumbs) / f"{ident}.png")
    idx["files"].append(entry)
    _save_index(store, idx)
    return entry


def _find(idx: dict, ident: str) -> dict:
    for e in idx["files"]:
        if e["id"] == ident:
            return e
    raise Refused(f"no design file {ident}")


def _rmdir(store: Path, ident: str) -> None:
    shutil.rmtree(store / "refs" / "design" / ident, ignore_errors=True)


def remove(store, ident: str) -> None:
    idx = load_index(store)
    entry = _find(idx, ident)
    idx["files"].remove(entry)
    _save_index(store, idx)
    _rmdir(Path(store), ident)


def edit(store, ident: str, kind: str | None = None, note: str | None = None) -> dict:
    idx = load_index(store)
    entry = _find(idx, ident)
    if kind is not None:
        if kind not in KINDS:
            raise Refused(f"unknown kind {kind}")
        entry["kind"] = kind
    if note is not None:
        entry["note"] = _check_note(note)
    _save_index(store, idx)
    return entry


def group(store, ids: list[str], note: str = "") -> dict:
    """Merge text-only entries into one new style-pack entry; the old entries and their ids are retired."""
    store = Path(store)
    _check_note(note)
    idx = load_index(store)
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise Refused("pick at least two different files to group")
    entries = [_find(idx, i) for i in ids]
    sources: list[tuple[str, Path]] = []
    for e in entries:
        for f in e["files"]:
            if _ext(f) not in TEXT_EXT:
                raise Refused("only .md .json .css .txt files can be grouped")
            sources.append((f, store / f))
    ident = _next_id(idx)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=_index_path(store).parent)) if _index_path(store).parent.is_dir() \
        else None
    if tmp is None:
        raise Refused("store is empty")
    try:
        names: list[str] = []
        for rel, src in sources:
            base = rel.rsplit("/", 1)[-1]
            n, stem, e = 2, *os.path.splitext(base)
            while base in names:
                base, n = f"{stem}-{n}{e}", n + 1
            names.append(base)
            shutil.copyfile(src, tmp / base)
        _install_dir(store, tmp, ident)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    main = (store / "refs" / "design" / ident / names[0]).read_bytes()
    entry = _entry(ident, "style-pack", note, [f"refs/design/{ident}/{n}" for n in names], main,
                   sum(e["size"] for e in entries), None)
    idx["files"] = [e for e in idx["files"] if e["id"] not in ids] + [entry]
    _save_index(store, idx)
    for i in ids:
        _rmdir(store, i)
    return entry


# ------------------------------------------------------------------ sanitizer container and upload pipeline
SANITIZE_TIMEOUT_S = 120
STATUS_KEEP_S = 7 * 86400
CHUNK = 65536


def _host_path(p) -> str:
    s = os.path.abspath(str(p))
    if ":" in s or "," in s:
        raise ValueError(f"host path with ':' or ',' cannot be bind-mounted: {s}")
    return s


def plan_sanitize(stage, capture_dir, image: str = "hands") -> dict:
    stage_dir, cap = _host_path(stage), _host_path(capture_dir)
    argv = [
        "docker", "run", "--rm",
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", "1g", "--pids-limit", "512",
        "-e", "HOME=/tmp",
        "-e", "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright",
        "-v", f"{stage_dir}/raw:/in:ro",
        "-v", f"{stage_dir}/clean:/out:rw",
        "-v", f"{cap}:/capture:ro",
        "--entrypoint", "node",
        image,
        "/capture/sanitize-media.mjs", "--in-dir", "/in", "--out-dir", "/out",
    ]
    return {"argv": argv, "timeout_s": SANITIZE_TIMEOUT_S}


def docker_launcher(docker: str = "docker"):
    """Launcher that runs a plan_sanitize request with argv[0] replaced; returns the exit code (124 on timeout)."""
    import subprocess

    def launch(req: dict, stage) -> int:
        try:
            return subprocess.run([docker, *req["argv"][1:]], stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=req["timeout_s"]).returncode
        except subprocess.TimeoutExpired:
            return 124
    return launch


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _set_status(upload_dir: Path, status: str, reason: str = "", design_id: str = "") -> dict:
    st = {"status": status, "reason": reason, "id": design_id or upload_dir.name, "upload": upload_dir.name}
    _write_json(upload_dir / "status.json", st)
    return st


def _prune_inbox(inbox: Path, now: float) -> None:
    for d in inbox.iterdir():
        try:
            if d.is_dir() and now - (d / "status.json").stat().st_mtime > STATUS_KEEP_S:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def receive(inbox, stream, name: str, kind: str, note: str, batch: str, size: int, group: str = "",
            group_n: int = 0) -> str:
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    _prune_inbox(inbox, time.time())
    ident = f"u{int(time.time())}-{token_hex(3)}"
    d = inbox / ident
    d.mkdir()
    try:
        total = 0
        with open(d / "raw", "wb") as out:
            while True:
                buf = stream.read(CHUNK)
                if not buf:
                    break
                total += len(buf)
                if total > size:
                    break
                out.write(buf)
        if total != size:
            raise ValueError(f"upload is {total} bytes, expected {size}")
        _write_json(d / "upload.json", {"name": name, "kind": kind, "note": note or "", "batch": batch,
                                        "size": size, "received_at": _now(None),
                                        **({"group": group, "group_n": group_n} if group and group_n else {})})
        _set_status(d, "queued")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return ident


def _sanitizer_reason(stage: Path, code: int) -> str:
    try:
        errs = json.loads((stage / "clean" / "errors.json").read_text(encoding="utf-8"))
        if code == 3 and errs:
            return "image could not be decoded"
    except (OSError, ValueError):
        pass
    return "image could not be decoded" if code == 3 else f"sanitizer failed (exit {code})"


def process_upload(upload_dir, store, thumbs, capture_dir, secrets=None, caps=None, launcher=None,
                   base_index=None, image: str = "hands") -> dict:
    d = Path(upload_dir)
    stage = d / "stage"
    launcher = launcher or docker_launcher()
    result: dict
    try:
        up = json.loads((d / "upload.json").read_text(encoding="utf-8"))
        _set_status(d, "processing")
        prepare(d / "raw", up["name"], up["kind"], up.get("note", ""), stage, secrets, caps)
        if any((stage / "raw").iterdir()):
            clean = stage / "clean"
            clean.mkdir(exist_ok=True)
            clean.chmod(0o777)
            code = launcher(plan_sanitize(stage, capture_dir, image), stage)
            if code != 0:
                raise Refused(_sanitizer_reason(stage, code))
        entry = ingest(store, stage, thumbs, base_index=base_index)
        result = {**_set_status(d, "ready", design_id=entry["id"]), "design_id": entry["id"], "entry": entry}
    except Refused as exc:
        result = _set_status(d, "refused", str(exc))
    except (OSError, ValueError, KeyError) as exc:
        result = _set_status(d, "refused", f"processing failed ({type(exc).__name__})")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        (d / "raw").unlink(missing_ok=True)
    return result


# ------------------------------------------------------------------ CLI
def _emit(obj) -> None:
    print(json.dumps(obj))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.designfiles")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, *args: str, multi: tuple[str, ...] = (), flags: tuple[str, ...] = ("--json",)):
        p = sub.add_parser(name)
        for a in args:
            p.add_argument(a)
        for a in multi:
            p.add_argument(a, nargs="+")
        for f in flags:
            p.add_argument(f, action="store_true")
        return p
    cmd("guess-kind", "name", flags=())
    cmd("safe-name", "name", flags=())
    c = cmd("check", "--file", "--name", "--kind", "--secrets", "--caps")
    c.set_defaults(required=("file", "name"))
    p = cmd("prepare", "--file", "--name", "--kind", "--note", "--secrets", "--caps", "--stage")
    p.set_defaults(required=("file", "name", "kind", "stage"))
    i = cmd("ingest", "--store", "--stage", "--thumbs", "--base-index")
    i.set_defaults(required=("store", "stage"))
    cmd("remove", "--store", "--id", flags=()).set_defaults(required=("store", "id"))
    e = cmd("edit", "--store", "--id", "--kind", "--note", flags=())
    e.set_defaults(required=("store", "id"))
    g = cmd("group", "--store", "--note", multi=("--ids",))
    g.set_defaults(required=("store", "ids"))
    cmd("list", "--store").set_defaults(required=("store",))
    cmd("metadata", "--file").set_defaults(required=("file",))
    cmd("clean-png", "--in", "--out", flags=()).set_defaults(required=("in", "out"))
    cmd("plan-sanitize", "--stage", "--capture-dir", "--image").set_defaults(required=("stage", "capture_dir"))
    cmd("receive", "--inbox", "--name", "--kind", "--note", "--batch", "--size").set_defaults(
        required=("inbox", "name", "kind", "batch", "size"))
    cmd("process", "--upload", "--store", "--thumbs", "--capture-dir", "--secrets", "--docker",
        "--base-index").set_defaults(required=("upload", "store", "thumbs", "capture_dir"))
    a = ap.parse_args(argv)
    for r in getattr(a, "required", ()):
        if getattr(a, r) is None:
            ap.error(f"--{r} is required")
    as_json = getattr(a, "json", False)
    try:
        if a.cmd == "guess-kind":
            print(guess_kind(a.name))
        elif a.cmd == "safe-name":
            print(safe_name(a.name))
        elif a.cmd == "check":
            info = check(a.file, a.name, a.kind, load_secrets(a.secrets), json.loads(a.caps) if a.caps else None)
            _emit(info) if as_json else print("ok")
        elif a.cmd == "prepare":
            meta = prepare(a.file, a.name, a.kind, a.note or "", a.stage, load_secrets(a.secrets),
                           json.loads(a.caps) if a.caps else None)
            _emit(meta) if as_json else print("ok")
        elif a.cmd == "ingest":
            _emit(ingest(a.store, a.stage, a.thumbs, base_index=a.base_index))
        elif a.cmd == "remove":
            remove(a.store, a.id)
        elif a.cmd == "edit":
            edit(a.store, a.id, a.kind, a.note)
        elif a.cmd == "group":
            _emit(group(a.store, a.ids, a.note or ""))
        elif a.cmd == "list":
            idx = load_index(a.store)
            _emit(idx) if as_json else [print(f"{x['id']}\t{x['kind']}\t{x['file']}") for x in idx["files"]]
        elif a.cmd == "metadata":
            markers = metadata_markers(a.file)
            _emit({"markers": markers}) if as_json else [print(m) for m in markers]
        elif a.cmd == "clean-png":
            Path(a.out).write_bytes(clean_png(Path(getattr(a, "in")).read_bytes()))
        elif a.cmd == "plan-sanitize":
            _emit(plan_sanitize(a.stage, a.capture_dir, a.image or "hands"))
        elif a.cmd == "receive":
            _emit({"id": receive(a.inbox, sys.stdin.buffer, a.name, a.kind, a.note or "", a.batch, int(a.size))})
        elif a.cmd == "process":
            res = process_upload(a.upload, a.store, a.thumbs, a.capture_dir, load_secrets(a.secrets),
                                 launcher=docker_launcher(a.docker or "docker"), base_index=a.base_index)
            _emit({k: v for k, v in res.items() if k != "entry"})
            return 0 if res["status"] == "ready" else 1
    except Refused as exc:
        if as_json:
            print(json.dumps({"ok": False, "reason": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
