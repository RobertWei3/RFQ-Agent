# Phase 10 — 评估脚本 + 策略对比

分支：`phase-10-eval`（从 main 881d4b0 建）

## 目标与验收（spec §14、§17）
`rfq eval --run RUN_ID` 生成 `reports/eval_{run_id}.md` 和图表；`rfq eval compare --runs A B C` 输出对比表；用真实 LLM 对 200 行跑三种策略并输出对比表（运行前先报预计成本）。

## 开工前已定的决策（P9 合并后讨论，2026-10-08）
用户同意以下全部按推荐方案定，已写回 RFQ-SPEC.md §12.2 / §14 与 GLOSSARY.md。P10 的 /grilling 从这里接着问其余问题，不再重复这几条。

| # | 问题 | 决定 |
|---|---|---|
| P1 | 红档行算不算“行正确”（phase-6 待决 3） | 两个标签。“行正确”保留 P6 Q5 的定义，用于整行准确率（正确判断出 not_found / multiple 也算对）。新增“可直接使用”= 行正确且 OE 为 `resolved`、零售不是“有报价但全部离群”（应有成本 `insufficient_data` 仍可算），用作 ECE、AUROC、可靠性图、各档实际比例、覆盖率-准确率曲线的标签，与分数含义一致。另报红档中“行正确”的比例（被正确拦下的行） |
| P2 | judge prompt 加不加业务规则（phase-6 待决 2） | 加入影响证据解读的规则：规格冲突以内部库为准；MSRP 冲突取较高价、可沿替代链回退旧号、只采用美元；零售离群按中位数阈值、参考零售价按匹配质量 → 有货 → 实际价最低；OE multiple 不查价。写明这些是系统有意执行的规则、不作为疑点，judge 只查证据读对没有、规则用对没有。规则文本在代码里只有一份（注释标明对应 spec 章节）。不采用“不加”（会产生不成立的疑点并把好行封顶到红档，策略对比也不公平）和“加完整规则逐条核对”（重复代码已执行的确定性检查） |
| P3 | judge 同意时的 concerns（phase-6 待决 1） | 同意时的 concerns 也写进需复核原因，作为抽检提示；不同意时照旧（封顶红档并写进需复核原因） |

## P10 中要做的置信度改动（在策略对比之前）
- P2、P3 改的是 `confidence/llm_checks.py`（judge prompt）与 judge 结果写回需复核原因的逻辑；FakeLLM 的 judge 处理函数照旧，用测试覆盖 prompt 含规则、同意时 concerns 写进需复核原因。
- 改完后用 20 行样例真实重跑 `evidence_plus_judge`（约 5 次 judge 调用，约 $0.01），与 phase-6.md 的冒烟结果对比（尤其第 18 行的疑点）；运行前报调用数与成本，经同意由用户用 `!` 执行。

## /grilling 决定（2026-10-08）
Q1 用户选推荐方案；其余用户授权按推荐方案定，计划整体交用户审阅。

### 输入与标签
| # | 问题 | 决定 |
|---|---|---|
| Q1 | 运行对应的 ground truth 怎么找 | 自动推断，`--truth PATH` 可覆盖：先去掉 API 上传加的 `<uuid>_` 前缀，再按原文件名套用 `default_truth` 的规则（`rfq_sample_*` → `<dataset>/<stem>_ground_truth.jsonl`；数据集自己的询价单 → `<dataset>/ground_truth.jsonl`）；找不到时报错并提示用 `--truth`。校验：truth 与运行行的 row_id 集合相同，标准化后的客户件号逐行相同，否则报错 |
| Q2 | 评估哪个版本的行（人工复核会改行） | 评估系统自己的结论：复核过的行取 `review_log` 中最早的快照（复核前的整行），没复核的行取 `run_rows`。报告注明“已复核 N 行，按复核前的系统结论评估”。人工价格不参与评估 |
| Q3 | 能评估的运行 | 已收尾（`awaiting_review` 或 `completed`）；`running` 时报错。批失败的行按行失败算错 |
| Q4 | 两个标签的代码位置 | `eval/labels.py`：`row_correct(row, truth)` 复用 `quick_compare.compare_worksheet` 的各阶段判对；`usable_as_is(row, truth)` = 行正确且 `oe_status == "resolved"` 且不是“有报价但全部离群”（与封顶 `gate_retail_all_outliers` 同一判断，复用 `confidence.evidence` 里的函数，不另写一份） |

