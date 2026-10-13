# Phase 6 — 实体消解 + 置信度三种策略

分支：`phase-6-resolution-confidence`

## 目标与验收（spec §12.1、§12.2、§17）
合并四个 agent 的结果，每个 row_id 生成一条 `WorksheetRow`；按可切换的策略（`evidence` / `evidence_plus_self` / `evidence_plus_judge`）给出 0–100 置信度和档位。
验收：多对一、一对多场景测试通过；三种策略都能切换运行。

## 已定决策
| # | 问题 | 决定 |
|---|---|---|
| Q1 | 输入与运行方式 | 新 CLI 读四个 agent 已有的 JSONL 输出（`--oe --msrp --retail --cost`，加 `--rfq` 取询价行），做消解与置信度，写 `data/runs/worksheet_<时间>.jsonl` + `.trace.jsonl` + `.meta.json`；`--strategy` 覆盖配置。`resolution/`、`confidence/` 为只收发 Pydantic 对象的函数（自评 / judge 为 async），P7 直接调用。不在 P6 串跑四个 agent（那是 P7） |
| Q2 | 多对一的价格复用 | OE `resolved` 且 `current_oe` 标准化后相同的行成组；组内 MSRP、零售结果统一用代表行（组内 row_id 最小且该路无行失败）的结果；每行注明“与第 X、Y 行共用 OE，价格取自第 X 行”；组内各行自己的价格结论不一致（MSRP 状态/数值、selected 站点/价格）时照用代表行结果，并在需复核原因中写“共用 OE 的行价格结论不一致”。应有成本不共用（按本公司件号）。`multiple` 行不分组。留给 P7：编排器对同一 `current_oe` 只跑一次价格 agent（P6 不实现） |
| Q3 | `WorksheetRow` 结构 | 两层：`WorksheetRow` 为扁平列模型（§12.1 各列 + 共享关系、OE 候选号、各阶段失败），Excel / 行列表 / Dashboard 表格共用；`ResolvedRow` 含 `WorksheetRow` 与完整的四路结果、各阶段 `RowFailure`、`ConfidenceResult`，供行详情与置信度使用，P6 输出 JSONL 每行一条 `ResolvedRow`。“建议参考价区间”不另设字段，即 MSRP、参考零售价、landed cost 三列并列 |
| Q4 | 行失败与缺行 | 某路为 `RowFailure` 时照常生成 `WorksheetRow`：该路列为空，失败列记“路:阶段:原因”（如 `msrp:search_chain:timeout`），需复核原因写“MSRP 未查成：…”，与“查了没有”（not_found / insufficient_data）分开表述；OE 失败时下游三路的 `upstream_oe` 只报一次根因“OE 解析失败”。四个文件与询价单的 row_id 集合必须一致，否则 CLI 报错列出差异、不出部分结果；`--limit N` 时取询价单前 N 行，四个文件都须含这些行 |
| Q5 | 置信度分数的含义 | 一行一个分数，表示“这一行结论整体可直接使用”的把握。“行正确”定义（P10 评估照此实现，P6 的对照表先用）：OE 按 phase-1 Q1 判对；MSRP status 正确且有价时相对误差 ≤1%；零售 selected 站点正确；应有成本 status 与规格（品类、材质、重量）正确；任一阶段失败或任一项不对即整行错 |

以下 Q6–Q16 由你授权按推荐方案定（2026-10-06）。

