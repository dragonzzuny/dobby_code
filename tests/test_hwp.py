"""HWPX and HWP 5.0: what the readers extract, and what the writer preserves.

TWO KINDS OF TEST, AND WHY BOTH ARE HERE

The fixtures below are SYNTHETIC. They have to be: the documents this was built
against are somebody's contest submission and unpublished paper, and committing
them to a public repository to make a test suite green is not a trade anyone
should make. Synthetic fixtures prove the logic — control-character widths, entity
escaping, entry order, refusals.

They cannot prove the parser handles what 한글 actually emits. That was measured
separately, on this machine, against 12 real `.hwpx` and 12 real `.hwp` documents:
all 24 read; every HWPX was byte-identical after a no-op save; `olefile` and the
stdlib compound-file reader here agreed on every stream of every `.hwp`. The
real-document tests at the bottom re-run that check wherever such files exist and
skip where they do not — a skip is recorded as a skip, never as a pass.

THE DEFECT THE FIXTURES ENCODE

Each fixture exists because something was wrong before it. `<hp:t>` elements
carrying child elements (65 of 4151 in the real corpus, and 26 of 48 in one
document) broke a whole-element replacement. `&lt;` in the raw bytes broke offset
arithmetic that assumed unescaped text. `<hp:tc>` orders `subList` before
`cellAddr`, so cell addresses read at paragraph-close time were always None and
every table form reported zero tables.
"""

from __future__ import annotations

import glob
import os
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dobby import hwp5, hwpx  # noqa: E402

NS = ('xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" '
      'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
      'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
      'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core"')


def section(body: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
            f"<hs:sec {NS}>{body}</hs:sec>").encode("utf-8")


def para(*runs: str) -> str:
    inner = "".join(f"<hp:run charPrIDRef='0'>{r}</hp:run>" for r in runs)
    return f"<hp:p id='0' paraPrIDRef='0'>{inner}</hp:p>"


def text(value: str) -> str:
    return f"<hp:t>{value}</hp:t>"


