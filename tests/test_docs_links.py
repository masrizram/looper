"""Documentation links must resolve, or the docs quietly lie.

Three ADR links in ``docs/adr/README.md`` pointed at filenames that never
existed (``005-cost-ceiling.md`` for ``005-cost-budget.md``, and two more).
Nothing caught it because nothing checked. This does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ADR_DIR = DOCS / "adr"

#: Relative markdown links, excluding anchors, external URLs and mailto.
LINK = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:|#)([^)#\s]+)")

MARKDOWN = sorted(
    p
    for p in ROOT.rglob("*.md")
    if ".venv" not in p.parts and "workspace" not in p.parts and ".git" not in p.parts
)


def test_markdown_files_were_found():
    assert len(MARKDOWN) >= 10


@pytest.mark.parametrize("doc", MARKDOWN, ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/"))
def test_relative_links_resolve(doc: Path):
    broken = [
        target
        for target in LINK.findall(doc.read_text(encoding="utf-8"))
        if not (doc.parent / target).exists()
    ]
    assert not broken, f"{doc.relative_to(ROOT)} links to missing paths: {broken}"


def test_every_adr_is_listed_in_the_index():
    """An ADR nobody links to is an ADR nobody reads."""
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    for adr in sorted(ADR_DIR.glob("[0-9][0-9][0-9]-*.md")):
        assert adr.name in index, f"{adr.name} is missing from the ADR index"


def test_adr_numbering_has_no_gaps_or_duplicates():
    numbers = sorted(int(p.name[:3]) for p in ADR_DIR.glob("[0-9][0-9][0-9]-*.md"))
    assert numbers == list(
        range(1, len(numbers) + 1)
    ), f"ADR numbering is not contiguous: {numbers}"


def test_new_features_are_documented():
    """Each v6 feature must be reachable from the docs, not just the code."""
    configuration = (DOCS / "configuration.md").read_text(encoding="utf-8")
    assert "--resume" in configuration
    assert "notifications" in configuration
    # Exit code 6 existed in cli.py but was absent from the published table.
    assert "| `6` |" in configuration

    safeguards = (DOCS / "safeguards.md").read_text(encoding="utf-8")
    assert "wsl" in safeguards.lower()