| # | 问题 | 决定 |
|---|---|---|
| Q6 | 硬性封顶（gate） | 先算分，再按规则封顶，三种策略都适用（LLM 不能把封顶的行抬上去）。封顶到红档上限（`yellow_min − 1`，默认 49）：OE 为 `multiple`（待人工选择）、OE 为 `not_found`、任一路行失败、零售有报价但全部离群（无参考零售价；一条报价都没有不封顶）。封顶到黄档上限（`green_min − 1`，默认 79）：共用 OE 的行价格结论不一致、应有成本 `insufficient_data`。被封顶的规则写进 `signals`（`gate_*`）和 explanation |
| Q7 | `evidence` 的六个信号（0–1） | `oe_match`：候选中最好的匹配类型，exact 1.0 / normalized 0.9 / interchange 0.7 / llm_inferred 0.2。`chain`：替代链因 4 跳上限被截断 0.3，否则 1.0。`price_sources`：(MSRP found/discontinued 记 1) + 非离群零售报价数，`min(n, 4) / 4`。`retail_dispersion`：非离群报价 effective_price 的变异系数 cv（总体标准差 / 均值），`max(0, 1 − cv / retail_outlier_threshold)`，少于 2 条记 0.5。`msrp_retail`：参考零售价 / MSRP 的比值 r，r ≤ 1.0 记 1.0、≤ 1.2 记 0.5、> 1.2 记 0，任一缺失记 0.5。`spec_source`：both_agree 1.0 / internal 0.8 / external 0.5 / conflict 0.3 / missing 0。分数 = ROUND_HALF_UP(100 × Σ权重·信号 / Σ权重)。默认权重 oe_match 0.25、chain 0.10、price_sources 0.20、retail_dispersion 0.15、msrp_retail 0.10、spec_source 0.20。权重和各档取值都写在 `settings.yaml` 的 `confidence.evidence` 下，P10 按评估结果调 |
| Q8 | 替代链是否完整从哪来 | `OEResolution` 新增 `chain_truncated: bool = False`（schema_version 升到 1.2）：`oe_interchange` 到 4 跳上限仍有未追的号码时为 true。已有 JSONL 没有该字段，按 false 读入 |
| Q9 | `evidence_plus_self` | 只对没有红档封顶的行调用（见 R5）。新角色 `self_eval`：模型同主模型，temperature 0.7（`llm.roles.self_eval`）。同一 prompt 采样 `self_samples`（2）次，每次让 LLM 自己回答关键判断并给出把握：`oe_choice`（它认为正确的当前 OE 号）+ `oe_confidence`；有参考零售价时再给 `retail_choice`（它会选的站点）+ `retail_confidence`。每个判断：`score` = 各次采样（答案与系统结论一致 ? 把握/100 : 0）的平均；`consistency` = 各次答案（件号按标准化比较）全部相同 ? 1 : 0；判断信号 = 0.5·score + 0.5·consistency；`llm_signal` = 各判断平均。最终分 = ROUND_HALF_UP((1 − self_weight)·封顶后证据分 + self_weight·100·llm_signal)（见 R1），`self_weight` 默认 0.3，之后再按 Q6 封顶。部分采样失败用成功的那些；全部失败则用证据分，explanation 注明“LLM 自评失败”，不算行失败 |
| Q10 | `evidence_plus_judge` | 只对证据分（封顶后）落在 `judge_band`（50–79）的行调用。角色 `judge`（deepseek-v4-pro，temperature 0），每行 1 次。judge 看到整行的证据与结论（OE 候选与证据片段、替代链、MSRP 报价、各站点报价与离群、参考零售价、规格来源与理由），输出 `agree`、`score`（0–100）、`concerns`。agree → 最终分 = ROUND_HALF_UP((1 − judge_weight)·封顶后证据分 + judge_weight·score)（见 R1），`judge_weight` 默认 0.5；不同意 → 同样混合后再封顶到 49（红档），`concerns` 写进需复核原因。judge 调用失败 → 用证据分，explanation 注明，不算行失败 |
| Q11 | LLM 看到什么 | 代码把 `ResolvedRow` 渲染成确定性的“行摘要”文本（自评只含 OE 与零售部分，judge 含全部）；不给 ground truth。FakeLLM 新增两个处理函数：自评按摘要里的证据和业务规则推出答案（替代链链尾、选价规则；摘要不含系统结论）、把握 80；judge 一律同意、score 70。测试里用临时注册的处理函数模拟不一致与不同意 |
| Q12 | 档位 | `score ≥ green_min` 绿；`≥ yellow_min` 黄；否则红；阈值读配置 |
| Q13 | 需复核原因与 explanation | 需复核原因是字符串列表，所有档位都填（不只红档）：封顶原因、各路失败、MSRP 未找到 / 停产 / 回退旧号、无参考零售价、离群站点、规格冲突 / 缺失 / insufficient_data、共用 OE 不一致、judge 的 concerns。explanation 写分数构成（证据分、得分最低的两项信号、LLM 部分、封顶）。与 P3–P5 的 reasoning 一致用英文 |
| Q14 | 各阶段 reasoning 摘要 | 每路 reasoning 截取前 200 字符（同 Evidence 片段上限），行失败时为失败原因 |
| Q15 | CLI | `rfq worksheet build --oe --msrp --retail --cost [--rfq] [--strategy] [--limit] [--truth] [--allow-real] [--trace/--no-trace]`。`--rfq` 缺省时依次从 should_cost、oe_interchange 的 `.meta.json` 取。运行前用已有的 `announce_and_guard` 报预计 LLM 调用数（self：没有红档封顶的行 × `self_samples`（见 R5）；judge：证据分在 judge_band 的行数，可先算出）；真实 provider 需 `--allow-real`。输出 `data/runs/worksheet_<时间>.jsonl`（每行一条 `ResolvedRow`）+ `.trace.jsonl` + `.meta.json`；终端打印档位分布。不写结果库（P8） |
| Q16 | 人工检查对照表 | 给 `--truth` 时生成 `.compare.md`：每行 score、档位、需复核原因、按 Q5 判的“行正确”（复用 `quick_compare` 现有的各阶段判对函数）；汇总各档位行数与其中正确行数。正式的 ECE / AUROC 等留到 P10 |

