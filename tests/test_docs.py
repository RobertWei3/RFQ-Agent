"""Project documents required by spec 16."""

from __future__ import annotations

import re

from rfq_agent.config import PROJECT_ROOT

# Spec 16: the design-notes template sections, in order.
DESIGN_NOTES_SECTIONS = [
    "原系统概述（平台版本）",
    "为什么拆成四个 agent",
    "阶段契约的设计",
    "实体消解的难点",
    "分批与容错",
    "置信度策略的选择与评估结果",
    "平台方案 vs 自建方案的取舍",
    "如果上生产还需要什么",
]


def test_the_design_notes_template_has_the_spec_sections_in_order() -> None:
    text = (PROJECT_ROOT / "docs" / "design-notes.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## \d+\. (.+)$", text, flags=re.MULTILINE)
    assert headings == DESIGN_NOTES_SECTIONS


SECTION_NUMBER = re.compile(r"^#{2,3} (\d+(?:\.\d+)*)\.? ", flags=re.MULTILINE)
CODE_BLOCK = re.compile(r"^```(\w*)\n(.*?)^```", flags=re.MULTILINE | re.DOTALL)


def _code_without_comments(language: str, body: str) -> list[str]:
    """Code lines of a python / yaml block with `#` comments removed (translations may differ)."""
    if language not in {"python", "yaml"}:
        return []
    return [line.split("#", 1)[0].rstrip() for line in body.splitlines()]


def test_the_english_spec_follows_the_chinese_spec() -> None:
    # Spec 16: the Chinese spec is authoritative; the English one must keep its structure.
    chinese = (PROJECT_ROOT / "RFQ-SPEC.md").read_text(encoding="utf-8")
    english = (PROJECT_ROOT / "RFQ-SPEC.en.md").read_text(encoding="utf-8")
    assert SECTION_NUMBER.findall(english) == SECTION_NUMBER.findall(chinese)
    chinese_blocks = CODE_BLOCK.findall(chinese)
    english_blocks = CODE_BLOCK.findall(english)
    assert [lang for lang, _ in english_blocks] == [lang for lang, _ in chinese_blocks]
    for (lang, zh), (_, en) in zip(chinese_blocks, english_blocks, strict=True):
        assert _code_without_comments(lang, en) == _code_without_comments(lang, zh)
