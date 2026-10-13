# Phase 6 — 第三轮审查修复清单

来源：第三轮 review session，复核 `phase-6-fixes-2.md` 的 6 项修复和“可选”部分的处理，并对 P6 做收尾检查；范围为相对 `910dee1` 的全部未提交改动（含未跟踪文件）。
审查时 fake 模式 `uv run pytest`：364 passed（29.3s）；ruff、`mypy src` 无问题；`test_stdio_server_via_cli` 未复现卡住。
§17 P6 验收标准全部通过。6 项中 5 项已修好；A1 修好了第二轮指出的写法，但同一函数还漏写了一类封顶，见下面 A1。

## 使用方式
- 同前两轮：按 `/tdd` 修，每条先写在现有代码上会失败的测试；完成后打勾并注明测试名。
- 本轮没有需要用户做业务决定的事项；都不改变分数和调用次数，只改 explanation 文本、防御性校验和文档。
- 不在本清单范围：phase-6.md 中三个待决问题；`run_price_agent` 命名与分支（P7）；`phase-6-fixes-2.md`“可选”里注明不改的两条。

## A. 必须修

- [x] **A1 有封顶起作用时，没起作用的封顶从 explanation 里消失**（`_explanation` 记下起作用的封顶，其余触发的封顶一律写进 `gates not binding`；测试 `test_explanation_lists_gates_that_never_bind`，三种策略参数化）
  - 位置：`src/rfq_agent/confidence/scoring.py:229-235`（`_explanation`；`gates not binding` 只在 `scored.evidence == scored.raw` 时才写）。
  - 问题：§12.2 要求“触发的封顶写进 `signals`（`gate_*`）和 explanation”，R1 要求写“触发的封顶”。同一行有一个封顶起作用、另一个没起作用时，后者只在 `signals` 里，explanation 里没有。fake 模式实测：
    - 红档 + 黄档（MSRP 失败 + 数据不足，evidence 策略）：`evidence score 62, capped at 49 by gate_failure (weakest signals: …)`，缺 `gate_insufficient_data`。
    - 全部离群 + 数据不足：`evidence score 65, capped at 49 by gate_all_outliers (…)`，同样缺 `gate_insufficient_data`。
    - 证据分被一个封顶压住、另一个封顶在混合后也没起作用时同理（由代码推断）。
  - 修法：在末尾统一列出“触发了但始终没起作用”的封顶：凡是 `caps` 里不在 `capped at …` 和 `final capped at …` 中出现的名称，都写进 `gates not binding: …`，与是否有别的封顶起作用无关。
  - 测试：`test_confidence_strategies.py` 对“MSRP 失败 + 数据不足”行（evidence 策略）断言 explanation 含 `capped at 49 by gate_failure` 和 `gates not binding: gate_insufficient_data`；再对“证据分被红档压住、另一黄档未起作用、judge / 自评不调用”的组合各断言一次即可。

## C. 应该修（低风险）

- [x] **C1 未归类的封顶会被悄悄当成黄档；`YELLOW_GATES` 是死代码**（改为映射表 `GATE_BANDS`，`RED_GATES` 由它派生，删除 `YELLOW_GATES`；未归类的名称在 `gates()` 中抛 `KeyError`；测试 `test_an_unclassified_gate_is_an_error`。两个集合不相交由映射表本身保证，不另写测试）
  - 位置：`src/rfq_agent/confidence/evidence.py:116-135`。
  - 问题：`YELLOW_GATES` 定义了但没有任何引用（grep 只命中定义本身）。`gates()` 对不在 `RED_GATES` 里的名称一律套黄档上限，以后新增一个红档封顶却忘了加进 `RED_GATES`，它会被封到 79，还会触发自评调用，而且没有任何报错。现在 `_triggered_gates` 产生的 6 个名称都已正确归类。
  - 修法：`gates()` 按两个集合分别取上限，名称不在任何一个集合里时抛错（`ValueError`）；或者把封顶名称与档位放进一个映射（例如 `GATE_BANDS: dict[str, Literal["red", "yellow"]]`），`RED_GATES` 从它派生，删掉另一份。
  - 测试：`test_confidence_evidence.py` 断言 `RED_GATES` 与 `YELLOW_GATES` 不相交；并构造各种行，断言 `gates()` 产生的名称都在两者并集中（或直接对未知名称断言抛错）。

