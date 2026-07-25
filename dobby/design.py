"""DESIGN.md support: parse and validate a machine-readable design system.

Why a third instruction file
----------------------------
Agent instructions have separated into three files with non-overlapping jobs:
`AGENTS.md` states how to work, `SKILL.md` states how to perform one procedure,
and `DESIGN.md` states what the product should look like. Keeping design tokens
in `AGENTS.md` fails in a specific way — the operating contract gets reloaded on
every task while a token table is only needed when touching UI, so merging them
spends context budget on colour values during a database migration.

The format is YAML frontmatter (machine-readable tokens) followed by a markdown
body (human-readable intent), with the body's `##` sections in a fixed order:
Overview, Colors, Typography, Layout, Elevation & Depth, Shapes, Components,
Do's and Don'ts.

What validation is for
----------------------
A token file that an agent silently misreads is worse than no token file: the
agent produces confidently wrong styling and nothing flags it. So validation
checks the two things that cause silent misreads — malformed token values, and
tokens declared without the prose rule that says WHEN to use them. Tokens tell an
agent which values exist; the body tells it which to reach for. A palette with no
usage rules gets applied at random.

No YAML dependency beyond the one the kit already has, and no network. Parsing
frontmatter is done with the stdlib plus PyYAML, which `core/cli.py` already
requires.
"""

from __future__ import annotations

import os
import re

#: Body sections, in the order the specification prescribes. Order is checked
#: because agents read these files top-down and a misordered file leads with the
#: wrong context — component specs before the palette they reference.
EXPECTED_SECTIONS: tuple[str, ...] = (
    "Overview", "Colors", "Typography", "Layout",
    "Elevation & Depth", "Shapes", "Components", "Do's and Don'ts",
)

#: Token groups the frontmatter may declare.
TOKEN_GROUPS: tuple[str, ...] = ("colors", "typography", "spacing", "radius",
                                 "elevation", "components")

#: Typography sub-properties. Checked individually because a font size without a
#: line height is the single most common cause of an agent producing text that
#: renders at the right size with wrong rhythm.
TYPOGRAPHY_PROPS: tuple[str, ...] = ("fontFamily", "fontSize", "fontWeight",
                                     "lineHeight", "letterSpacing")

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

# --------------------------------------------------------------------------
# Aesthetic frameworks
#
# Tokens say WHAT values exist. An aesthetic says what the interface is TRYING
# to be, and it is the missing half: two products can share an identical palette
# and spacing scale and still look nothing alike, because one is dense and
# utilitarian and the other is airy and editorial. An agent given only tokens
# reproduces the values and invents the character, which is why AI-generated UI
# converges on the same generic look regardless of the token file it was handed.
#
# Each entry is a commitment with consequences, not a mood word. `density` and
# `contrast_strategy` are the two properties that actually change layout code.
# --------------------------------------------------------------------------

AESTHETICS: dict[str, dict] = {
    "utilitarian": {
        "intent": "the reader is here to make a decision, not to be impressed",
        "density": "high",
        "contrast_strategy": "semantic colour only; hue carries state",
        "signature": "tabular data, monospace values, minimal chrome",
        "avoid": "decorative gradients, hero sections, animated transitions",
    },
    "editorial": {
        "intent": "the reader is being led through an argument",
        "density": "low",
        "contrast_strategy": "typographic scale carries hierarchy",
        "signature": "generous line height, wide margins, few colours",
        "avoid": "dense tables, competing accent colours",
    },
    "minimal": {
        "intent": "nothing is present that does not earn its place",
        "density": "medium",
        "contrast_strategy": "space and weight, not colour",
        "signature": "one accent, flat surfaces, restrained radius",
        "avoid": "shadows used for emphasis, more than two type weights",
    },
    "brutalist": {
        "intent": "the structure of the system is the visual language",
        "density": "high",
        "contrast_strategy": "hard borders and raw contrast",
        "signature": "visible grid, monospace, zero radius, no shadow",
        "avoid": "soft shadows, rounded corners, muted palettes",
    },
    "glass": {
        "intent": "layers imply depth and focus",
        "density": "medium",
        "contrast_strategy": "blur and translucency separate planes",
        "signature": "backdrop blur, translucent surfaces, soft borders",
        "avoid": "small text over translucency — it fails contrast checks",
    },
    "enterprise": {
        "intent": "consistency across hundreds of screens beats local optimality",
        "density": "high",
        "contrast_strategy": "strict token adherence; no per-screen decisions",
        "signature": "predictable component slots, dense forms, clear affordances",
        "avoid": "bespoke layouts, one-off components",
    },
}