### 指标（§14）
| # | 问题 | 决定 |
|---|---|---|
| Q5 | OE | 覆盖率 = resolved / 全部行；准确率 = resolved 行中 current_oe 等于主号的比例（另报全行的 OE 结论正确率，含正确的 not_found / multiple）；替代链完全正确 = 系统与 truth 都是 resolved 的行中，链（标准化后）完全相同的比例 |
| Q6 | MSRP | 覆盖率 = found 或 discontinued / 全部行（与 `RunSummary` 一致）；准确率 = 有价的行中相对误差 ≤1% 的比例；另报 status 正确率与平均相对误差 |
| Q7 | 零售 | 每站点覆盖率 = 该站点有报价的行 / truth 在该站点有挂牌的行（另列有报价行数）；参考零售价站点正确率（全部行，truth 预期不选时不选也算对）；离群检测按（行，站点）对计算精确率和召回率，只看 truth 为 resolved 的行；没有预测离群时精确率记为空 |
| Q8 | 应有成本 | 规格正确率（品类、材质、重量 ≤1%）；到岸成本 MAPE 只算系统与 truth 都 estimated 的行，按零件自身 cycle time 与品类默认 cycle time 分开报（沿用 quick_compare） |
| Q9 | 置信度校准 | 标签“可直接使用”，概率 = score / 100。ECE：10 个等宽区间 [0,10)…[90,100]，Σ(区间行数 / 总行数) × \|平均分数 − 实际比例\|。AUROC 自己实现（Mann-Whitney 秩和，平局取平均秩），只有一类标签时为空并注明。可靠性图 = 各区间的平均分数与实际比例（附行数）。各档：行数、可直接使用比例、行正确比例；另报红档中行正确的比例。不加 sklearn |
| Q10 | 覆盖率-准确率曲线与阈值 | 阈值 t 取 0–100 每个整数：覆盖率 = 分数 ≥ t 的行 / 全部行，准确率 = 其中可直接使用的比例（无行时为空）。报告另列目标准确率 90% / 95% / 99% 时可用的最低阈值及其覆盖率，作为调档位阈值的依据。P10 不改 `settings.yaml` 的阈值和权重：200 行结果出来后给出调整建议，由用户决定 |
| Q11 | 成本与耗时 | 读 `llm_calls`，按 agent 分组：调用数、错误数、输入 / 输出 token、成本；合计、`RunSummary.elapsed_s`、每千行成本 = 合计成本 / 行数 × 1000。有未定价调用时成本为空 |