- [x] **C2 judge 不同意时 “held at” 的措辞在混合分已低于红档上限时不对**（只有混合分高于红档上限才写 “held at”；测试 `test_low_judge_blend_is_not_described_as_held`，`test_judge_disagreement_sends_the_row_to_red` 补断言）
  - 位置：`scoring.py:188-191`（`_add_judge`）。
  - 问题：混合分 46 时输出 `judge disagreed (score 20): 0.5 x 72 + 0.5 x 20 -> 46, held at 46 (red)`。46 本来就低于 49，并没有被“压住”。混合分超过上限时（`-> 81, held at 49 (red)`）是对的。
  - 修法：只有 `blended > red_cap` 时才写 `, held at {red_cap} (red)`；否则不写这一段（档位已是红档）。
  - 测试：judge 不同意、给分 20 的数据不足行，断言 explanation 不含 `held at`；给分 85 时断言含 `held at 49 (red)`（现有 `test_judge_disagreement_sends_the_row_to_red` 可加这条断言）。

- [x] **C3 phase-6.md 的记录不完整**（phase-6.md：R1–R8 与第一轮重跑点名 `phase-6-fixes.md`，本轮记录点名 `phase-6-fixes-3.md`；seam 标题改为“已确认”；末尾新增“待决问题”小节；“交付”补三份 review 清单）
  - 第一轮 review 的处理（R1–R8 和 “review 修复后” 一段）没有点名 `phase-6-fixes.md`，第二轮有（`phase-6-fixes-2.md`）。补上第一轮和本轮（`phase-6-fixes-3.md`）的文件名。
  - “TDD 测试接口（seam，待你确认）”标题里的“待你确认”已过时，改为已确认。
  - `phase-6-fixes.md` 说三个待决问题“列在 phase-6.md 末尾”，实际散在“零成本检查”和“真实 LLM 冒烟测试 / 观察”两处。建议在 phase-6.md 末尾加一个“待决问题”小节，集中列出这三条（judge 同意时的 concerns、judge prompt 缺业务规则、红档行是否算“行正确”），并注明留给哪个 Phase 或等用户决定。
  - “交付”一节补上三份 review 清单。

## 合并时一并做（不是缺陷，是合并步骤）
- `docs/plans/README.md` 第 14 行 P6 的状态从“进行中（分支 …）”改为与 P3–P5 相同的写法：合并提交「Merge phase-6-resolution-confidence」。可在同一行注明 review 清单 `phase-6-fixes*.md`。

## 可选（判断类，不改也可以）
处理情况：`WorksheetCheck.band` 已标为 `Band`，`worksheet_summary` 用 `get_args(Band)`。不改的：`evidence.py` 的 `msrp and msrp.quote` 不是多余的，mypy 需要它收窄 `Optional`（`_has_msrp` 不收窄类型）；其余三条（共用的“是否调用”小函数、`oe_status` / `msrp_status` 的 Literal 别名、config 的 key 类型）只影响写法，留到 P7 / P8 碰到这些代码时再整理。
- `scoring.py:46-58` 的 `expected_llm_calls` 与 `_score_row:87-101` 各算一遍 `gates → apply_gates(evidence_score(…)) → wants_*`；可抽成一个共用的小函数，两处只调用它。
- `schemas.py:219`、`:235` 的 `oe_status`、`msrp_status` 又写了一遍 Literal，可像 `MatchType` 一样抽出别名。
- `eval/quick_compare.py` 的 `WorksheetCheck.band` 可标为 `Band`，`worksheet_summary` 可用 `get_args(Band)`。
- `config.py` 的 `oe_match` / `spec_source` 已由 validator 校验 key，类型可直接写成 `dict[MatchType, float]` / `dict[SpecSource, float]`。
- `evidence.py:87` 的 `msrp and msrp.quote and _has_msrp(msrp)` 中前两个条件多余。

## 完成标准
- A 组修完，C 组完成或注明不做的理由；fake 模式 `uv run pytest` 全部通过；ruff、`mypy src` 无问题。
- 分数、档位、信号与 `worksheet_20261008T033130Z` 相同（零成本重跑 `evidence` 核对即可），只有 explanation 文本变化；不需要重跑真实 LLM。

## 完成记录（2026-10-08）
- fake 模式 `uv run pytest`：369 passed；ruff、`mypy src` 无问题。
- 零成本重跑 `evidence`（`worksheet_20261008T034210Z`）：与 `worksheet_20261008T033130Z` 相比，`WorksheetRow` 各列、信号、explanation 全部相同。这 20 行里没有“一个封顶起作用、另一个不起作用”的行，所以 explanation 也没变。未重跑真实 LLM。
- “合并时一并做”的 README 一行留到合并步骤处理。
