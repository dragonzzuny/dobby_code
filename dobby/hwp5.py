"""Read text out of a legacy `.hwp` (HWP 5.0), using nothing but the standard library.

WHY NOT `olefile`

`olefile` is on the authoring machine and would save two hundred lines. It is not
on every machine dobby installs onto, and this harness has no dependency file at
all — the installer's only third-party mention is an optional PyYAML warning.
Adding a hard import would mean an install that copies cleanly and then fails on
the first document, which is worse than either working or refusing up front.

So the compound-file layer is implemented here. Where `olefile` IS importable,
`tests/test_hwp5.py` uses it as an ORACLE: two independent readers must return
the same stream bytes. That is the point of writing it out — a second method to
disagree with, rather than a single implementation trusted because it is the only
one.

WHAT IT DOES AND DOES NOT DO

Reads text. Does not write: HWP 5.0 stores the body as compressed record streams
inside a compound file whose sector allocation would have to be rebuilt, and a
half-correct writer would corrupt documents in ways that only appear when 한글
opens them. `dobby/hwpx.py` edits HWPX; the conversion path for a `.hwp` is 한글's
own "다른 이름으로 저장 → HWPX".

Refuses, with the reason named, on:
  - password-protected documents (the body is encrypted; there is nothing to read)
  - 배포용 문서 (distribution/DRM documents, likewise)
"""

from __future__ import annotations

import dataclasses
import os
import struct
import zlib

#: The compound-file signature. A `.hwp` that does not start with this is not
#: HWP 5.0 — most often it is an HWPX (a ZIP) saved under the wrong extension.
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: What `FileHeader` must say for this to be an HWP document at all.
HWP_SIGNATURE = b"HWP Document File"

#: Sector chain terminators in the FAT.
_FREE, _END_OF_CHAIN, _FAT_SECTOR, _DIFAT_SECTOR = (
    0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC)

#: Record tags. `HWPTAG_BEGIN` is 0x10; paragraph text is the 51st tag after it.
_TAG_PARA_TEXT = 0x10 + 51

#: Control characters inside PARA_TEXT that occupy EIGHT UTF-16 units, not one.
#: Treating them as ordinary characters shifts every following character by seven
#: positions and turns the rest of the paragraph into noise — the failure looks
#: like a broken encoding rather than a broken parser.
_EXTENDED_CONTROLS = frozenset({1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23})
_INLINE_CONTROLS = frozenset({4, 5, 6, 7, 8, 9, 19, 20})

#: Single-unit controls, and what they mean for the text stream.
_LINE_BREAK, _PARA_BREAK = 10, 13


class Hwp5Error(RuntimeError):
    """A precondition this module refuses to work around."""


# ---------------------------------------------------------------- CFBF ----
@dataclasses.dataclass(frozen=True)
class _Entry:
    name: str
    kind: int          # 1 storage, 2 stream, 5 root
    start: int
    size: int
    left: int
    right: int
    child: int


