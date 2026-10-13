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