#: Layout sections an agent is repeatedly asked to produce. Naming them is what
#: lets a DESIGN.md say "our pricing table is comparison-first with 3 tiers"
#: instead of leaving the agent to invent a structure each time — which is where
#: inconsistency between screens actually originates.
LAYOUT_SECTIONS: dict[str, tuple[str, ...]] = {
    "hero": ("centred-statement", "split-media", "minimal-headline"),
    "pricing": ("tier-columns", "comparison-table", "single-plan"),
    "features": ("icon-grid", "alternating-rows", "bento"),
    "nav": ("top-bar", "sidebar", "command-palette"),
    "data_table": ("dense-rows", "grouped", "expandable-detail"),
    "form": ("single-column", "stepped", "inline-validation"),
    "empty_state": ("illustrated", "instructional", "silent"),
    "report": ("verdict-first", "evidence-table", "narrative"),
}

#: Contrast floors from WCAG. Included because a generated palette that fails
#: them is not a style choice — a design system that ships unreadable text is
#: broken in the same way a wrong colour token is broken, and neither is caught
#: by any of the structural checks above.
CONTRAST_MINIMUMS: dict[str, float] = {
    "body_text": 4.5,
    "large_text": 3.0,
    "ui_component": 3.0,
}


def _srgb_channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float | None:
    """WCAG relative luminance, or None when the value is not a plain hex."""
    text = hex_colour.strip()
    if not _HEX_RE.match(text):
        return None
    body = text[1:]
    if len(body) == 3:
        body = "".join(c * 2 for c in body)
    body = body[:6]
    r, g, b = (int(body[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g)
            + 0.0722 * _srgb_channel(b))


def contrast_ratio(a: str, b: str) -> float | None:
    """WCAG contrast ratio between two hex colours, or None if unparseable."""
    la, lb = relative_luminance(a), relative_luminance(b)
    if la is None or lb is None:
        return None
    lighter, darker = max(la, lb), min(la, lb)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def check_contrast(colors: dict) -> list[dict]:
    """Check text-on-background pairs against the WCAG floors.

    Only pairs that are actually named as text-on-surface are checked. Testing
    every colour against every other would produce a wall of irrelevant failures
    — an accent is not required to contrast with a border — and a report nobody
    reads is the same as no report.
    """
    problems: list[dict] = []
    if not isinstance(colors, dict):
        return problems
    surfaces = [k for k in colors
                if any(s in k.lower() for s in ("background", "surface"))]
    texts = [k for k in colors if "text" in k.lower()]
    for text_key in texts:
        for surface_key in surfaces:
            ratio = contrast_ratio(str(colors[text_key]),
                                   str(colors[surface_key]))
            if ratio is None:
                continue
            floor = (CONTRAST_MINIMUMS["large_text"]
                     if "muted" in text_key.lower()
                     else CONTRAST_MINIMUMS["body_text"])
            if ratio < floor:
                problems.append({
                    "severity": "error",
                    "where": f"contrast:{text_key}/{surface_key}",
                    "detail": (f"{ratio}:1 is below the {floor}:1 floor — this "
                               "pair is unreadable for a meaningful share of "
                               "users, which is a defect, not a style choice")})
    return problems


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (yaml_source, markdown_body). yaml_source is None when absent."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group(1), m.group(2)


def parse_design_md(text: str) -> dict:
    """Parse into tokens + section map, without judging either."""
    import yaml
    yaml_src, body = split_frontmatter(text)
    tokens: dict = {}
    yaml_error = None
    if yaml_src is not None:
        try:
            loaded = yaml.safe_load(yaml_src)
            tokens = loaded if isinstance(loaded, dict) else {}
            if loaded is not None and not isinstance(loaded, dict):
                yaml_error = (f"frontmatter parsed as {type(loaded).__name__}, "
                              "not a mapping")
        except yaml.YAMLError as exc:
            yaml_error = f"YAML parse error: {exc}"

    sections: dict[str, str] = {}
    order: list[str] = []
    current = None
    buf: list[str] = []
    for line in body.splitlines():
        heading = re.match(r"^##\s+(.*?)\s*$", line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = heading.group(1)
            order.append(current)
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()

    return {"tokens": tokens, "yaml_error": yaml_error,
            "sections": sections, "section_order": order,
            "has_frontmatter": yaml_src is not None}


def _check_colors(colors) -> list[dict]:
    problems = []
    if not isinstance(colors, dict):
        return [{"severity": "error", "where": "colors",
                 "detail": f"expected a mapping of role -> value, got "
                           f"{type(colors).__name__}"}]
    for role, value in colors.items():
        if isinstance(value, dict):
            problems.extend(_check_colors(value))
            continue
        text = str(value).strip()
        if _HEX_RE.match(text):
            continue
        if re.match(r"^(rgb|rgba|hsl|hsla|oklch|color-mix)\(", text):
            continue
        if text.startswith("var(") or text.startswith("{"):
            # A reference to another token. Legal, and resolving it is the
            # consumer's job; flagging it would reject valid indirection.
            continue
        problems.append({
            "severity": "error", "where": f"colors.{role}",
            "detail": f"{text!r} is not a recognizable colour value "
                      "(hex, rgb/hsl/oklch function, or a token reference)"})
    return problems


def _check_typography(typo) -> list[dict]:
    problems = []
    if not isinstance(typo, dict):
        return [{"severity": "error", "where": "typography",
                 "detail": f"expected a mapping, got {type(typo).__name__}"}]
    for style, props in typo.items():
        if not isinstance(props, dict):
            problems.append({
                "severity": "warning", "where": f"typography.{style}",
                "detail": "a bare value: an agent cannot tell whether it is a "
                          "size, a family, or a weight — use named properties"})
            continue
        if "fontSize" in props and "lineHeight" not in props:
            problems.append({
                "severity": "warning", "where": f"typography.{style}",
                "detail": "fontSize without lineHeight: text will render at the "
                          "right size with unspecified rhythm"})
        unknown = [k for k in props if k not in TYPOGRAPHY_PROPS]
        if unknown:
            problems.append({
                "severity": "info", "where": f"typography.{style}",
                "detail": f"non-standard properties {unknown} — consumers may "
                          "ignore them"})
    return problems


def _check_spacing(spacing) -> list[dict]:
    if isinstance(spacing, dict):
        scale = spacing.get("scale", spacing)
    else:
        scale = spacing
    if isinstance(scale, dict):
        values = list(scale.values())
    elif isinstance(scale, (list, tuple)):
        values = list(scale)
    else:
        return [{"severity": "error", "where": "spacing",
                 "detail": f"expected a scale array or mapping, got "
                           f"{type(spacing).__name__}"}]
    numeric = []
    for v in values:
        try:
            numeric.append(float(re.sub(r"[a-zA-Z%]+$", "", str(v))))
        except ValueError:
            return [{"severity": "error", "where": "spacing",
                     "detail": f"non-numeric scale entry {v!r}"}]
    problems = []
    if numeric and numeric != sorted(numeric):
        # An unsorted scale is the failure that produces inconsistent gaps: an
        # agent asked for "the third step" gets an arbitrary value.
        problems.append({"severity": "error", "where": "spacing",
                         "detail": f"scale is not ascending: {values}"})
    if len(set(numeric)) != len(numeric):
        problems.append({"severity": "warning", "where": "spacing",
                         "detail": "duplicate steps make the scale ambiguous"})
    return problems


def validate_design_md(path: str) -> dict:
    """Full validation with severities and a plain verdict.

    Three severities, because they demand different responses: `error` means an
    agent will misread the file, `warning` means it will guess, and `info` means a
    consumer may ignore something. Collapsing them to a pass/fail would make the
    common case (a usable file with two warnings) look like a failure.
    """
    if not os.path.exists(path):
        return {"path": path, "exists": False,
                "verdict": "no DESIGN.md: agents have no design contract and "
                           "will invent styling per file",
                "fix": "create DESIGN.md with YAML token frontmatter and the "
                       "eight prescribed sections",
                "problems": [], "errors": 0, "warnings": 0}

    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    parsed = parse_design_md(text)
    problems: list[dict] = []

    if not parsed["has_frontmatter"]:
        problems.append({
            "severity": "error", "where": "frontmatter",
            "detail": "no YAML frontmatter: the tokens are not machine-readable, "
                      "so an agent must infer values from prose"})
    if parsed["yaml_error"]:
        problems.append({"severity": "error", "where": "frontmatter",
                         "detail": parsed["yaml_error"]})

    tokens = parsed["tokens"]
    if "colors" in tokens:
        problems.extend(_check_colors(tokens["colors"]))
        problems.extend(check_contrast(tokens["colors"]))

    # An unnamed aesthetic is the gap that makes token-only files produce
    # generic output: the agent reproduces the values and invents the character.
    aesthetic = tokens.get("aesthetic")
    if aesthetic is None:
        problems.append({
            "severity": "warning", "where": "frontmatter.aesthetic",
            "detail": ("no aesthetic declared. Tokens say what values exist; an "
                       "aesthetic says what the interface is trying to BE. "
                       "Without it two products with identical tokens still "
                       f"diverge. Choose one of {sorted(AESTHETICS)} or "
                       "describe your own in the Overview section")})
    elif isinstance(aesthetic, str) and aesthetic not in AESTHETICS:
        problems.append({
            "severity": "info", "where": "frontmatter.aesthetic",
            "detail": (f"'{aesthetic}' is not a known preset "
                       f"({sorted(AESTHETICS)}); make sure the Overview section "
                       "defines its density and contrast strategy")})
    if "typography" in tokens:
        problems.extend(_check_typography(tokens["typography"]))
    if "spacing" in tokens:
        problems.extend(_check_spacing(tokens["spacing"]))

    declared = [g for g in TOKEN_GROUPS if g in tokens]
    if not declared:
        problems.append({
            "severity": "error", "where": "frontmatter",
            "detail": f"no token groups declared; expected one or more of "
                      f"{list(TOKEN_GROUPS)}"})

    sections = parsed["sections"]
    missing = [s for s in EXPECTED_SECTIONS if s not in sections]
    for s in missing:
        problems.append({"severity": "warning", "where": f"section:{s}",
                         "detail": "prescribed section missing"})

    present_order = [s for s in parsed["section_order"] if s in EXPECTED_SECTIONS]
    expected_order = [s for s in EXPECTED_SECTIONS if s in present_order]
    if present_order != expected_order:
        problems.append({
            "severity": "info", "where": "section_order",
            "detail": f"sections out of prescribed order: {present_order} "
                      f"(expected {expected_order})"})

    # The check that matters most: tokens without usage rules.
    for group in declared:
        section = {"colors": "Colors", "typography": "Typography",
                   "spacing": "Layout", "radius": "Shapes",
                   "elevation": "Elevation & Depth",
                   "components": "Components"}[group]
        body = sections.get(section, "")
        if len(body.split()) < 15:
            problems.append({
                "severity": "warning", "where": f"{group}/{section}",
                "detail": f"'{group}' tokens are declared but the '{section}' "
                          "section has almost no prose: an agent knows the "
                          "values and not when to use them, so it applies them "
                          "arbitrarily"})

    errors = sum(1 for p in problems if p["severity"] == "error")
    warnings = sum(1 for p in problems if p["severity"] == "warning")
    return {
        "path": path, "exists": True,
        "token_groups": declared,
        "token_counts": {g: len(tokens[g]) if isinstance(tokens[g], dict)
                         else len(tokens[g]) if isinstance(tokens[g], list) else 1
                         for g in declared},
        "sections_present": list(sections),
        "sections_missing": missing,
        "problems": problems,
        "errors": errors, "warnings": warnings,
        "verdict": ("valid and usable" if not errors and not warnings else
                    f"{errors} error(s), {warnings} warning(s): "
                    + ("agents will MISREAD this file" if errors else
                       "agents will guess where prose is missing")),
    }