### 存储、CLI、对比
| # | 问题 | 决定 |
|---|---|---|
| Q12 | 结果模型与存储 | 新契约 `RunEval`（§7，含上面全部指标、曲线点、可靠性区间、每行的分数与两个标签、truth 路径、评估时间），`eval/metrics.py` 由纯函数算出。结果库新表 `run_evals(run_id PRIMARY KEY, created_at, data)`，重跑评估覆盖旧结果 |
| Q13 | `rfq eval --run RUN_ID [--truth] [--out reports]` | 计算 `RunEval` → 写库 → 写 `reports/eval_{run_id}.md` 和 PNG：`eval_{run_id}_reliability.png`、`eval_{run_id}_coverage.png`。终端打印要点（行正确、可直接使用、ECE、AUROC、各档）。0 次 LLM 调用。`reports/` 已在 `.gitignore` 中；验收数字记入本文件 |
| Q14 | 图表库 | 后端 PNG 用 matplotlib（新增运行时依赖，`Agg` 后端，不弹窗）；写之前查官方文档确认 API。Dashboard 仍用 Altair，按 `RunEval` 的 JSON 自己画 |
| Q15 | `rfq eval compare --runs A B C [--out reports]` | 只读已存的 `RunEval`，缺评估的运行报错并提示先跑 `rfq eval --run`。所有运行的 truth 客户件号必须相同（同一份询价单），否则报错。输出 `reports/compare_<时间>.md` 与叠加的可靠性图、覆盖率-准确率曲线 PNG，终端打印对比表。列：运行、策略、模型（各角色）、行数、绿 / 黄 / 红、行正确率、可直接使用率、ECE、AUROC、绿档可直接使用比例、红档行正确比例、准确率 95% 时的阈值与覆盖率、置信度阶段 LLM 调用 / 成本、合计成本（含 agent）、每千行成本、耗时 |
| Q16 | 三种策略怎么跑 200 行 | 新命令 `rfq rescore RUN_ID --strategy S [--allow-real]`：新建一个运行，复制源运行的询价行与四路结果（批全部标记 completed），只做收尾（消解 + 置信度，不复核），四个 agent 不重跑。这样三种策略基于同一份 agent 结果，对比只反映置信度策略本身，agent 成本也只花一次。`RunSpec` 新增 `rescored_from: str \| None = None`（schema 升到 1.1）。源运行必须已收尾。对比表的合计成本 = 本运行调用 + 源运行的 agent 调用（注明“含源运行 agent 成本”） |
| Q17 | API | `GET /runs/{id}/eval` 返回已存的 `RunEval`（200），没有时 404；不在 API 里触发评估（评估要读 ground truth，由 CLI 做）。`RunSpec.rescored_from` 通过已有接口自然返回 |
| Q18 | Dashboard 评估页 | 通过 HTTP，只读 JSON：① 404 显示“暂无评估，用 `rfq eval --run <id>` 生成”；② 各阶段指标表（OE / MSRP / 零售各站点 / 离群 / 应有成本）；③ 置信度：各档表、ECE、AUROC、可靠性图（Altair，对角线参考）、覆盖率-准确率曲线（目标准确率的阈值标注）；④ 成本与耗时表；⑤ 策略对比：多选其他运行，逐个 `GET eval`（404 的标“未评估”），客户端拼对比表和叠加曲线。图表规格由 `charts.py` 的纯函数生成并单测。不 import 后端 |

### 真实 LLM
| # | 问题 | 决定 |
|---|---|---|
| Q19 | 20 行 judge 重跑（P2、P3 改完后） | `rfq worksheet build` 读 phase-6 冒烟用的同一组 P2–P5 输出，`--strategy evidence_plus_judge --allow-real`。用 `expected_llm_calls` 算出调用数，报成本后由用户用 `!` 执行 |
| Q20 | 200 行验收 | ① `rfq run --input data/generated/n200_s42/rfq_200.xlsx --strategy evidence --allow-real`（四个 agent，粗估约 850 次调用、约 $0.5，按 P8 的 20 行约 85 次、约 $0.05 推算）；② 跑完后用 `expected_llm_calls` 算出 self / judge 的准确调用数（粗估 self 约 300 次、约 $0.15，judge 约 20–50 次、约 $0.05–0.1），再报一次；③ `rfq rescore` 两次 → 三个运行各 `rfq eval` → `rfq eval compare`。每一步的命令都报调用数和成本，经同意由用户用 `!` 执行 |