## 实施中追加的决定
I1–I6 为实施中追加的决定；R1–R8 为第一轮 review（`phase-6-fixes.md`）的处理。

| # | 问题 | 决定 |
|---|---|---|
| I1 | 封顶“共用 OE 的行价格结论不一致”靠什么判断 | `WorksheetRow` 加布尔列 `shared_prices_disagree`，不去解析需复核原因的文字；已写入 §7 |
| I2 | 需复核原因由谁生成 | 消解阶段按各路结果直接生成（OE、失败、MSRP、零售、应有成本、共用 OE 不一致）；置信度阶段只追加 judge 的 concerns |
| I3 | 一个路径的代表行与另一路不同时怎么注明 | `reasoning_summary["shared"]` 写“MSRP taken from row X, retail taken from row Y”；`price_source_row` 取 MSRP 的代表行，MSRP 用本行时取零售的代表行 |
| I4 | 截断怎么判 | `oe_interchange` 到 `MAX_HOPS` 时仍有待追号码即为截断，reasoning 末尾注明 |
| I5 | 只有 1 次自评采样成功时的一致性 | 记 1（无法比较）；explanation 注明“only 1 self-assessment sample(s) succeeded” |
| R1 | （review B1）混合用封顶前还是封顶后的证据分 | 封顶后（用户选 A）；混合后再封顶。explanation 依次写：封顶前的证据分及触发的封顶（如 “evidence score 92, capped at 79 by gate_shared_disagree”）、参与混合的封顶后分数（如 “0.5 x 79 + 0.5 x 70 -> 75”）、混合结果再次被封顶时的 “final capped at N by …”。写入 §12.2 |
| R2 | （review B2）零报价是否封顶 | 不封顶（用户选 A），只有“有报价但全部离群”封顶；Q6 措辞已改。写入 §12.2 |
| R3 | （review B3）OE 非 resolved 时工作表 `msrp_status` | 为空（用户选 A），`ResolvedRow.msrp` 保留原始结果。写入 §12.1 |
| R4 | （review B4）`oe_candidates` 是否含 `llm_inferred` | 不含（用户选 A），只留在 `ResolvedRow.oe.candidates`。写入 §12.1 |
| R5 | （review A1）自评的触发条件 | 改为“没有红档封顶”，与 Q9 一致；自评 / judge 是否调用抽成一个函数，CLI 预计调用数共用 |
| R6 | （review C2）Q11 的 fake 写法 | Q11 改为：自评 fake 按摘要里的证据和业务规则推出答案（替代链链尾、选价规则），摘要本身不含系统结论 |
| R7 | （review C1）`evidence` 策略的运行前提示 | 只有 LLM 策略走 `announce_and_guard`（按角色打印 provider / model 并拦截真实调用）；`evidence` 策略没有 LLM 角色，只打印行数和 0 次调用，不做拦截 |
| R8 | （review C5/C6）配置 | `confidence.evidence` 新增 `msrp_retail_suspicious_ratio: 1.2`；`oe_match` / `spec_source` 的 key 必须与契约中的取值完全一致，否则加载配置时报错；`schemas.MatchType` 抽出供 config 校验 |
| I6 | P2 代码改动 | 除 `chain_truncated` 外，`agents/runner.py` 加通用 `read_outcomes`（`read_oe_outcomes` 改为调用它），行为不变 |

## 留给后续 Phase
- P7：编排器对同一 `current_oe` 只跑一次价格 agent，结果按 Q2 复用；`run_price_agent` 的命名与 should_cost 分支（phase-5 遗留）在 P7 整理，P6 不动。
- P8：`WorksheetRow` 写入结果库、Excel 着色；P10：按“可直接使用”做校准评估（见文末待决问题 3 的决定），再定阈值和权重。

