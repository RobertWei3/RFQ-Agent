# Phase 6 — 第二轮审查修复清单

来源：第二轮 review session（2026-10-08），复核 `phase-6-fixes.md` 的 25 项修复；范围为相对 `910dee1` 的全部未提交改动（含未跟踪文件）。
审查时 fake 模式 `uv run pytest`：357 passed（30.5s）；`test_stdio_server_via_cli` 未复现卡住。
§17 P6 验收标准全部通过；25 项中 23 项已修好，B1 与 D 组的 I5 用例各有遗留，见下。

## 使用方式
- 同 `phase-6-fixes.md`：按 `/tdd` 修，每条先写在现有代码上会失败的测试；完成后打勾并注明测试名。
- 本轮没有需要用户做业务决定的事项。C1 的配置校验只收紧明显无效的配置，不改默认值。
- 不在本清单范围：phase-6.md 末尾三个待决问题；`run_price_agent` 命名与分支（P7）。

## A. 必须修

- [x] **A1 B1 的 explanation 写错了封顶，也没写出参与混合的封顶后分数**（`_explanation` 改为：证据分后紧跟封顶、LLM 部分写明混合基数、混合再被封顶才写 `final capped`；测试 `test_explanation_shows_the_gate_before_the_judge_blend`、`test_explanation_flags_a_gate_that_caps_the_blend_again`）
  - 位置：`src/rfq_agent/confidence/scoring.py:220-235`（`_explanation`，条件 `min(caps) < max(raw, score)`）。
  - 问题：B1 要求 explanation 显示封顶前的证据分并说明封顶。现在的输出是：
    `evidence score 92 (...); judge agreed (score 70) -> 75; capped at 79 by gate_shared_disagree`，最终分是 75。
    - 读者看到 “capped at 79” 会以为最终分是 79。
    - 参与混合的 79 没有出现，读者按 0.5 × 92 + 0.5 × 70 算出 81，对不上 75。
    - judge 不同意（最终 49）、judge 低分（最终 50）、自评把分数拉低（最终 70）时都是同样的写法。
  - 修法：把封顶分成两处说明。
    - 证据分被封顶时，紧跟证据分写，例如 `evidence score 92, capped at 79 by gate_shared_disagree (weakest signals: …)`。
    - LLM 部分写明混合基数，例如 `judge agreed (score 70): 0.5 × 79 + 0.5 × 70 -> 75`。
    - 只有混合结果再次被封顶（最终分 < 混合分）时，才在末尾写 `final capped at N by …`；没有起作用的封顶仍写 `gates not binding`。
  - 测试：`test_confidence_strategies.py` 对上面这一行（judge 同意、70 分）断言 explanation 含 `capped at 79` 且出现在 judge 部分之前、含混合基数 79、不含 `final capped`；再对自评混合后高于封顶的行（例如 `shared_disagreeing` + 默认 fake，混合 82）断言含 `final capped at 79`。

- [x] **A2 自评的 B1 测试恒真**（`test_self_blends_with_the_gated_evidence_score` 改为 LLM 信号 0.5 的脚本化自评，断言 70；原用例保留为 `test_self_cannot_lift_a_row_above_its_gate`）
  - 位置：`tests/test_confidence_strategies.py:274`（`test_self_blends_with_the_gated_evidence_score`）。
  - 问题：`self_weight = 1.0` 时证据分一项系数为 0，封顶前和封顶后算出的都是 90，再封到 79，修复前也能通过，证明不了 B1。
  - 修法：用 `0 < self_weight < 1`、封顶起作用、且 LLM 把分数拉低的行，让两种算法结果不同。示例：`shared_disagreeing()`（原始 92，封顶 79），脚本化自评让 LLM 信号为 0.5（例如 OE 两次答对、把握 0，零售两次一致但答错），`self_weight` 0.3：
    - 封顶后算：0.7 × 79 + 0.3 × 50 = 70.3 → 70（正确）。
    - 封顶前算：0.7 × 92 + 0.3 × 50 = 79.4 → 79（修复前）。
  - 测试：断言最终分 70。原测试可删除或保留为“自评不能越过封顶”。

## C. 应该修（低风险）

- [x] **C1 档位配置允许 `green_min == yellow_min` 或 `yellow_min = 0`，红档判断按数值推断**（`BandSettings` 要求 `1 ≤ yellow_min < green_min`；`RED_GATES` / `YELLOW_GATES` 按名称定档位，`has_red_gate` 按名称判断，`red_cap()` 供 judge 不同意时共用；测试 `test_bands_need_distinct_positive_thresholds`、`test_red_gates_are_known_by_name_not_by_value`）
  - 位置：`src/rfq_agent/config.py:146-150`（`BandSettings._check_order` 只拒绝 `yellow_min > green_min`）；`src/rfq_agent/confidence/evidence.py:143`（`has_red_gate` 按 `cap == yellow_min − 1` 判断）。
  - 问题：
    - `green_min == yellow_min`（例如 50 / 50）时黄档封顶值等于红档封顶值，`insufficient_data` 这类黄档封顶被当成红档，自评不再调用，预计调用数也随之变化。
    - `yellow_min = 0` 时红档封顶为 −1，`ConfidenceResult` 校验 `score ≥ 0` 失败，整批报错。
  - 修法：
    - `BandSettings` 要求 `1 ≤ yellow_min < green_min`。
    - `gates()` 记录每个封顶属于红档还是黄档（例如返回带档位的小结构，或另给 `RED_GATES` 名称集合），`has_red_gate` 按档位判断，不按数值推断。`scoring.py:182` 的红档上限也从同一处取。
  - 测试：`test_config.py` 断言 50 / 50 与 `yellow_min = 0` 被拒；`test_confidence_evidence.py` 断言 `insufficient_data` 行不算红档封顶。

