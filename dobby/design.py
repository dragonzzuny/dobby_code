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
