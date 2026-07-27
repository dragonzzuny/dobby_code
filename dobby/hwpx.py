"""Read and edit HWPX documents, with the edit preserving every byte it did not change.

HWPX is a ZIP of XML: `Contents/section0.xml` holds the body, `mimetype` must be
the first entry and STORED, and the section declares fourteen namespace prefixes.
Those three facts decide the whole design.

WHY THE EDIT IS A BYTE SPLICE, NOT A RE-SERIALISATION

The obvious implementation parses the section with ElementTree, changes the text,
and writes the tree back. That rewrites the entire section: ElementTree renames
namespace prefixes it was not told about to `ns0`..`ns13` and drops declarations
it believes are unused. Whether 한글 still opens the result is a question nobody
has answered, and "probably" is not a basis for editing somebody's contest
submission.

So an edit here changes only the CHARACTER DATA it targets and leaves every other
byte of the section identical. `xml.parsers.expat` — stdlib — reports
`CurrentByteIndex`, which locates each character-data run exactly.

WHY CHARACTER DATA AND NOT THE `<hp:t>` ELEMENT

Measured across the twelve HWPX documents on the authoring machine: of 4151
`<hp:t>` elements, **65 contain CHILD ELEMENTS**, and in one real document
(a paper summary) 26 of its 48 do. Treating `<hp:t>` content as a string would
have destroyed inline markup in exactly the file its author was working on. A
regex scan that assumed pure text found 22 nodes there where the parse tree found
48 — two methods disagreeing, which is itself the finding.

Character data is a string in every case, including mixed content, so that is the
unit this module edits.

WHAT IS NOT SUPPORTED, AND WHY

- **Inserting or deleting paragraphs.** Both require rewriting structure, which
  puts the namespace problem back. Text replacement covers the form-filling case
  this was built for; new paragraphs do not.
- **`.hwp` (HWP 5.0 binary).** A different format entirely — an OLE compound
  file, not a ZIP. `dobby/hwp5.py` reads it; nothing here writes it.
- **Formatting.** A replacement inherits the run's existing formatting. Replacing
  text that spans two differently-formatted runs is refused rather than guessed,
  because silently collapsing formatting is the kind of change an author notices
  only after submitting.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import zipfile
from typing import Iterator
from xml.parsers import expat
from xml.sax.saxutils import escape, unescape

#: The paragraph namespace. Everything this module reads or edits lives in it.
HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"

#: expat is asked to join a namespace URI and a local name with a space, so
#: element names arrive as `"<uri> <local>"`.
_SEP = " "
_T = f"{HP}{_SEP}t"
_P = f"{HP}{_SEP}p"
_TBL = f"{HP}{_SEP}tbl"
_TR = f"{HP}{_SEP}tr"
_TC = f"{HP}{_SEP}tc"
_CELL_ADDR = f"{HP}{_SEP}cellAddr"

#: A fixed-width space is an ELEMENT, not a character. Dropping it silently joins
#: two words that are separated on screen.
_FW_SPACE = f"{HP}{_SEP}fwSpace"

#: `Contents/section0.xml`, `Contents/section1.xml`, ...
_SECTION_RE = re.compile(r"Contents/section(\d+)\.xml$")

#: The ZIP magic. The extension is a claim; the first two bytes are a measurement.
_ZIP_MAGIC = b"PK"

#: The OLE/CFBF magic that marks a legacy `.hwp`, so this module can name the
#: right tool instead of failing with a confusing ZIP error.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: What HWPX declares in its uncompressed first entry.
_MIMETYPE = b"application/hwp+zip"


class HwpxError(RuntimeError):
    """A precondition this module refuses to work around."""


@dataclasses.dataclass(frozen=True)
class Span:
    """One run of character data, and exactly where its bytes are.

    `start`/`end` index the RAW SECTION BYTES, which is what makes an edit a
    splice. They are invalidated by any edit to the same section, so
    `HwpxDocument` re-indexes after each write rather than letting a caller hold
    a stale offset.

    TWO FORMS OF THE SAME TEXT, deliberately.

    `raw` is what the bytes literally say: a public notice on this machine holds
    `&lt; ’26. 7. 15.(수) &gt;`. `text` is what the document says: `< ’26. 7.
    15.(수) >`. Reporting `raw` as the text is wrong for a reader, and writing a
    literal `<` back into the section is wrong for the XML — it stops parsing.

    Offsets belong to `raw`, so all splice arithmetic uses it and everything
    user-facing uses `text`.
    """

    section: str
    start: int
    end: int
    text: str
    raw: str
    paragraph: int


@dataclasses.dataclass(frozen=True)
class Paragraph:
    """A `<hp:p>`, its text, and where it sits.

    `cell` is the table address as `(row, col)` when the paragraph is inside a
    table cell, else None. The contest proposal this was built against is a form:
    52 of its elements are table cells, so "edit the document" means "edit a
    cell" and a paragraph model that could not say which cell would be useless.
    """

    index: int
    section: str
    text: str
    cell: tuple[int, int] | None
    table: int | None

    @property
    def location(self) -> str:
        if self.cell is None:
            return f"{self.section}#p{self.index}"
        return (f"{self.section}#p{self.index} "
                f"table{self.table}[r{self.cell[0]},c{self.cell[1]}]")


def detect_format(path: str) -> str:
    """`"hwpx"`, `"hwp5"`, or a refusal — decided by magic bytes.

    A file named `.hwpx` that is actually an OLE compound file is common: 한글
    saves legacy format under whatever name the user typed. Trusting the
    extension produces a `BadZipFile` three frames deep instead of a sentence
    saying which format arrived.
    """
    with open(path, "rb") as handle:
        head = handle.read(8)
    if head[:2] == _ZIP_MAGIC:
        return "hwpx"
    if head == _OLE_MAGIC:
        return "hwp5"
    raise HwpxError(
        f"{os.path.basename(path)} is neither a ZIP (HWPX) nor an OLE compound "
        f"file (HWP 5.0); its first bytes are {head!r}")


def _scan(raw: bytes, section: str) -> tuple[list[Span], list[Paragraph]]:
    """One expat pass producing both the spans and the paragraphs.

    Two passes would let the two disagree, and a paragraph index that does not
    match the span index is a defect that only shows up during an edit.
    """
    spans: list[Span] = []
    paragraphs: list[Paragraph] = []

    stack: list[str] = []
    para_index = -1
    para_parts: list[str] = []
    para_open = False
    # Tables nest: a cell contains paragraphs which may contain another table.
    #
    # A cell's ADDRESS cannot be read when its paragraphs close. Measured on the
    # contest proposal: `<hp:tc>` orders its children `subList, cellAddr, ...`,
    # so every paragraph in the cell has already been emitted by the time the
    # row and column arrive. Resolving the address at flush time therefore
    # produced `cell=None` for all 52 cells and reported the form as having no
    # tables at all. Each cell gets an id during the scan and the addresses are
    # attached afterwards, once the whole section has been seen.
    cell_stack: list[int] = []
    cell_addr: dict[int, tuple[int, int]] = {}
    cell_seq = -1
    table_stack: list[int] = []
    table_count = -1
    pending: list[int | None] = [None]
    para_cell: list[int | None] = []
    para_table: list[int | None] = []

    parser = expat.ParserCreate(namespace_separator=_SEP)

    def close_span(end: int) -> None:
        if pending[0] is None:
            return
        start = pending[0]
        pending[0] = None
        if end <= start:
            return
        literal = raw[start:end].decode("utf-8")
        text = unescape(literal)
        spans.append(Span(section, start, end, text, literal, para_index))
        para_parts.append(text)

    def start_el(name: str, attrs: dict) -> None:
        nonlocal para_index, para_open, table_count, cell_seq
        close_span(parser.CurrentByteIndex)
        if name == _TBL:
            table_count += 1
            table_stack.append(table_count)
        elif name == _TC:
            cell_seq += 1
            cell_stack.append(cell_seq)
        elif name == _CELL_ADDR and cell_stack:
            try:
                cell_addr[cell_stack[-1]] = (int(attrs.get("rowAddr", "0")),
                                             int(attrs.get("colAddr", "0")))
            except ValueError:
                pass
        elif name == _P:
            _flush_paragraph()
            para_index += 1
            para_open = True
            para_parts.clear()
        elif name == _FW_SPACE and para_open:
            # An element that renders as whitespace. It carries no character
            # data, so nothing else in this pass would record it.
            para_parts.append(" ")
        stack.append(name)

    def _flush_paragraph() -> None:
        nonlocal para_open
        if not para_open:
            return
        para_open = False
        para_cell.append(cell_stack[-1] if cell_stack else None)
        para_table.append(table_stack[-1] if table_stack else None)
        paragraphs.append(Paragraph(
            index=para_index, section=section, text="".join(para_parts),
            cell=None, table=None))

    def end_el(name: str) -> None:
        close_span(parser.CurrentByteIndex)
        if stack:
            stack.pop()
        if name == _P:
            _flush_paragraph()
        elif name == _TC and cell_stack:
            cell_stack.pop()
        elif name == _TBL and table_stack:
            table_stack.pop()

    def chars(_data: str) -> None:
        if stack and stack[-1] == _T and pending[0] is None:
            pending[0] = parser.CurrentByteIndex

    parser.StartElementHandler = start_el
    parser.EndElementHandler = end_el
    parser.CharacterDataHandler = chars
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise HwpxError(f"{section} is not well-formed XML: {exc}") from exc
    _flush_paragraph()
    # Attach the addresses now that every `cellAddr` in the section has been read.
    resolved = [
        dataclasses.replace(para,
                            cell=cell_addr.get(para_cell[i])
                            if para_cell[i] is not None else None,
                            table=para_table[i])
        for i, para in enumerate(paragraphs)]
    return spans, resolved


class HwpxDocument:
    """An open HWPX. Edits are staged in memory; `save` writes a new file."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        kind = detect_format(self.path)
        if kind != "hwpx":
            raise HwpxError(
                f"{os.path.basename(path)} is {kind}, not hwpx. HWP 5.0 is a "
                f"different format and is read by dobby/hwp5.py; nothing writes it.")
        with zipfile.ZipFile(self.path) as archive:
            self._order = [info.filename for info in archive.infolist()]
            self._compress = {info.filename: info.compress_type
                              for info in archive.infolist()}
            self._raw = {name: archive.read(name) for name in self._order}
        declared = self._raw.get("mimetype", b"").strip()
        if declared != _MIMETYPE:
            raise HwpxError(
                f"mimetype entry is {declared!r}, expected {_MIMETYPE!r}; this "
                f"ZIP is not an HWPX document")
        self.sections = sorted(
            (n for n in self._order if _SECTION_RE.match(n)),
            key=lambda n: int(_SECTION_RE.match(n).group(1)))
        if not self.sections:
            raise HwpxError("no Contents/sectionN.xml — nothing to read")
        self._edits = 0
        self._reindex()

    # -- reading ----------------------------------------------------------
    def _reindex(self) -> None:
        self._spans: list[Span] = []
        self._paragraphs: list[Paragraph] = []
        for section in self.sections:
            spans, paras = _scan(self._raw[section], section)
            offset = len(self._paragraphs)
            self._spans.extend(dataclasses.replace(
                s, paragraph=s.paragraph + offset) for s in spans)
            self._paragraphs.extend(dataclasses.replace(
                p, index=p.index + offset) for p in paras)

    @property
    def paragraphs(self) -> list[Paragraph]:
        return list(self._paragraphs)

    @property
    def spans(self) -> list[Span]:
        return list(self._spans)

    @property
    def text(self) -> str:
        """The document's text, paragraph per line.

        Assembled from the XML, never from `Preview/PrvText.txt`. That entry
        exists and looks like a shortcut; it is a PREVIEW, it is not regenerated
        on every edit, and on the authoring machine it decoded without a single
        replacement character into visible mojibake — an absence of errors that
        was not a correct decoding.
        """
        return "\n".join(p.text for p in self._paragraphs)

    def tables(self) -> list[dict]:
        """Table cells, grouped by table, in document order."""
        grouped: dict[int, dict[tuple[int, int], list[str]]] = {}
        for para in self._paragraphs:
            if para.table is None or para.cell is None:
                continue
            grouped.setdefault(para.table, {}).setdefault(para.cell, [])
            grouped[para.table][para.cell].append(para.text)
        return [{"table": index,
                 "cells": [{"row": r, "col": c, "text": "\n".join(v).strip()}
                           for (r, c), v in sorted(cells.items())]}
                for index, cells in sorted(grouped.items())]

    def find(self, needle: str) -> list[Paragraph]:
        return [p for p in self._paragraphs if needle in p.text]

    # -- editing ----------------------------------------------------------
    def replace_text(self, old: str, new: str, *, count: int | None = None
                     ) -> dict:
        """Replace `old` with `new` wherever it lies INSIDE a single span.

        A match straddling two spans is REFUSED, not stitched. Two spans are two
        runs, usually with different formatting, and joining them would silently
        move text from one style to another. The refusal is reported so the
        caller can see the match exists and was declined, rather than concluding
        the text is absent.
        """
        if not old:
            raise HwpxError("refusing to replace the empty string")
        applied: list[dict] = []
        # The search runs on the ESCAPED form, because that is what the byte
        # offsets index. Searching the unescaped text and then using its length
        # as an offset would be off by three bytes for every `&lt;` before the
        # match, and the splice would land mid-tag.
        needle = escape(old)
        payload = escape(new).encode("utf-8")
        # Group by section, and splice from the END so earlier offsets stay valid.
        by_section: dict[str, list[tuple[Span, int]]] = {}
        for span in self._spans:
            start = span.raw.find(needle)
            while start != -1:
                by_section.setdefault(span.section, []).append((span, start))
                start = span.raw.find(needle, start + 1)

        # Computed BEFORE anything is spliced. Afterwards the index still
        # describes the pre-edit document until `_reindex` runs, so a paragraph
        # that was just successfully edited would still look like it contains
        # `old` and would be reported as a straddle it never was.
        straddled = [
            {"paragraph": para.index, "location": para.location,
             "text": para.text,
             "runs": [s.text for s in self._spans if s.paragraph == para.index],
             "why": ("the match crosses two runs, which usually means two "
                     "different formats. Replacing across them would give the "
                     "whole result the first run's formatting, so it is "
                     "declined. Replace one run's substring instead, or use "
                     "set_paragraph_text if the paragraph really is one run.")}
            for para in self._paragraphs
            if old in para.text
            and not any(old in s.text for s in self._spans
                        if s.paragraph == para.index)]

        remaining = count
        for section in self.sections:
            hits = by_section.get(section, [])
            # Descending byte order: a later splice cannot move an earlier one.
            hits.sort(key=lambda pair: (pair[0].start, pair[1]), reverse=True)
            raw = self._raw[section]
            for span, offset in hits:
                if remaining is not None and remaining <= 0:
                    break
                prefix = span.raw[:offset].encode("utf-8")
                target_start = span.start + len(prefix)
                target_end = target_start + len(needle.encode("utf-8"))
                raw = raw[:target_start] + payload + raw[target_end:]
                applied.append({
                    "section": section,
                    "paragraph": span.paragraph,
                    "before": span.text,
                    "replaced": old,
                    "with": new,
                })
                if remaining is not None:
                    remaining -= 1
            self._raw[section] = raw

        if applied:
            self._edits += len(applied)
            self._reindex()
        return {"applied": applied, "replaced": len(applied),
                "straddled": straddled,
                "note": ("nothing matched" if not applied and not straddled
                         else None)}

    def set_paragraph_text(self, index: int, text: str) -> dict:
        """Replace a whole paragraph's text.

        Refused when the paragraph has more than one span: that means more than
        one run, and collapsing them would apply the first run's formatting to
        all of it. `replace_text` is the operation that preserves formatting.
        """
        matches = [p for p in self._paragraphs if p.index == index]
        if not matches:
            raise HwpxError(f"no paragraph with index {index}; the document has "
                            f"{len(self._paragraphs)}")
        para = matches[0]
        spans = [s for s in self._spans if s.paragraph == index]
        if not spans:
            raise HwpxError(
                f"paragraph {index} carries no character data, so there is "
                f"nothing to replace. It may be an empty line or hold only "
                f"inline objects; inserting text would need new markup, which "
                f"this module does not write.")
        if len(spans) > 1:
            raise HwpxError(
                f"paragraph {index} is {len(spans)} runs, probably with "
                f"different formatting. Replacing it wholesale would give all "
                f"of it the first run's style. Use replace_text for a substring "
                f"inside one run. Runs: "
                + " | ".join(repr(s.text[:24]) for s in spans))
        span = spans[0]
        raw = self._raw[span.section]
        self._raw[span.section] = (
            raw[:span.start] + escape(text).encode("utf-8") + raw[span.end:])
        self._edits += 1
        self._reindex()
        return {"paragraph": index, "location": para.location,
                "before": span.text, "after": text}

    # -- writing ----------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._edits > 0

    def save(self, path: str, *, overwrite: bool = False) -> dict:
        """Write the document, preserving entry order and compression.

        `mimetype` must be the FIRST entry and STORED — measured on real files,
        where `mimetype`, `version.xml` and `Preview/PrvImage.png` are all
        stored uncompressed. Rebuilding the archive with default settings would
        deflate `mimetype`, which is the one entry the format requires not be.

        Refuses to overwrite by default. The documents this operates on are
        somebody's submission, and an in-place write that goes wrong has no
        undo — the original is the only copy of the pre-edit state.
        """
        target = os.path.abspath(path)
        if os.path.exists(target) and not overwrite:
            raise HwpxError(
                f"{target} exists. Pass overwrite=True only if you have another "
                f"copy of it; this module cannot restore what it replaces.")
        if target == self.path and not overwrite:
            raise HwpxError("refusing to write over the source document")

        tmp = target + ".partial"
        try:
            with zipfile.ZipFile(tmp, "w") as archive:
                for name in self._order:
                    archive.writestr(
                        zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)),
                        self._raw[name],
                        compress_type=self._compress.get(name,
                                                         zipfile.ZIP_DEFLATED))
            shutil.move(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

        # Validate the OUTPUT, not the fact that writing returned.
        check = HwpxDocument(target)
        if check.text != self.text:
            raise HwpxError(
                "the file was written but reads back with different text; it "
                "has been left in place for inspection")
        return {"written": target, "entries": len(self._order),
                "edits": self._edits, "paragraphs": len(self._paragraphs),
                "verified": "reopened and text matches"}


def open_document(path: str) -> HwpxDocument:
    return HwpxDocument(path)


def iter_paragraphs(path: str) -> Iterator[Paragraph]:
    yield from HwpxDocument(path).paragraphs


def document_text(path: str) -> str:
    return HwpxDocument(path).text


def summarize(path: str) -> dict:
    """What is in this document, without dumping it."""
    doc = HwpxDocument(path)
    paras = doc.paragraphs
    non_empty = [p for p in paras if p.text.strip()]
    tables = doc.tables()
    return {
        "path": doc.path,
        "format": "hwpx",
        "sections": doc.sections,
        "entries": len(doc._order),
        "paragraphs": len(paras),
        "paragraphs_with_text": len(non_empty),
        "characters": sum(len(p.text) for p in paras),
        "tables": len(tables),
        "table_cells": sum(len(t["cells"]) for t in tables),
        "editable_spans": len(doc.spans),
        "head": [p.text for p in non_empty[:8]],
    }