## TDD 测试接口（seam，已确认）
1. `confidence.score_rows`（已有 seam）：judge prompt 含业务规则文本（临时 fake 处理函数捕获请求）；judge 同意且有 concerns 时写进需复核原因，空 concerns 不写；不同意照旧。
2. `eval.labels.row_correct / usable_as_is`（纯函数，用 `tests/builders.py` 构造行与 truth）：not_found / multiple 的行正确但不可直接使用；全部离群的行不可直接使用；insufficient_data 的行可直接使用。
3. `eval.metrics`（纯函数）：`ece`、`auroc`（平局、单一类别）、可靠性区间、覆盖率-准确率曲线与目标阈值、各阶段指标、`build_eval(rows, truth, usage, summary) -> RunEval`。手算的小例子断言。
4. `eval.report`：markdown 含各节与数字；PNG 文件生成（非空、PNG 文件头）。
5. 结果库：`save_eval / latest_eval`；`rescore` 复制行与结果后收尾（fake，断言四个 agent 没有调用、`rescored_from` 已记）。
6. CLI（CliRunner + fake）：`rfq eval --run`（含 truth 推断、去 uuid 前缀、校验失败报错）、`rfq eval compare`（缺评估、询价单不同报错）、`rfq rescore`。
7. API：`GET /runs/{id}/eval` 200 与 404。
8. Dashboard：`charts.py` 评估图表的纯函数；评估页 AppTest（注入 `TestClient`：有评估、无评估、策略对比）。

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| I1 | `--limit` 的运行（行数少于 truth）能不能评估 | 能。Q1 的校验改为：运行的每一行都必须在 truth 中、标准化后的客户件号相同；truth 中多出的行不参与评估。用错 truth 文件仍会被客户件号校验拦下（200 行的 truth 与 20 行样例的同号行件号不同） |
| I2 | 源运行有人工复核时能否 rescore | 不能，报错。复核会改行（改 OE 号会改四路结果、改价会覆盖工作表），复制过去就不再是“同一份系统结果”；策略对比用的运行都不开复核 |
| I3 | `eval compare` 怎么判断“同一份询价单” | `RunEval` 不存客户件号，所以比较 truth 文件路径和评估的 row_id 集合，两者都相同才对比 |
| I4 | `rescore` 的运行前提示 | 源运行的证据分已知，所以 judge / self 的调用数是准确值（`expected_llm_calls`），不是粗估；真实 provider 同样需要 `--allow-real` |

## 实施顺序
1. 写回文档（spec §7 `RunEval`、`RunSpec.rescored_from`；§13 `rfq rescore`；§14 指标细则 Q5–Q11、Q13、Q15；§15.1 / §15.2 eval 接口与页面；§18 无变化；GLOSSARY：重新打分），单独提交。
2. seam 1 → 报 20 行 judge 重跑的调用数与成本，等用户同意、由用户执行。
3. seam 2 → 3 → 4 → 5 → 6 → 7 → 8；用脚本替换文本时每处断言只匹配一次。
4. 停下汇报；按 Q20 报 200 行验收的调用数与成本，经同意由用户执行。

## 完成记录（2026-10-09，fake 模式）
- `uv run pytest`：601 passed（P9 合并时 528；新增 judge 规则与 note 3、标签 6、指标 19、报告 5、结果库 6、CLI 8、API 1、Dashboard 图表 4 与评估页 2，另改了 2 个已有测试）；ruff、`mypy src`、`mypy --ignore-missing-imports dashboard` 无问题。
- fake 冒烟（200 行，`rfq_200.xlsx`）：`rfq run --strategy evidence` 864 次调用，绿 115 / 黄 49 / 红 36，行正确 200/200；`rfq rescore` 两次：self 预计 328、实际 328，judge 预计 49、实际 49；三个运行 `rfq eval` 后 `rfq eval compare` 输出对比表与两张叠加图（已目视检查 PNG）。fake 下 ECE 约 0.2，原因是 fake 结论全对、而黄档分数在 50–79。
- 冒烟中发现的问题：真实结果库是旧版本建的，`row_outcomes` 后加的两列在表末尾，rescore 按位置复制会错位。已改为写明列名，回归测试 `test_rescore_copies_outcomes_in_a_database_whose_columns_were_added_later`。
- 真实 LLM：20 行 judge 重跑与 200 行三策略验收待用户执行（见 Q19、Q20）。

## 20 行 judge 真实重跑（P2、P3 改完后，2026-10-09，用户执行）
输入同 phase-6 冒烟（P2–P5 的 20 行真实输出）；输出 `worksheet_20261009T221550Z`：5 次调用，0 错误，0.67 万 / 0.05 万 token，$0.011（预计约 $0.012）；绿 15 / 黄 1 / 红 4（phase-6 为 13 / 3 / 4）；行正确 20/20。

| 行 | phase-6（judge 同意 / 给分 → 最终分） | 现在 | 需复核原因中的 judge note |
|---|---|---|---|
| 1 | 88 → 79 黄（insufficient_data 封顶） | 95 → 79 黄 | 无 |
| 5 | 82 → 81 绿 | 85 → 82 绿 | MSRP 只有一页、无旁证；规格只有外部页 |
| 12 | 82 → 81 绿 | 92 → 86 绿 | MSRP 高于全部零售价；规格只有外部页 |
| 13 | 82 → 79 黄 | 92 → 84 绿 | MSRP 页无币种按 USD、规则允许；MSRP 高于零售价 |
| 18 | 78 → 77 黄 | 92 → 84 绿 | 无 |