class CompoundFile:
    """The minimum of MS-CFB needed to pull named streams out of a `.hwp`."""

    def __init__(self, data: bytes):
        if data[:8] != OLE_MAGIC:
            raise Hwp5Error("not an OLE compound file")
        self._data = data
        (sector_shift, mini_shift) = struct.unpack_from("<HH", data, 30)
        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_shift
        (self._fat_count, self._dir_start, _tx, self.mini_cutoff,
         self._minifat_start, self._minifat_count,
         self._difat_start, self._difat_count) = struct.unpack_from(
            "<I I I I I I I I", data, 44)
        self._fat = self._read_fat()
        self._dir = self._read_directory()
        self._mini_fat = self._read_mini_fat()
        root = self._dir[0]
        self._mini_stream = (self._read_chain(root.start, root.size)
                             if root.size else b"")

    # -- sectors ------------------------------------------------------
    def _sector(self, index: int) -> bytes:
        start = 512 + index * self.sector_size
        chunk = self._data[start:start + self.sector_size]
        if len(chunk) != self.sector_size:
            raise Hwp5Error(f"sector {index} runs past the end of the file")
        return chunk

    def _read_fat(self) -> list[int]:
        sectors = list(struct.unpack_from("<109I", self._data, 76))
        # DIFAT continuation, for files large enough to need it.
        nxt, seen = self._difat_start, 0
        while nxt not in (_END_OF_CHAIN, _FREE) and seen < self._difat_count:
            block = self._sector(nxt)
            per = self.sector_size // 4 - 1
            sectors.extend(struct.unpack_from(f"<{per}I", block, 0))
            nxt = struct.unpack_from("<I", block, self.sector_size - 4)[0]
            seen += 1
        fat: list[int] = []
        for index in sectors[:self._fat_count]:
            if index in (_FREE, _END_OF_CHAIN):
                continue
            fat.extend(struct.unpack_from(
                f"<{self.sector_size // 4}I", self._sector(index), 0))
        return fat

    def _chain(self, start: int) -> list[int]:
        out, cursor, guard = [], start, 0
        limit = len(self._fat) + 1
        while cursor not in (_END_OF_CHAIN, _FREE) and guard < limit:
            out.append(cursor)
            if cursor >= len(self._fat):
                break
            cursor = self._fat[cursor]
            guard += 1
        return out

    def _read_chain(self, start: int, size: int) -> bytes:
        raw = b"".join(self._sector(i) for i in self._chain(start))
        return raw[:size] if size else raw

    def _read_mini_fat(self) -> list[int]:
        if not self._minifat_count:
            return []
        raw = b"".join(self._sector(i) for i in self._chain(self._minifat_start))
        return list(struct.unpack_from(f"<{len(raw) // 4}I", raw, 0))

    def _read_mini_chain(self, start: int, size: int) -> bytes:
        out, cursor, guard = [], start, 0
        while cursor not in (_END_OF_CHAIN, _FREE) and guard <= len(self._mini_fat):
            offset = cursor * self.mini_sector_size
            out.append(self._mini_stream[offset:offset + self.mini_sector_size])
            if cursor >= len(self._mini_fat):
                break
            cursor = self._mini_fat[cursor]
            guard += 1
        return b"".join(out)[:size]

    # -- directory ----------------------------------------------------
    def _read_directory(self) -> list[_Entry]:
        raw = b"".join(self._sector(i) for i in self._chain(self._dir_start))
        entries = []
        for offset in range(0, len(raw), 128):
            block = raw[offset:offset + 128]
            if len(block) < 128:
                break
            name_len = struct.unpack_from("<H", block, 64)[0]
            name = (block[:max(0, name_len - 2)].decode("utf-16-le", "replace")
                    if name_len else "")
            kind = block[66]
            left, right, child = struct.unpack_from("<III", block, 68)
            start, size_low, size_high = struct.unpack_from("<III", block, 116)
            entries.append(_Entry(name, kind, start,
                                  size_low | (size_high << 32),
                                  left, right, child))
        return entries

    def _walk(self, index: int, prefix: str, out: dict) -> None:
        if index in (_FREE, 0xFFFFFFFF) or index >= len(self._dir):
            return
        entry = self._dir[index]
        self._walk(entry.left, prefix, out)
        path = f"{prefix}/{entry.name}" if prefix else entry.name
        if entry.kind == 2:
            out[path] = entry
        elif entry.kind == 1:
            self._walk(entry.child, path, out)
        self._walk(entry.right, prefix, out)

    def streams(self) -> dict[str, _Entry]:
        out: dict[str, _Entry] = {}
        root = self._dir[0]
        self._walk(root.child, "", out)
        return out

    def read(self, path: str) -> bytes:
        entry = self.streams().get(path)
        if entry is None:
            raise Hwp5Error(f"no stream named {path!r}")
        if entry.size < self.mini_cutoff:
            return self._read_mini_chain(entry.start, entry.size)
        return self._read_chain(entry.start, entry.size)


# ----------------------------------------------------------------- HWP ----
@dataclasses.dataclass(frozen=True)
class Hwp5Info:
    version: str
    compressed: bool
    encrypted: bool
    distributed: bool
    sections: int


def _read_header(cfb: CompoundFile) -> Hwp5Info:
    header = cfb.read("FileHeader")
    if not header.startswith(HWP_SIGNATURE):
        raise Hwp5Error(
            f"FileHeader does not start with {HWP_SIGNATURE!r}; this compound "
            f"file is not an HWP 5.0 document")
    major, minor, build, revision = header[35], header[34], header[33], header[32]
    flags = struct.unpack_from("<I", header, 36)[0]
    sections = sum(1 for name in cfb.streams()
                   if name.startswith("BodyText/Section"))
    return Hwp5Info(version=f"{major}.{minor}.{build}.{revision}",
                    compressed=bool(flags & 0x01),
                    encrypted=bool(flags & 0x02),
                    distributed=bool(flags & 0x04),
                    sections=sections)


