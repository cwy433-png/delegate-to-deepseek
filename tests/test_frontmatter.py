"""Guard the YAML frontmatter every harness parses before it reads anything else.

A description is the only text a harness sees while deciding whether to invoke
this skill, so a frontmatter that fails to parse costs the whole skill. The
failure is quiet: a plain scalar containing a colon-and-space reads as a nested
mapping, so the value is rejected or truncated with nothing in the file looking
wrong. Every check here exists because the repository shipped that bug once.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Every file a harness parses as frontmatter. Adding a harness means adding its
# file here, not discovering *.md — an unlisted file is the failure this guards.
FRONTMATTER_FILES = (
    "SKILL.md",
    ".claude/skills/delegate-to-deepseek/SKILL.md",
    ".codebuddy/agents/deepseek.md",
)
BLOCK_SCALAR_INDICATORS = (">-", ">", "|-", "|", ">+", "|+")


def split_frontmatter(text: str) -> str:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if match is None:
        raise AssertionError("file does not begin with a --- frontmatter block")
    return match.group(1)


def parse_frontmatter(block: str) -> dict[str, str]:
    """Parse the frontmatter subset this repository uses.

    Handles plain scalars and folded/literal block scalars. PyYAML would be the
    obvious tool, but the repository ships with no third-party dependencies and
    the installers run wherever Python does; keeping the tests on the standard
    library preserves that.
    """
    fields: dict[str, str] = {}
    lines = block.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith(" "):
            raise AssertionError(f"unexpected indented line outside a block scalar: {line!r}")
        key, _, value = line.partition(":")
        value = value.strip()
        if value in BLOCK_SCALAR_INDICATORS:
            folded: list[str] = []
            while index < len(lines) and (not lines[index].strip() or lines[index].startswith("  ")):
                folded.append(lines[index].strip())
                index += 1
            fields[key.strip()] = " ".join(part for part in folded if part)
        else:
            fields[key.strip()] = value
    return fields


class FrontmatterParseTests(unittest.TestCase):
    def test_every_frontmatter_file_exists(self) -> None:
        for name in FRONTMATTER_FILES:
            self.assertTrue((ROOT / name).is_file(), f"missing: {name}")

    def test_plain_scalars_never_contain_a_colon_and_space(self) -> None:
        # The exact bug this file exists for. Note `model: custom-local:slug` is
        # legal and must not trip: YAML only splits on a colon *followed by a
        # space*, so a colon inside a token is fine.
        for name in FRONTMATTER_FILES:
            block = split_frontmatter((ROOT / name).read_text(encoding="utf-8"))
            for line in block.split("\n"):
                if not line or line.startswith((" ", "#")):
                    continue
                _, _, value = line.partition(":")
                value = value.strip()
                if value in BLOCK_SCALAR_INDICATORS:
                    continue
                if value[:1] in ("'", '"'):
                    continue
                self.assertNotIn(
                    ": ",
                    value,
                    f"{name}: plain scalar needs a block scalar or quotes: {line!r}",
                )

    def test_descriptions_survive_parsing_intact(self) -> None:
        # A truncated description still parses; it just stops early. Anchor on
        # the closing sentence so a silent cut is caught too.
        for name in FRONTMATTER_FILES:
            fields = parse_frontmatter(split_frontmatter((ROOT / name).read_text(encoding="utf-8")))
            description = fields.get("description", "")
            self.assertGreater(len(description), 200, f"{name}: description missing or truncated")
            self.assertTrue(
                description.rstrip().endswith("in-plan Claude tokens do not."),
                f"{name}: description does not end with its final sentence",
            )

    def test_descriptions_carry_both_routing_directions(self) -> None:
        # A description that only says when to route here is the circular
        # phrasing this repository replaced; the negative case has to survive.
        for name in FRONTMATTER_FILES:
            fields = parse_frontmatter(split_frontmatter((ROOT / name).read_text(encoding="utf-8")))
            description = fields["description"]
            self.assertIn("environment supplies the answer", description, name)
            self.assertIn("Not for work", description, name)

    def test_the_workbuddy_agent_keeps_its_required_fields(self) -> None:
        fields = parse_frontmatter(
            split_frontmatter((ROOT / ".codebuddy/agents/deepseek.md").read_text(encoding="utf-8"))
        )
        self.assertEqual(fields["name"], "deepseek")
        # Colon inside the value, no space after it — legal plain scalar.
        self.assertEqual(fields["model"], "custom-local:deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