观察：
- 第 18 行“内部重量没有明确理由就被采用”的疑点消失，第 13 行的“内外部重量冲突”也不再出现：judge 按规则把它们当作有意行为。
- 加规则后 judge 给分普遍升高（78–88 → 85–95），第 13、18 行从黄升到绿。这 20 行全对，看不出是更准还是更宽松，要看 200 行的校准结果。
- judge 同意时的 concerns 现在写进需复核原因（P3）。第 13 行的 concerns 末尾写了 “No material concern found”，说明 judge 有时把“没有问题”的说明也写进 concerns，会给需复核原因带来噪声；先记录，不改。

## 200 行三策略验收（真实 LLM，2026-10-09，用户执行）
输入 `data/generated/n200_s42/rfq_200.xlsx`。先用 `evidence` 完整运行，再在同一份 agent 结果上 `rfq rescore` 两次；三个运行 `rfq eval` 后 `rfq eval compare`（`reports/compare_20261009T224658Z.md`，`reports/` 不入库）。

| 运行 | 策略 | 调用（预计 / 实际） | 本运行成本 | 合计（含 agent） | 每千行 | 绿 / 黄 / 红 | 行正确 | 可直接使用 | ECE | AUROC | 95% 阈值（覆盖率） |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-20261009T223513Z-be7e70` | evidence | 864 / 864（fake 预演） | $0.453 | $0.453 | $2.27 | 115 / 49 / 36 | 100% | 82.0% | 0.209 | 1.000 | 50（82.0%） |
| `run-20261009T224531Z-1a1d21` | evidence_plus_self | 328 / 328 | $0.166 | $0.619 | $3.09 | 148 / 16 / 36 | 100% | 82.0% | 0.190 | 1.000 | 50（82.0%） |
| `run-20261009T224635Z-5c985b` | evidence_plus_judge | 49 / 49 | $0.108 | $0.561 | $2.81 | 154 / 10 / 36 | 100% | 82.0% | 0.192 | 1.000 | 50（82.0%） |

全部 0 错误；合计 $0.727（运行前估计约 $0.72）。完整运行耗时 163 秒。

各阶段（三个运行共用同一份 agent 结果）：OE 覆盖率 82.0%、resolved 行准确率 100%、结论正确率 100%、替代链 100%；MSRP 覆盖率 79.5%、准确率 100%；零售各站点覆盖率 100%、参考站点 100%、离群检测精确率 / 召回率 100%；应有成本规格 100%，到岸成本 MAPE 0.0%（零件自身 cycle time，n=146）/ 8.3%（品类默认 cycle time，n=30，按设计有偏差）。

观察：
- deepseek-flash 在 200 行合成数据上各阶段全对。所以“可直接使用”恰好等于“没有红档封顶”（红档 36 行全是 OE multiple 20 行、not_found 16 行，都判对了但不能直接用），AUROC = 1 是封顶规则决定的，不说明分数的区分能力。
- 分数偏保守：60–90 分的 132 行全部可直接使用（ECE 约 0.2 全来自低估）。self 与 judge 都只是把黄档行抬上绿档（ECE 0.209 → 0.190 / 0.192），没有拦下任何错行，因为没有错行可拦。
- 成本：judge 49 次 $0.108，self 328 次 $0.166；在这份数据上两者效果相近，judge 更便宜。
- 结论：验收要求的报告、图表与三策略对比表都已产出；但这份合成数据太干净，不能作为选择策略和调阈值的依据（曲线上的阈值 50 = 现行黄档下限，是封顶决定的，不是分数校准出来的）。阈值与权重暂不改，待用户决定（见下）。

用户决定（2026-10-10）：
1. 是否按这次结果调档位阈值 / 权重 → **不调**。
2. 是否做一次“更难的数据”对比 → **做**，见 `phase-10-inject.md`。原文：是否做一次“更难的数据”对比，让分数有错行可区分：例如用生成器加大噪声比例或换 seed 再跑 200 行，或注入 agent 错误。会再花约 $0.7（同样的三步），属于新的实验范围。