- [x] **C2 文档与修复后的规则不一致**（phase-6.md Q9、Q10、Q15、R1 已改；§12.2 补 explanation 要求；§7 与 `schemas.py` 的 `oe_candidates`、`msrp_status` 说明已更新）
  - `docs/plans/phase-6.md` Q15 仍写“self：未封顶行 × 2”，应为“没有红档封顶的行 × self_samples”（R5）。
  - `phase-6.md` Q9、Q10 的公式仍写“证据分”，应为“封顶后证据分”（R1）。Q6、Q11 已就地改写，Q9、Q10 也应就地改写，或注明“见 R1”。
  - RFQ-SPEC.md §12.2 和 phase-6.md R1 没有写 B1 对 explanation 的要求，应补一句：explanation 显示封顶前的证据分、触发的封顶、参与混合的封顶后分数，以及最终是否再次封顶（与 A1 一致）。
  - RFQ-SPEC.md §7（第 330、335 行）和 `src/rfq_agent/schemas.py:224,233` 的字段说明没有反映 B3、B4：
    - `oe_candidates` 应为“一对多时各 grounded 候选的当前号（不含 llm_inferred）”。
    - `msrp_status` 应注明“OE 不是 resolved 时为空”。

- [x] **C3 配置 key 名带死了 1.2**（改名 `up_to_suspicious`，config.py、settings.yaml、§18 三处同步）
  - 位置：`config.py` `MsrpRetailLevels.up_to_1_2x`；`config/settings.yaml`；RFQ-SPEC.md §18。
  - 问题：阈值已经可配置（`msrp_retail_suspicious_ratio`），key 名里的 `1_2x` 会过时。
  - 修法：改名为 `up_to_suspicious`（或类似名称），三处同步；旧 key 不需要兼容（P6 未合并）。

- [x] **C4 I5 测试证明不了“一致性记 1”**（参数化 `test_one_failed_self_sample_keeps_the_other`：两次答案不同且都成功 → 0；第一次失败 → 1）
  - 位置：`tests/test_confidence_strategies.py:292`（`test_one_failed_self_sample_keeps_the_other`）。
  - 问题：两次 fake 答案相同，两次都成功时 consistency 本来也是 1，断言 `self_oe_consistency == 1` 不能区分。explanation 那条断言有效。
  - 修法：用同一个脚本化处理函数做对照：两次采样给出不同的 OE 号时 consistency 为 0；把其中一次改为抛错后，consistency 为 1。

## 可选（判断类，不改也可以）
处理情况：TypeVar 已改名 `_Outcome`；测试里的 `# type: ignore[arg-type]` 已去掉（参数标为 `StrategyName`）。其余两条不改：自评 0.5 / 0.5 是 spec 写定的规则，不是调参项；`evidence_signals` 返回 `dict[SignalName, float]` 会牵动 `ConfidenceResult.signals`（还含 `gate_*`、LLM 信号）的类型，收益小。
- `scoring.py` 自评信号的 `0.5 × score + 0.5 × consistency` 写死在代码里，其他权重都在配置；spec 已写定 0.5，可以保持。
- `SignalName` 定义在 `config.py`，`evidence_signals` 仍返回 `dict[str, float]`；可改为 `dict[SignalName, float]`。
- `cli/worksheet.py:31` 的 TypeVar 名 `_O` 可改为 `_Outcome`。
- `tests/test_confidence_strategies.py:319-320` 的 `# type: ignore[arg-type]` 可通过把参数标注为 `StrategyName` 去掉。

## 完成标准
- A 组修完，C 组完成或注明不做的理由；fake 模式 `uv run pytest` 全部通过；ruff、`mypy src` 无问题。
- A1 只改 explanation 文本，不改分数；C1 只拒绝无效配置，默认配置下分数和调用次数不变。修完后零成本重跑 `evidence`，确认 20 行的分数和档位与 `worksheet_20261008T031515Z` 相同，记在 phase-6.md；不需要重跑真实 LLM。

## 完成记录（2026-10-08）
- fake 模式 `uv run pytest`：364 passed；ruff、`mypy src` 无问题。
- 零成本重跑 `evidence`（`worksheet_20261008T033130Z`）：20 行的 `WorksheetRow` 各列、信号与 `worksheet_20261008T031515Z` 完全相同（档位 11 / 5 / 4，行正确 20/20）；只有 explanation 文本按 A1 改了写法。未重跑真实 LLM。
