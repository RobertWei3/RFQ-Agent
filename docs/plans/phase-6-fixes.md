# Phase 6 — 审查后修复清单

来源：review session（2026-10-07），审查范围为相对 `910dee1` 的全部未提交改动（含未跟踪文件）。
审查时 fake 模式 `uv run pytest`：326 passed；`test_stdio_server_via_cli` 未复现卡住。
§17 P6 验收标准全部通过；下面是合并前要处理的事项。

## 使用方式
- 按 CLAUDE.md 的实施流程：先处理 B 组（逐条问用户，一次一个问题，决定写回 RFQ-SPEC.md §12.1 / §12.2 和 phase-6.md 决策表），再按 `/tdd` 修 A、C 组，每条先写失败的测试。
- 每条完成后在本文件对应行打勾，注明提交或测试名。
- 不在本清单范围：phase-6.md 末尾已列的三个待决问题（judge 同意时的 concerns、judge prompt 缺业务规则、红档行是否算“行正确”）；`run_price_agent` 命名与 should_cost 分支（留给 P7）。

## A. 必须修（与已定规则不一致）

- [x] **A1 自评漏调用证据分 < 50 但没有封顶的行**（`evidence.wants_self_check` / `wants_judge`，`scoring.expected_llm_calls` 供 CLI 共用；测试 `test_self_also_runs_on_low_rows_without_a_red_gate`、`test_expected_calls_match_the_calls_made`）
  - 位置：`src/rfq_agent/confidence/scoring.py:77`；同一条件在 `src/rfq_agent/cli/worksheet.py:154`（预计调用数）。
  - 问题：代码条件是 `gated >= yellow_min`。§12.2 / Q9 是“只对没有封顶到红档的行调用”，Q15 是“未封顶行 × 2”。证据分本身低于 50、没有任何红档封顶的行被跳过，预计调用数也因此偏少。
  - 复现（fake）：OE resolved + `chain_truncated=True`，MSRP not_found，零售无报价，`spec_source=conflict` → 证据分 39，`gates()` 为空，`evidence_plus_self` 下没有 `llm_signal`。
  - 修法：判断条件改为“`gates()` 里没有值等于红档上限（`yellow_min − 1`）的封顶”。把“这一行会不会调用自评 / judge”抽成一个函数，`scoring.py` 和 CLI 的 `_expected_calls` 共用（同时解决 C5 的重复）。
  - 测试：`test_confidence_strategies.py` 加用例——上述行在 `evidence_plus_self` 下调用 2 次、最终分按混合公式计算；`test_worksheet_cli.py` 或单测断言该行计入预计调用数。

## B. 需要用户决定（先问，定了再改并写回 spec）

- [x] **B1 混合分用封顶前还是封顶后的证据分**（`test_judge_blends_with_the_gated_evidence_score`、`test_self_blends_with_the_gated_evidence_score`）
  - 位置：`scoring.py:123`（自评）、`scoring.py:158`（judge）。
  - 现状：选 judge 行用封顶后的分数，混合用封顶前的 `scored.evidence`，最后再封顶。
  - 差异示例：证据分 90、被封到 79，judge 给 70 → 现在 80 再封到 79；用封顶后的分数则是 75。
  - 推荐：用封顶后的分数（与“judge 只看封顶后落在 judge_band 的行”一致，分数更好解释）。决定后写进 §12.2 和 phase-6.md。
  - **用户决定（2026-10-08）：A**——自评和 judge 的混合都用封顶后的证据分，混合后仍再封顶。

- [x] **B2 一条零售报价都没有时是否封顶到红档**（代码不变；spec §12.2 与 Q6 措辞已改）
  - 位置：`src/rfq_agent/confidence/evidence.py:131`（`retail.quotes and retail.selected is None`）。
  - 现状：只有“有报价且全部离群”才封顶；零报价不封顶。spec 原文是“零售报价全部离群”，但 Q6 括号写“（无参考零售价）”，两种读法覆盖范围不同。
  - 推荐：保持现状（零报价已经由 `price_sources`、`msrp_retail` 信号压低分数，且需复核原因写了 “no retail quotes found”），把 Q6 括号改成准确的说法。
  - **用户决定（2026-10-08）：A**——保持现状，只有“有报价且全部离群”才封顶；Q6 括号改为“（有报价但全部离群）”。