## 要写回的文档
- RFQ-SPEC.md：§7 新增 `WorksheetRow`、`ResolvedRow`，`OEResolution` 1.2；§12.1 写入 Q2–Q4、Q14；§12.2 写入 Q5–Q13；§18 补 `confidence.evidence`、`self_weight`、`judge_weight` 与 `llm.roles.self_eval`。
- GLOSSARY.md：共用 OE 组 / 代表行、证据信号、封顶、LLM 自评、judge 复核、需复核原因、行正确。

## TDD 测试接口（seam，已确认）
1. `resolution.resolve_rows(lines, oe, msrp, retail, cost) -> list[ResolvedRow]`（纯函数）：多对一成组与复用、组内不一致、一对多、各路失败、OE 失败只报一次根因、row_id 不一致报错。
2. `confidence.evidence_signals(row, cfg)` / `evidence_score(...)` / `apply_gates(...)`（纯函数）：每个信号的取值、加权取整、各封顶规则、档位。
3. `confidence.score_rows(rows, settings, strategy, model=None) -> list[ResolvedRow]`（async）：三种策略在 FakeLLM 下切换运行；用临时 fake 处理函数验证自评的一致性 / 混合计算、judge 只覆盖 50–79、不同意封顶、LLM 失败回退；断言 `llm_calls` 调用次数。
4. CLI `rfq worksheet build`（CliRunner + fake）：读 P2–P5 格式的 JSONL，三种 `--strategy` 都能跑，产出文件与对照表。
另：`oe_interchange` 截断时 `chain_truncated=true` 的单测。

## 实施顺序
1. 写回 spec / glossary（本计划确认后）。
2. `/tdd`：seam 1 → 2 → `chain_truncated` → 3 → fake 处理函数 → 4。
3. 报真实 LLM 冒烟的调用次数和成本（复用 P2–P5 的 20 行真实输出，跑 evidence_plus_self 与 evidence_plus_judge），经同意再跑。

## 零成本检查（evidence 策略，真实 P2–P5 输出，2026-10-08）
`rfq worksheet build` 读 `oe_interchange_20261004T193845Z`、`oe_msrp_20261005T234019Z`、`retail_price_20261006T081732Z`、`should_cost_20261007T055911Z`：0 次 LLM 调用；绿 11、黄 5、红 4；按 Q5 判行正确 20/20（各档全对）。第 12、19 行共用 OE `1328.97.109`，价格一致，未触发不一致封顶。
注意：红档 4 行（OE not_found 2、multiple 2）按 Q5 也算“行正确”，因为 OE 结论本身是对的。它们不能直接使用，所以 P10 做校准时，“行正确”是否要排除这类行需要再定。

## 交付
`schemas.py`（`WorksheetRow`、`ResolvedRow`、`OEResolution` 1.2）、`resolution/merge.py`、`confidence/{evidence,scoring,llm_checks,brief,fake_handlers}.py`、`config.py`（`confidence.evidence`、`self_weight`、`judge_weight`）、`settings.yaml`（含 `llm.roles.self_eval`）、`cli/worksheet.py`（`rfq worksheet build`）、`quick_compare` 的工作表对照；测试 `test_resolution.py`、`test_confidence_evidence.py`、`test_confidence_strategies.py`、`test_worksheet_cli.py`、`test_oe_interchange.py` 截断用例、`tests/builders.py`。
三轮 review 清单及处理记录：`phase-6-fixes.md`、`phase-6-fixes-2.md`、`phase-6-fixes-3.md`。

## 真实 LLM 冒烟测试（2026-10-08）
输入同上（P2–P5 的 20 行真实输出）；命令行 `RFQ_LLM_PROVIDER=deepseek … --allow-real`。

| 策略 | 输出 | 调用 | token（输入 / 输出） | 成本 | 档位 绿/黄/红 | 行正确 |
|---|---|---|---|---|---|---|
| evidence | `worksheet_20261008T004431Z` | 0 | – | $0 | 11 / 5 / 4 | 20/20 |
| evidence_plus_self（deepseek-flash，T=0.7） | `worksheet_20261008T004924Z` | 32，0 错误 | 2.95 万 / 0.60 万 | $0.016 | 15 / 1 / 4 | 20/20 |
| evidence_plus_judge（deepseek-v4-pro） | `worksheet_20261008T004935Z` | 5，0 错误 | 0.55 万 / 0.06 万 | $0.009 | 13 / 3 / 4 | 20/20 |

运行前估计合计约 $0.018、上限约 $0.05；实际 $0.026。自评输出 token 约为估计的 2 倍，原因是 reasoning 写得较长。