def _records(stream: bytes):
    """Yield `(tag, level, payload)` from a HWP record stream."""
    cursor, size = 0, len(stream)
    while cursor + 4 <= size:
        header = struct.unpack_from("<I", stream, cursor)[0]
        cursor += 4
        tag = header & 0x3FF
        level = (header >> 10) & 0x3FF
        length = (header >> 20) & 0xFFF
        if length == 0xFFF:
            if cursor + 4 > size:
                break
            length = struct.unpack_from("<I", stream, cursor)[0]
            cursor += 4
        payload = stream[cursor:cursor + length]
        if len(payload) < length:
            break
        cursor += length
        yield tag, level, payload


def _para_text(payload: bytes) -> str:
    """Decode one PARA_TEXT record.

    The units are UTF-16LE, but a control character is not always one unit. The
    extended and inline controls occupy EIGHT, and reading them as single
    characters shifts everything after by seven — which looks like a charset bug
    and is not one.
    """
    out: list[str] = []
    units = len(payload) // 2
    i = 0
    while i < units:
        code = struct.unpack_from("<H", payload, i * 2)[0]
        if code in _EXTENDED_CONTROLS or code in _INLINE_CONTROLS:
            i += 8
            continue
        if code in (_LINE_BREAK, _PARA_BREAK):
            out.append("\n")
            i += 1
            continue
        if code < 32:
            i += 1
            continue
        out.append(chr(code))
        i += 1
    return "".join(out)


def _section_text(raw: bytes, compressed: bool) -> str:
    if compressed:
        try:
            raw = zlib.decompress(raw, -15)
        except zlib.error as exc:
            raise Hwp5Error(
                f"a body section did not inflate ({exc}). The FileHeader says "
                f"the document is compressed; if it is also 배포용 or password "
                f"protected the body is encrypted and cannot be read here.")
    parts = [_para_text(payload) for tag, _lvl, payload in _records(raw)
             if tag == _TAG_PARA_TEXT]
    return "".join(parts)


def info(path: str) -> dict:
    """Header facts, without decoding the body."""
    with open(path, "rb") as handle:
        data = handle.read()
    cfb = CompoundFile(data)
    head = _read_header(cfb)
    return {"path": os.path.abspath(path), "format": "hwp5",
            "version": head.version, "compressed": head.compressed,
            "encrypted": head.encrypted, "distributed": head.distributed,
            "sections": head.sections,
            "streams": sorted(cfb.streams())[:24]}


def document_text(path: str) -> str:
    """The document's text, or a refusal that says which precondition failed."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != OLE_MAGIC:
        if data[:2] == b"PK":
            raise Hwp5Error(
                f"{os.path.basename(path)} is a ZIP, i.e. HWPX saved under a "
                f".hwp name. Read it with dobby/hwpx.py, which can also edit it.")
        raise Hwp5Error(f"{os.path.basename(path)} is not an OLE compound file")
    cfb = CompoundFile(data)
    head = _read_header(cfb)
    if head.encrypted:
        raise Hwp5Error("the document is password protected; its body is "
                        "encrypted and no amount of parsing will read it")
    if head.distributed:
        raise Hwp5Error("this is a 배포용 (distribution) document; the body is "
                        "encrypted and cannot be read here")
    names = sorted(n for n in cfb.streams() if n.startswith("BodyText/Section"))
    if not names:
        raise Hwp5Error("no BodyText/SectionN stream — nothing to read")
    return "\n".join(_section_text(cfb.read(n), head.compressed) for n in names)


def summarize(path: str) -> dict:
    """What is in this document. Read-only by construction."""
    facts = info(path)
    try:
        text = document_text(path)
    except Hwp5Error as exc:
        facts.update(readable=False, why=str(exc))
        return facts
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    facts.update(readable=True, characters=len(text), lines=len(lines),
                 head=lines[:8],
                 editable=False,
                 editable_why=("HWP 5.0 writing is not implemented: the body is "
                               "compressed records inside a compound file whose "
                               "sector allocation would have to be rebuilt. Save "
                               "as HWPX in 한글 and edit that."))
    return facts