def build_hwpx(path: str, body: str) -> str:
    """A minimal but structurally faithful HWPX.

    `mimetype` is first and STORED because the format requires it — measured on
    real documents, where `mimetype`, `version.xml` and `Preview/PrvImage.png`
    are all uncompressed.
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype", (1980, 1, 1, 0, 0, 0)),
                         b"application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(zipfile.ZipInfo("version.xml", (1980, 1, 1, 0, 0, 0)),
                         b"<?xml version='1.0'?><version/>",
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr(zipfile.ZipInfo("Contents/section0.xml",
                                         (1980, 1, 1, 0, 0, 0)),
                         section(body), compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr(zipfile.ZipInfo("settings.xml", (1980, 1, 1, 0, 0, 0)),
                         b"<?xml version='1.0'?><settings/>",
                         compress_type=zipfile.ZIP_DEFLATED)
    return path


class _Fixture(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dobby_hwp_")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def doc(self, body: str, name: str = "d.hwpx") -> hwpx.HwpxDocument:
        path = build_hwpx(os.path.join(self.dir, name), body)
        return hwpx.HwpxDocument(path)

    def out(self, name: str = "out.hwpx") -> str:
        return os.path.join(self.dir, name)


class ReadsParagraphsAndRuns(_Fixture):

    def test_text_is_assembled_from_runs_in_order(self):
        doc = self.doc(para(text("규모 경계 "), text("규제 분석")) + para(text("둘째 문단")))
        self.assertEqual([p.text for p in doc.paragraphs],
                         ["규모 경계 규제 분석", "둘째 문단"])
        self.assertEqual(doc.text, "규모 경계 규제 분석\n둘째 문단")

    def test_a_text_node_with_a_child_element_still_yields_its_text(self):
        """65 of 4151 real `<hp:t>` elements carry children.

        Whole-element replacement treated their content as a string and would
        have destroyed the inline markup. Character data is a string in every
        case, which is why that is the unit.
        """
        doc = self.doc(para(text("앞") + "<hp:t>가운데<hp:fwSpace/>뒤</hp:t>"))
        # The `<hp:fwSpace/>` between them renders as a space, which is the
        # behaviour the next test pins; what matters here is that the character
        # data on BOTH sides of the child element survives.
        self.assertEqual(doc.paragraphs[0].text, "앞가운데 뒤")
        self.assertEqual(len([s for s in doc.spans if s.paragraph == 0]), 3)

    def test_a_fixed_width_space_renders_as_a_space(self):
        doc = self.doc(para("<hp:t>가</hp:t><hp:fwSpace/><hp:t>나</hp:t>"))
        self.assertEqual(doc.paragraphs[0].text, "가 나")

    def test_entities_are_reported_as_the_characters_they_stand_for(self):
        doc = self.doc(para(text("&lt; 2026. 7. 15. &amp; 이후 &gt;")))
        self.assertEqual(doc.paragraphs[0].text, "< 2026. 7. 15. & 이후 >")
        self.assertEqual(doc.spans[0].raw, "&lt; 2026. 7. 15. &amp; 이후 &gt;")


class ReadsTableCells(_Fixture):
    """`<hp:tc>` puts `subList` BEFORE `cellAddr`.

    Resolving a cell address when its paragraphs close therefore always found
    None, and a 51-cell contest form reported zero tables — the reader worked and
    the answer was empty, which is the worst combination to debug.
    """

    def cell(self, row: int, col: int, body: str) -> str:
        return (f"<hp:tc><hp:subList>{body}</hp:subList>"
                f"<hp:cellAddr colAddr='{col}' rowAddr='{row}'/>"
                f"<hp:cellSpan colSpan='1' rowSpan='1'/></hp:tc>")

    def test_cells_carry_their_address_even_though_it_comes_last(self):
        body = ("<hp:tbl><hp:tr>"
                + self.cell(0, 0, para(text("목표")))
                + self.cell(0, 1, para(text("내용")))
                + "</hp:tr></hp:tbl>")
        doc = self.doc(body)
        cells = doc.tables()[0]["cells"]
        self.assertEqual([(c["row"], c["col"], c["text"]) for c in cells],
                         [(0, 0, "목표"), (0, 1, "내용")])

    def test_a_paragraph_reports_which_cell_it_is_in(self):
        body = "<hp:tbl><hp:tr>" + self.cell(2, 3, para(text("셀"))) + "</hp:tr></hp:tbl>"
        para_ = self.doc(body).paragraphs[0]
        self.assertEqual(para_.cell, (2, 3))
        self.assertEqual(para_.table, 0)
        self.assertIn("r2,c3", para_.location)

    def test_a_body_paragraph_has_no_cell(self):
        p = self.doc(para(text("본문")))
        self.assertIsNone(p.paragraphs[0].cell)
        self.assertIsNone(p.paragraphs[0].table)


class EditsPreserveEverythingElse(_Fixture):

    def test_a_no_op_save_is_byte_identical(self):
        doc = self.doc(para(text("변경없음")))
        target = self.out()
        doc.save(target)
        with zipfile.ZipFile(doc.path) as a, zipfile.ZipFile(target) as b:
            self.assertEqual(a.namelist(), b.namelist())
            for name in a.namelist():
                self.assertEqual(a.read(name), b.read(name), name)
                self.assertEqual(a.getinfo(name).compress_type,
                                 b.getinfo(name).compress_type, name)

    def test_mimetype_stays_first_and_stored(self):
        doc = self.doc(para(text("가")))
        doc.replace_text("가", "나")
        target = self.out()
        doc.save(target)
        with zipfile.ZipFile(target) as archive:
            first = archive.infolist()[0]
        self.assertEqual(first.filename, "mimetype")
        self.assertEqual(first.compress_type, zipfile.ZIP_STORED)

    def test_replacing_touches_only_the_matched_bytes(self):
        doc = self.doc(para(text("앞부분")) + para(text("바꿀말")) + para(text("뒷부분")))
        doc.replace_text("바꿀말", "새로운말")
        target = self.out()
        doc.save(target)
        back = hwpx.HwpxDocument(target)
        self.assertEqual([p.text for p in back.paragraphs],
                         ["앞부분", "새로운말", "뒷부분"])

    def test_written_text_is_escaped_on_disk_and_plain_when_read(self):
        doc = self.doc(para(text("자리")))
        doc.replace_text("자리", "A < B & C > D")
        target = self.out()
        doc.save(target)
        with zipfile.ZipFile(target) as archive:
            raw = archive.read("Contents/section0.xml")
        self.assertIn(b"A &lt; B &amp; C &gt; D", raw)
        self.assertNotIn(b"A < B", raw, "a literal < would end the element")
        self.assertEqual(hwpx.HwpxDocument(target).paragraphs[0].text,
                         "A < B & C > D")

    def test_a_match_inside_escaped_text_lands_on_the_right_bytes(self):
        """Offsets index the escaped form; `&lt;` is four bytes, `<` is one."""
        doc = self.doc(para(text("&lt;머리말&gt; 본문")))
        result = doc.replace_text("본문", "교체됨")
        self.assertEqual(result["replaced"], 1)
        target = self.out()
        doc.save(target)
        self.assertEqual(hwpx.HwpxDocument(target).paragraphs[0].text,
                         "<머리말> 교체됨")

    def test_count_limits_the_replacements(self):
        doc = self.doc(para(text("반복")) + para(text("반복")) + para(text("반복")))
        self.assertEqual(doc.replace_text("반복", "일회", count=1)["replaced"], 1)
        self.assertEqual([p.text for p in doc.paragraphs].count("반복"), 2)


class RefusalsSayWhy(_Fixture):

    def test_a_match_crossing_two_runs_is_reported_not_silently_skipped(self):
        """Returning zero for this reads as 'the text is not here'."""
        doc = self.doc(para(text("규모 경계 "), text("규제")))
        result = doc.replace_text("경계 규제", "새 규제")
        self.assertEqual(result["replaced"], 0)
        self.assertEqual(len(result["straddled"]), 1)
        self.assertEqual(result["straddled"][0]["runs"], ["규모 경계 ", "규제"])
        self.assertIn("two runs", result["straddled"][0]["why"])

    def test_a_genuinely_absent_string_is_distinguished_from_a_straddle(self):
        doc = self.doc(para(text("있는말")))
        result = doc.replace_text("없는말", "x")
        self.assertEqual(result["replaced"], 0)
        self.assertEqual(result["straddled"], [])
        self.assertEqual(result["note"], "nothing matched")

    def test_setting_a_multi_run_paragraph_is_refused_with_the_runs_named(self):
        doc = self.doc(para(text("굵게"), text("보통")))
        with self.assertRaises(hwpx.HwpxError) as caught:
            doc.set_paragraph_text(0, "통째로")
        self.assertIn("2 runs", str(caught.exception))
        self.assertIn("formatting", str(caught.exception))

    def test_setting_a_single_run_paragraph_works(self):
        doc = self.doc(para(text("원래말")))
        doc.set_paragraph_text(0, "바뀐말")
        self.assertEqual(doc.paragraphs[0].text, "바뀐말")

    def test_saving_over_an_existing_file_is_refused_by_default(self):
        doc = self.doc(para(text("가")))
        target = self.out()
        doc.save(target)
        with self.assertRaises(hwpx.HwpxError) as caught:
            doc.save(target)
        self.assertIn("exists", str(caught.exception))
        doc.save(target, overwrite=True)

    def test_replacing_the_empty_string_is_refused(self):
        with self.assertRaises(hwpx.HwpxError):
            self.doc(para(text("가"))).replace_text("", "x")

    def test_a_zip_that_is_not_hwpx_is_named(self):
        path = os.path.join(self.dir, "plain.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", b"application/zip")
        with self.assertRaises(hwpx.HwpxError) as caught:
            hwpx.HwpxDocument(path)
        self.assertIn("not an HWPX", str(caught.exception))


class FormatIsDetectedFromBytesNotTheName(_Fixture):

    def test_a_zip_is_hwpx_whatever_it_is_called(self):
        path = build_hwpx(os.path.join(self.dir, "misnamed.hwp"),
                          para(text("가")))
        self.assertEqual(hwpx.detect_format(path), "hwpx")

    def test_an_ole_file_is_hwp5_whatever_it_is_called(self):
        path = os.path.join(self.dir, "misnamed.hwpx")
        with open(path, "wb") as handle:
            handle.write(hwp5.OLE_MAGIC + b"\0" * 500)
        self.assertEqual(hwpx.detect_format(path), "hwp5")
        with self.assertRaises(hwpx.HwpxError) as caught:
            hwpx.HwpxDocument(path)
        self.assertIn("hwp5", str(caught.exception))

    def test_neither_is_refused_with_the_bytes_quoted(self):
        path = os.path.join(self.dir, "notes.txt")
        with open(path, "wb") as handle:
            handle.write(b"just text")
        with self.assertRaises(hwpx.HwpxError) as caught:
            hwpx.detect_format(path)
        self.assertIn("neither", str(caught.exception))

    def test_an_hwpx_handed_to_the_hwp5_reader_names_the_right_tool(self):
        path = build_hwpx(os.path.join(self.dir, "z.hwp"), para(text("가")))
        with self.assertRaises(hwp5.Hwp5Error) as caught:
            hwp5.document_text(path)
        self.assertIn("hwpx", str(caught.exception).lower())


class Hwp5RecordDecoding(unittest.TestCase):
    """The parts of HWP 5.0 that are pure functions over bytes."""

    def test_a_short_record_header_packs_tag_level_and_size(self):
        payload = b"abcd"
        header = (67 & 0x3FF) | (1 << 10) | (len(payload) << 20)
        stream = struct.pack("<I", header) + payload
        self.assertEqual(list(hwp5._records(stream)), [(67, 1, payload)])

    def test_an_oversized_record_uses_the_extended_length_word(self):
        payload = b"x" * 5000
        header = (67 & 0x3FF) | (0 << 10) | (0xFFF << 20)
        stream = struct.pack("<I", header) + struct.pack("<I", len(payload)) + payload
        self.assertEqual(list(hwp5._records(stream)), [(67, 0, payload)])

    def test_a_truncated_record_stops_instead_of_raising(self):
        header = (67 & 0x3FF) | (100 << 20)
        self.assertEqual(list(hwp5._records(struct.pack("<I", header) + b"ab")), [])

    def _units(self, *codes: int) -> bytes:
        return b"".join(struct.pack("<H", c) for c in codes)

    def test_plain_text_decodes(self):
        self.assertEqual(hwp5._para_text("안전".encode("utf-16-le")), "안전")

    def test_an_extended_control_consumes_eight_units_not_one(self):
        """Reading it as one character shifts everything after it by seven.

        The result looks like a charset problem and is a parser problem, which is
        the kind of misdiagnosis worth a test.
        """
        payload = (self._units(0x11) + self._units(0, 0, 0, 0, 0, 0, 0x11)
                   + "뒤".encode("utf-16-le"))
        self.assertEqual(hwp5._para_text(payload), "뒤")

    def test_an_inline_control_also_consumes_eight_units(self):
        payload = (self._units(9) + self._units(0, 0, 0, 0, 0, 0, 9)
                   + "탭뒤".encode("utf-16-le"))
        self.assertEqual(hwp5._para_text(payload), "탭뒤")

    def test_paragraph_and_line_breaks_become_newlines(self):
        payload = ("가".encode("utf-16-le") + self._units(13)
                   + "나".encode("utf-16-le") + self._units(10)
                   + "다".encode("utf-16-le"))
        self.assertEqual(hwp5._para_text(payload), "가\n나\n다")


# ------------------------------------------------------------------------
# Real documents, where they exist. A skip is a skip, never a pass.
# ------------------------------------------------------------------------
def _real(pattern: str, kind: str) -> list[str]:
    """Documents that MEASURE as `kind`, not merely ones named like it.

    The extension is a claim, which is the rule the rest of this file tests. A
    download folder collects counterexamples: the file that first broke this was
    a JPEG saved as `.hwp`. A corpus test that trips over one is reporting what
    is in the folder, not what the parser does, so the filter goes here rather
    than a skip going into each test.

    The cap applies after filtering — twelve real documents, not twelve
    candidates of which some are not documents at all.
    """
    roots = [os.path.expanduser("~/Downloads"), os.path.expanduser("~/Documents")]
    found: list[str] = []
    for root in roots:
        if os.path.isdir(root):
            found.extend(glob.glob(os.path.join(root, "**", pattern),
                                   recursive=True))
    measured: list[str] = []
    for path in sorted(set(found)):
        try:
            if hwpx.detect_format(path) != kind:
                continue
        except (hwpx.HwpxError, OSError):
            continue                      # not a 한글 document of any kind
        measured.append(path)
        if len(measured) == 12:
            break
    return measured


class RealDocuments(unittest.TestCase):
    """Runs only where 한글 documents are present; otherwise reports a skip."""

    def test_every_real_hwpx_reads_and_survives_a_no_op_save(self):
        files = _real("*.hwpx", "hwpx")
        if not files:
            self.skipTest("no real .hwpx documents on this machine")
        scratch = tempfile.mkdtemp(prefix="dobby_hwpx_real_")
        self.addCleanup(lambda: __import__("shutil").rmtree(scratch, True))
        for index, path in enumerate(files):
            with self.subTest(document=os.path.basename(path)):
                doc = hwpx.HwpxDocument(path)
                self.assertTrue(doc.paragraphs, "document read as empty")
                target = os.path.join(scratch, f"{index}.hwpx")
                doc.save(target)
                with zipfile.ZipFile(path) as a, zipfile.ZipFile(target) as b:
                    self.assertEqual(a.namelist(), b.namelist())
                    for name in a.namelist():
                        self.assertEqual(a.read(name), b.read(name), name)

    def test_every_real_hwp_reads(self):
        files = _real("*.hwp", "hwp5")
        if not files:
            self.skipTest("no real legacy .hwp documents on this machine")
        for path in files:
            with self.subTest(document=os.path.basename(path)):
                facts = hwp5.info(path)
                self.assertTrue(facts["version"].startswith("5."))
                if facts["encrypted"] or facts["distributed"]:
                    continue
                self.assertTrue(hwp5.document_text(path).strip(),
                                "readable document produced no text")

    def test_the_stdlib_reader_agrees_with_olefile(self):
        """A second implementation to disagree with, where one is installed."""
        try:
            import olefile
        except ImportError:
            self.skipTest("olefile is not installed; no oracle available")
        files = _real("*.hwp", "hwp5")
        if not files:
            self.skipTest("no real legacy .hwp documents on this machine")
        for path in files:
            with self.subTest(document=os.path.basename(path)):
                with open(path, "rb") as handle:
                    mine = hwp5.CompoundFile(handle.read())
                ole = olefile.OleFileIO(path)
                try:
                    theirs = {"/".join(p) for p in ole.listdir()}
                    self.assertEqual(set(mine.streams()), theirs)
                    for name in sorted(theirs):
                        with ole.openstream(name.split("/")) as handle:
                            self.assertEqual(mine.read(name), handle.read(), name)
                finally:
                    ole.close()


if __name__ == "__main__":
    unittest.main()