观察：
- 自评：16 行的 32 次采样，OE 和零售站点都与系统结论一致，两次采样也完全一致（consistency 全为 1），把握在 72–95。LLM 信号 0.88–0.97，只把分数抬高了 0–7 分，第 1、5、12、13、18 行因此升档，这个区分度很弱。原因是合成页面规整，系统结论全对，LLM 没有可反对的地方。
- judge：第 1、5、12、13、18 行全部同意，给分 78–88；第 5、12 行从黄档升到绿档。虽然都同意，5 行都写了 concerns（如第 1 行“MSRP 237.92 明显高于零售价 110–139”，第 13、18 行“内外部重量冲突”）。按 Q10，concerns 只在不同意时才写进需复核原因，所以这些 concerns 现在只记在 trace 里，工作表上看不到（待你决定是否改）。
- judge 对第 18 行说“内部重量没有明确理由就被采用”，这与 §11.4“冲突以内部库为准”的规则矛盾：judge 不知道这条业务规则。
- 三种策略都没有误判。这 20 行全对，样本又太小，所以看不出哪种策略校准更好，要到 P10 用 200 行跑完再比较。

第一轮 review 修复后（2026-10-08，`phase-6-fixes.md`）：上面两次真实冒烟是按修复前的逻辑跑的。用修复后的代码对同一输入零成本重跑 `evidence`（`worksheet_20261008T031515Z`），分数和档位不变，只有第 2、6、7、9 行的 `msrp_status` 变为空。这 20 行不涉及 A1 / B1，自评和 judge 的调用行与混合分也不会变；只有 prompt 措辞改了（C3）。未重跑真实 LLM，是否重跑由用户决定。

第一轮 review 修复后真实 LLM 重跑（2026-10-08，经用户同意）：
| 策略 | 输出 | 调用 | 成本 | 档位 绿/黄/红 | 行正确 |
|---|---|---|---|---|---|
| evidence_plus_self | `worksheet_20261008T032231Z` | 32，0 错误 | $0.016 | 15 / 1 / 4 | 20/20 |
| evidence_plus_judge | `worksheet_20261008T032239Z` | 5，0 错误 | $0.010 | 13 / 3 / 4 | 20/20 |

预计调用数与实际一致（32、5）。档位分布与修复前相同。
逐行分数与修复前的差异都来自 LLM 采样波动（自评 temperature 0.7），没有档位变化：自评有 6 行相差 1–2 分（第 4、5、12、13、14、18 行）；judge 只有第 13 行从 77 变为 79（judge 给分从 78 变为 82）。

第二轮 review 修复后（2026-10-08，`phase-6-fixes-2.md`）：只改 explanation 文本、档位配置校验、红档封顶按名称判断、配置 key 改名 `up_to_suspicious` 和文档。零成本重跑 `evidence`（`worksheet_20261008T033130Z`），分数、档位、信号与 `worksheet_20261008T031515Z` 完全相同。默认配置下自评和 judge 的调用行和混合分不变，所以没有重跑真实 LLM。

第三轮 review 修复后（2026-10-08，`phase-6-fixes-3.md`）：explanation 列出所有没有起作用的封顶；judge 不同意时只有混合分高于红档上限才写 “held at”；封顶名称与档位改为一张映射表 `GATE_BANDS`，没有归类的封顶直接报错；文档补全。分数、档位、信号不变（零成本重跑 `evidence` 核对，见 `phase-6-fixes-3.md` 完成记录）。

## 待决问题（P9 合并后由用户决定，2026-10-08：三条都按推荐，已写回 RFQ-SPEC.md §12.2 / §14、GLOSSARY.md，实现见 phase-10.md）
1. judge 同意时写的 concerns 是否也写进需复核原因。现在只有不同意时才写，同意时的 concerns 只在 trace 里（见“真实 LLM 冒烟测试 / 观察”）。建议写进去，作为抽检时的提示。可在合并前或 P8（工作表输出）前决定。→ **决定：写进去**（同意与不同意都写）。
2. judge prompt 缺少业务规则。judge 不知道“冲突时以内部库为准”等规则，第 18 行因此提出了不成立的疑点。可在 P10 比较策略前，把关键业务规则加进 judge prompt。→ **决定：加入影响证据解读的规则**，并说明这些规则不作为疑点。
3. 红档行是否算“行正确”。OE not_found / multiple 的行按 Q5 算正确，但不能直接使用，这会影响 §14 的校准指标（见“零成本检查”）。P10 做校准评估前必须决定。→ **决定：两个标签**：“行正确”保留，用于整行准确率；新增“可直接使用”（行正确且 OE resolved、零售不是全部离群），用于校准与覆盖率-准确率曲线；另报红档中行正确的比例。