- [x] **B3 一对多 / OE not_found 的行 `msrp_status` 是否留空**（`test_unpriced_rows_show_no_msrp_status`）
  - 位置：`src/rfq_agent/resolution/merge.py:180`。
  - 现状：OE 为 `multiple` 时 `msrp_status` 显示上游给出的 `"not_found"`，看起来像“查了没有”，实际是“没查”。§12.1 写“价格列为空”。
  - 推荐：OE 不是 `resolved` 时 `msrp_status` 为空；`ResolvedRow.msrp` 仍保留原始结果。
  - **用户决定（2026-10-08）：A**——OE 不是 `resolved` 时工作表 `msrp_status` 为空；`ResolvedRow.msrp` 保留原始结果。

- [x] **B4 `oe_candidates` 是否包含 `llm_inferred` 候选**（`test_inferred_candidates_stay_off_the_worksheet`）
  - 位置：`merge.py:199`；来源 `src/rfq_agent/agents/oe_interchange.py:~477`（`llm_inferred` 候选被追加到 `candidates`）。
  - 问题：`llm_inferred` 候选没有走替代链，不是 §12.1 说的“各候选的当前号”。
  - 推荐：`oe_candidates` 只列 grounded 候选（替代链链尾），`llm_inferred` 只留在 `ResolvedRow.oe.candidates` 里。
  - **用户决定（2026-10-08）：A**——工作表只列 grounded 候选，`llm_inferred` 只留在 `ResolvedRow.oe.candidates`。

## C. 应该修（低风险，不需要业务决定）

- [x] **C1 CLI 未用 Q15 指定的 `announce_and_guard`**：`cli/worksheet.py:130-139` 自写 `_announce` + `guard_real`，打印少了模型名。改用 `src/rfq_agent/cli/runs.py:31` 的 `announce_and_guard`；如果它不适用，就在 phase-6.md “实施中追加的决定”记一条原因。（LLM 策略改用 `announce_and_guard`，打印 provider 与 model；`evidence` 策略不调用 LLM、没有对应角色，仍单独打印且不做真实成本拦截，已记 phase-6.md R7）
- [x] **C2 自评 fake 与 Q11 的写法不一致**：`confidence/fake_handlers.py` 按替代链和选价规则重新推出答案，而不是“照抄系统结论”（`self_brief` 里本来就不含结论）。二选一：改 Q11 措辞以符合现状（推荐，现状更接近真实 LLM），或让 fake 读系统结论。不得读取 ground truth。（改 Q11 措辞，fake 不变，见 phase-6.md R6）
- [x] **C3 prompt 中两个术语都写成 “match”**：`confidence/brief.py:44` 改为 `match type {…}`，`brief.py:56` 改为 `match quality {…}`（GLOSSARY：匹配类型 vs 匹配质量）。同步更新 `fake_handlers.py` 中的 `_QUOTE` 正则。（`brief.py` 改为 `match type` / `match quality`，fake 正则同步）
- [x] **C4 `or` 依赖 row_id 从 1 开始**：`merge.py:66-67`、`merge.py:297-298` 的 `share.msrp_from or rid` 改为 `is not None` 判断。（`_or_self`；`test_row_zero_can_be_the_representative`）
- [x] **C5 重复的条件与常量**：（共用触发函数；角色取自 `llm_checks`；信号名用 `get_args(SignalName)`；阈值移到 `confidence.evidence.msrp_retail_suspicious_ratio`，§18 同步；`test_suspicious_ratio_comes_from_config`）
  - 触发条件重复：见 A1，抽成共用函数。
  - `cli/worksheet.py:23` 的 `STRATEGY_ROLE` 改为引用 `llm_checks.SELF_ROLE` / `JUDGE_ROLE`。
  - `scoring.py:219` 的 `_EVIDENCE_SIGNALS` 改为 `get_args(SignalName)`。
  - `evidence.py:19` 的阈值 1.2 写死，而配置 key `up_to_1_2x` 也隐含 1.2：把阈值放进 `EvidenceSettings`（如 `msrp_retail_suspicious_ratio: 1.2`），同步 §18 示例。
