import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.design import (AESTHETICS, CONTRAST_MINIMUMS, EXPECTED_SECTIONS,
                          LAYOUT_SECTIONS, check_contrast, contrast_ratio,
                          parse_design_md, relative_luminance,
                          validate_design_md)

FULL_BODY = "\n\n".join(
    f"## {s}\n\n" + ("prose " * 20) for s in EXPECTED_SECTIONS)

GOOD = f"""---
aesthetic: utilitarian
colors:
  background: "#0e1116"
  text: "#e6edf3"
  primary: "#4c8eda"
typography:
  body:
    fontFamily: "Inter"
    fontSize: "14px"
    lineHeight: "1.6"
spacing:
  scale: [4, 8, 16, 24]
---

{FULL_BODY}
"""


def write(tmpdir, text, name="DESIGN.md"):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class TestParsing(unittest.TestCase):
    def test_frontmatter_and_sections_split(self):
        parsed = parse_design_md(GOOD)
        self.assertTrue(parsed["has_frontmatter"])
        self.assertIn("colors", parsed["tokens"])
        self.assertIn("Overview", parsed["sections"])

    def test_missing_frontmatter_detected(self):
        parsed = parse_design_md("# Just prose\n\n## Colors\n\ntext")
        self.assertFalse(parsed["has_frontmatter"])

    def test_malformed_yaml_is_reported_not_swallowed(self):
        parsed = parse_design_md("---\ncolors: [unclosed\n---\n\n## Overview\n")
        self.assertIsNotNone(parsed["yaml_error"])


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_file_says_agents_will_invent_styling(self):
        out = validate_design_md(os.path.join(self.tmp.name, "nope.md"))
        self.assertFalse(out["exists"])
        self.assertIn("invent styling", out["verdict"])

    def test_good_file_validates_clean(self):
        out = validate_design_md(write(self.tmp.name, GOOD))
        self.assertEqual(out["errors"], 0, out["problems"])
        self.assertEqual(out["warnings"], 0, out["problems"])

    def test_no_frontmatter_is_an_error(self):
        out = validate_design_md(write(self.tmp.name, "# x\n\n## Overview\n\nx"))
        self.assertGreater(out["errors"], 0)

    def test_bad_hex_is_an_error(self):
        bad = GOOD.replace('"#4c8eda"', '"blueish"')
        out = validate_design_md(write(self.tmp.name, bad))
        self.assertTrue(any("not a recognizable colour" in p["detail"]
                            for p in out["problems"]))

    def test_token_reference_is_allowed(self):
        ok = GOOD.replace('"#4c8eda"', '"var(--brand)"')
        out = validate_design_md(write(self.tmp.name, ok))
        self.assertEqual(out["errors"], 0, out["problems"])

    def test_unsorted_spacing_scale_is_an_error(self):
        bad = GOOD.replace("[4, 8, 16, 24]", "[8, 4, 24, 16]")
        out = validate_design_md(write(self.tmp.name, bad))
        self.assertTrue(any("not ascending" in p["detail"]
                            for p in out["problems"]))

    def test_font_size_without_line_height_warns(self):
        bad = GOOD.replace('    lineHeight: "1.6"\n', "")
        out = validate_design_md(write(self.tmp.name, bad))
        self.assertTrue(any("lineHeight" in p["detail"]
                            for p in out["problems"]))

    def test_tokens_without_prose_is_the_primary_target(self):
        """The check that matters: values with no rule for when to use them."""
        thin = GOOD.replace(FULL_BODY, "## Colors\n\nshort")
        out = validate_design_md(write(self.tmp.name, thin))
        self.assertTrue(any("applies them arbitrarily" in p["detail"]
                            for p in out["problems"]))


class TestAesthetic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_aesthetic_warns(self):
        out = validate_design_md(write(
            self.tmp.name, GOOD.replace("aesthetic: utilitarian\n", "")))
        self.assertTrue(any(p["where"] == "frontmatter.aesthetic"
                            for p in out["problems"]))

    def test_unknown_aesthetic_is_info_not_error(self):
        out = validate_design_md(write(
            self.tmp.name, GOOD.replace("utilitarian", "vaporwave")))
        problem = next(p for p in out["problems"]
                       if p["where"] == "frontmatter.aesthetic")
        self.assertEqual(problem["severity"], "info")

    def test_every_preset_names_density_and_contrast_strategy(self):
        for name, spec in AESTHETICS.items():
            for key in ("intent", "density", "contrast_strategy", "signature",
                        "avoid"):
                self.assertIn(key, spec, f"{name} is missing {key}")

    def test_layout_sections_offer_variations(self):
        for section, variants in LAYOUT_SECTIONS.items():
            self.assertGreaterEqual(len(variants), 2,
                                    f"{section} offers no alternative")


class TestContrast(unittest.TestCase):
    def test_luminance_anchors(self):
        self.assertAlmostEqual(relative_luminance("#ffffff"), 1.0, places=4)
        self.assertAlmostEqual(relative_luminance("#000000"), 0.0, places=4)

    def test_black_on_white_is_the_maximum_ratio(self):
        self.assertEqual(contrast_ratio("#000000", "#ffffff"), 21.0)

    def test_shorthand_hex_supported(self):
        self.assertEqual(contrast_ratio("#000", "#fff"), 21.0)

    def test_unparseable_returns_none_rather_than_a_wrong_number(self):
        self.assertIsNone(contrast_ratio("var(--x)", "#ffffff"))
        self.assertIsNone(relative_luminance("rgb(0,0,0)"))

    def test_failing_pair_is_flagged_as_a_defect_not_a_style_choice(self):
        problems = check_contrast({"background": "#ffffff", "text": "#eeeeee"})
        self.assertTrue(problems)
        self.assertEqual(problems[0]["severity"], "error")
        self.assertIn("not a style choice", problems[0]["detail"])

    def test_passing_pair_is_silent(self):
        self.assertEqual(
            check_contrast({"background": "#0e1116", "text": "#e6edf3"}), [])

    def test_muted_text_held_to_the_large_text_floor(self):
        # A ratio between the two floors: fails as body, passes as muted.
        colors = {"background": "#ffffff", "textMuted": "#949494"}
        ratio = contrast_ratio(colors["textMuted"], colors["background"])
        self.assertGreater(ratio, CONTRAST_MINIMUMS["large_text"])
        self.assertLess(ratio, CONTRAST_MINIMUMS["body_text"])
        self.assertEqual(check_contrast(colors), [])

    def test_unrelated_pairs_are_not_checked(self):
        """A report nobody reads is the same as no report."""
        self.assertEqual(check_contrast({"primary": "#4c8eda",
                                         "border": "#2d333b"}), [])


class TestShippedDesignFile(unittest.TestCase):
    def test_the_repos_own_design_md_validates(self):
        out = validate_design_md(os.path.join(REPO, "DESIGN.md"))
        self.assertTrue(out["exists"])
        self.assertEqual(out["errors"], 0, out["problems"])
        self.assertEqual(out["warnings"], 0, out["problems"])


if __name__ == "__main__":
    unittest.main()