- [x] **C6 配置 key 拼错会悄悄记 0 分**：`config.py:184-200` 的 `EvidenceSettings.oe_match` / `spec_source` 为 `dict[str, float]`，`evidence.py:50,104` 用 `.get(..., 0.0)`。改为 Literal key，或在 validator 中要求 key 集合与 `match_type` / `spec_source` 的 Literal 完全一致。（validator 要求 key 与 `MatchType` / `SpecSource` 一致，`schemas.MatchType` 新增；`test_evidence_levels_must_name_every_known_value`）
- [x] **C7 类型偏宽**：`cli/worksheet.py:110` 的 `types: dict[str, Any]` 收紧（例如逐路显式调用 `read_outcomes`）。（逐路显式 `read_outcomes`，`--limit` 过滤用 Protocol 约束的 TypeVar）

## D. 补测试（按 seam）

`resolve_rows`（`tests/test_resolution.py`）：
- [x] 两路代表行不同时：`price_source_row` 取 MSRP 的代表行；`reasoning_summary["shared"]` 为 “MSRP taken from row X, retail taken from row Y”（I3）。
- [x] 组内不一致的单项分支：只有零售 selected 站点不同；只有 MSRP 状态不同；组内一行失败（不算不一致）；一行没有参考零售价、另一行有（算不一致）。
- [x] 旧 JSONL：不含 `chain_truncated`、`schema_version` 为 1.1 的 `OEResolution` 记录能读入且为 False（可放 `tests/test_schemas.py` 或 runner 测试）。

evidence 纯函数（`tests/test_confidence_evidence.py`）：
- [x] `msrp_retail` 在 r 正好等于 1.2 时记 0.5。
- [x] `retail_dispersion` 在 cv ≥ 阈值时为 0（不出现负数）。
- [x] MSRP 为 `discontinued` 且有价时计入 `price_sources`。

`score_rows`（`tests/test_confidence_strategies.py`）：
- [x] A1 的用例。
- [x] I5：只有 1 次采样成功时 consistency 为 1，explanation 含 “only 1 self-assessment sample(s) succeeded”。
- [x] judge_band 边界：封顶后正好 50、正好 79 的行调用 judge，49、80 的行不调用。
- [x] 证据分高、被黄档封顶到 79 的行会进入 judge（结果随 B1 的决定）。

CLI（`tests/test_worksheet_cli.py`）：
- [x] `--limit N`：只输出前 N 行。
- [x] 真实 provider 且没有 `--allow-real` 时拒绝运行，不调用 LLM。
- [x] `evidence_plus_self` / `evidence_plus_judge` 打印的预计调用数与实际 fake 调用数一致。

## 完成标准
- A 组修完；B 组每条都有用户决定，并写回 RFQ-SPEC.md 与 phase-6.md；C、D 组完成或在本文件注明不做的理由。
- fake 模式 `uv run pytest` 全部通过。
- 如果 A1 / B1 改变了已有分数，在 phase-6.md 注明：真实冒烟（`worksheet_20261008T004924Z`、`worksheet_20261008T004935Z`）是按旧逻辑跑的；是否重跑由用户决定（重跑前先报预计调用数）。

## 完成记录（2026-10-08）
- D 组全部补上：`test_resolution.py`（两路代表行不同、组内不一致四个分支、row 0 代表行）、`test_schemas.py::test_oe_resolution_1_1_records_read_as_untruncated`、`test_confidence_evidence.py`（r = 1.2、离散度不为负、停产计入来源、阈值可配）、`test_confidence_strategies.py`（A1、B1、judge_band 边界、单次采样失败）、`test_worksheet_cli.py`（`--limit`、真实 provider 拦截、预计调用数 = 实际调用数）。新增的 D 组用例中，不一致分支、两路代表行、旧 JSONL、边界、单次采样等在现有代码上直接通过，作为回归测试保留。
- fake 模式 `uv run pytest`：357 passed；ruff、`mypy src` 无问题。
- 对已有真实冒烟的影响：用修复后的代码对同一输入重跑 `evidence`（0 次调用，`worksheet_20261008T031515Z`），分数和档位与修复前完全相同；唯一变化是第 2、6、7、9 行的 `msrp_status` 变为空（B3）。这 20 行没有 A1（证据分 < 50 且无红档封顶）或 B1（被黄档封顶且封顶起作用）的行，所以自评 / judge 的调用行和混合分不变。只有 C3 改了 prompt 措辞（match type / match quality），重跑时 LLM 输出可能略有不同。是否重跑由用户决定。
