# Phase 10 追加 — 错误注入实验（置信度策略能否识别错行）

分支：`phase-10-inject`（从 main 95842b8 建）

## 背景（2026-10-10）
- P10 的 200 行真实验收各阶段全对，三种策略比不出区分度（`phase-10.md`）。用户决定：档位阈值和权重**不调**；再做一次“更难的数据”对比。
- 第一次尝试（/grilling Q1 选 A）：生成 `data/generated/n200_s43`（seed 43，替代链 0.30、一对多 0.20、零售离群 0.20、件号写法不同 0.40、规格冲突 0.15、输入脏乱 0.15），干扰页 `max_decoys` 2 → 4（只在配置副本里改）。真实运行 `run-20261010T214022Z-cf1928`：860 次调用、0 错误、$0.452，绿 88 / 黄 56 / 红 56，行正确仍为 200/200。
- 原因：agent 在调 LLM 之前只保留正文提到所查件号的页面（P2 的确定性预过滤，trace 记 `pages_filtered_out`），干扰页只是件号前缀相近，全部被过滤；难点比例只影响规则层。所以用现有开关做不出 LLM 会犯的错，这份数据上不跑 rescore（省约 $0.27）。
- 用户选 B：在已有结果上注入错误，测各策略能否把错行压下去。

## /grilling 决定
| # | 问题 | 决定 |
|---|---|---|
| Q1 | “更难”从哪来 | 先用现有开关（A，结果见上），错行太少再追加注入（B） |
| Q2 | 第①步参数 | 如上（seed 43、提高的比例、4 个干扰页），其余与 P10 验收相同 |
| Q3 | 怎么制造错行 | B：在结果上注入错误。不改生成器（C 可以以后单独做：正文提到所查件号、讲的却是另一个零件的“难页”） |
| Q4 | 注入什么、多少 | 按固定 seed 从**可注入行**中抽 40 行（约全部的 20%），三类各约 1/3：① OE 错：`current_oe` 与替代链链尾换成相邻假件号（最后一位数字 +1，9 变 0），候选与证据片段不动；② MSRP 错：MSRP 报价改为原价 × 1.15–1.35（两位小数），证据片段不动；③ 参考零售价错：`selected` 改为另一个非离群、按规则不该选的站点。不连带改其他路；ground truth 不变，评估照常把注入行判为行错、不可直接使用 |
| Q5 | 怎么实现、记在哪 | 分支 `phase-10-inject`；命令 `rfq eval inject SOURCE --rows 40 --seed 7`：按 rescore 的方式复制源运行 → 改写四路结果 → 用 `evidence` 收尾（不调 LLM）；`RunSpec.rescored_from` 记源运行；结果库新表 `run_injections(run_id, row_id, kind)`；`rfq rescore` 复制运行时连同注入记录一起复制；`RunEval` 新增可选的 `injection`（注入行数、按类型的各档行数、被拦下的比例、注入行与未注入的可直接使用行之间的 AUROC），评估报告、`eval compare`、Dashboard 评估页都显示 |

### 实施细则（用户授权按推荐定）
| # | 问题 | 决定 |
|---|---|---|
| D1 | 可注入行（不读 ground truth，只看系统结果） | 没有红档封顶、OE 为 resolved、没有行失败、不在共用 OE 组（`shared_oe_rows` 为空，组内价格取自代表行，改非代表行不生效）、有 MSRP 报价、有参考零售价且还有别的非离群报价可换。源运行 `run-20261010T214022Z-cf1928` 有 120 行可注入 |
| D2 | 抽样与分类 | `random.Random(seed)` 打乱可注入行的 row_id（先排序），取前 N 行，第 i 行的类型为 `("oe", "msrp", "retail")[i % 3]`。可注入行不足 N 时报错 |
| D3 | 注入后的运行 | `strategy=evidence`、`review=False`、`rescored_from=SOURCE`；源运行同 rescore 的要求（已收尾、无人工复核）。注入只改 `row_outcomes`，收尾（消解 + 置信度）照常 |
| D4 | “被拦下” | 注入行落在黄档或红档（不在绿档，即不会被直接使用） |
| D5 | 注入 AUROC | 正类 = 未注入且可直接使用的行，负类 = 注入行，按分数算 AUROC（同 §14 的实现）；表示分数把注入行排在正常行之后的能力 |
| D6 | 契约 | `InjectionKind = Literal["oe", "msrp", "retail"]`；`InjectionEval{rows, bands_by_kind: dict[kind, dict[Band, int]], held_back_rate, green_injected, auroc}`；`RunEval.injection: InjectionEval \| None = None`，`RunEval` 升 1.1 |
| D7 | 对比表 | 新增三列：注入行被拦下比例、绿档中的注入行、注入 AUROC；没有注入的运行显示 `-`。报告、Dashboard 两边与一致性测试同步 |

## TDD 测试接口（seam）
1. `eval.inject.inject_errors(rows, outcomes, n, seed) -> (outcomes, list[Injection])`（纯函数）：可注入行的筛选、抽样可复现、三类改写的内容（OE 相邻号、MSRP 倍数范围、站点改选）、证据不动、不足 N 报错。
2. 结果库：`save_injections / injections`；`copy_for_rescore` 带上注入记录。
3. `eval.metrics.injection_eval`（纯函数）+ `evaluate(..., injections=...)`：各档计数、被拦下比例、AUROC。
4. CLI（fake）：`rfq eval inject` 建运行、收尾、0 次 LLM 调用；在其上 `rfq rescore` 后注入记录还在；`rfq eval` 报告含注入一节；`eval compare` 含新列。
5. 报告与 Dashboard：注入一节、对比表新列（含一致性测试）、评估页显示注入结果。

## 实施顺序
1. 写回 spec §7 / §13 / §14 / §15.2、GLOSSARY（注入行），单独提交。
2. seam 1 → 2 → 3 → 4 → 5。
3. 在真实源运行上 `rfq eval inject`（不花钱）→ 按注入后结果算出 self / judge 的准确调用数，报成本，经同意由用户执行两次 rescore → 我跑 `rfq eval` ×3 与 `eval compare`，结果记进本文件。

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| I1 | 复制来的运行合计成本少算（真实实验中发现：rescore 注入运行时，源运行没有 agent 调用，合计只含本运行） | 合计成本沿 `rescored_from` 一直往上加到没有源的运行（`run_eval.lineage_usage`，回归测试 `test_the_cost_of_a_run_includes_every_run_it_was_copied_from`）；spec §7、§14 措辞已改 |

## 完成记录（2026-10-10）
- fake 模式 `uv run pytest`：625 passed（合并 P10 时 609；审查修复后见下）；ruff、`mypy src`、Dashboard mypy 无问题。
- 注入：在 `run-20261010T214022Z-cf1928`（seed 43，真实 agent）上 `rfq eval inject --rows 40 --seed 7` → `run-20261010T215744Z-fd5f0f`：OE 14、MSRP 13、零售 13，0 次 LLM 调用。
- 真实 rescore（用户执行，0 错误，预计与实际调用数一致）：self `run-20261010T220811Z-805707` 288 次 $0.148；judge `run-20261010T220906Z-6153c9` 56 次 $0.128。

| 策略 | 绿 / 黄 / 红 | 注入行被拦下 | 留在绿档的错行 | 注入 AUROC | 整体 AUROC | ECE | 本运行 / 合计成本 |
|---|---|---|---|---|---|---|---|
| evidence（对照） | 88 / 56 / 56 | 45% | 22 / 40 | 0.565 | 0.819 | 0.191 | $0 / $0.452 |
| evidence_plus_self | 109 / 35 / 56 | 55% | 18 / 40 | 0.719 | 0.883 | 0.199 | $0.148 / $0.600 |
| evidence_plus_judge | 128 / 5 / 67 | 30% | 28 / 40 | 0.661 | 0.859 | 0.192 | $0.128 / $0.580 |

留在绿档的注入行按类型（OE / MSRP / 零售）：evidence 8 / 6 / 8；self 5 / 10 / 3；judge 10 / 9 / 9（judge 另把 11 行压到红档，全是注入行）。

观察：
- 证据信号几乎分不出这类“看起来合理”的错误（注入 AUROC 0.565）。
- self 最好，但只拦下一半多：对 OE 与零售有效，对 MSRP 无效（自评只问 OE 号和参考站点，不问 MSRP），绿档中的 MSRP 错行反而从 6 增到 10。
- judge 不同意时很准（压到红档的 11 行全是注入行），但它同意了更多错行，混合后把黄档里的注入行抬上了绿档；而且只审 50–79 分的行，原本就在绿档的 22 行错行它看不到。结果绿档错行最多（28）。
- 结论：三种策略都不能可靠拦下看起来合理的错误；按每次调用的效果，self 最好。

待用户决定（置信度规则的改动，不在本实验内做）：
1. 自评是否加问 MSRP（它从页面读到的价格），让自评也能发现 MSRP 错。
2. judge 是否也审绿档（`judge_band` 扩到 50–100，调用数约从 56 增到 140），以及“同意”时是否不再加分（只在不同意时降分）。
3. 以上改动后是否在同一份注入结果上再 rescore 一次比较（约 $0.3–0.5）。

## 审查后修复（review session 2026-10-10，`/code-review 95842b8`；用户同意按表处理）
- [x] **N1** `eval/inject.py` 用 `store.runs.RowOutcomes`：不改代码；spec §7 末尾写明这个例外（注入直接改写结果库的四路结果）。
- [x] **N2** `InjectionEval.bands_by_kind` 补 `Field(description=...)`。
- [x] **S1** `inject` 命令挪到 `cli/evaluate.py`；驱动复制来的运行收成 `cli/run.drive_copy`，rescore 与 inject 共用。
- [x] **S2** 注入摘要合成 `report.injection_summary`（CLI 与报告共用）；Dashboard 的中文版保留（不能 import 后端）。
- [x] **S3** 报告的档位列表改为 `get_args(Band)`。
- [x] **S4** 不改名 `rescored_from`；spec §7 注释与 GLOSSARY（重新打分）写明也包括注入产生的副本。
- [x] **L1** spec §15.2 评估页补注入一节与对比表的三列。
- [x] **L2** 复制、改写、记录放进同一个事务：`RunStore.copy_for_injection`（`save_injections` 删除）；测试 `test_an_injection_copy_is_all_or_nothing`（修复前失败）。
- [x] **L3** 被拦下只数黄档 + 红档；测试 `test_an_injected_row_without_a_band_is_not_counted_as_held_back`（修复前失败）。
- 不改（记录）：**S5** `inject.py` 用 `assert` 收窄类型，`_injectable` 已保证这些条件，实验代码；**L4** OE 注入的相邻号理论上可能与别的行撞号，影响可以忽略。

## 第三轮审查（2026-10-10，`/code-review 2f03abe`，通过；用户选择修后合并）
- [x] `cli/evaluate.py`：`drive_copy` 并入顶层导入；复用已取得的 `settings`。
- [x] `inject_errors` 只返回被改过的行（`dict` 记注入），CLI 不再过滤；测试 `test_only_the_rows_made_wrong_are_returned`。
- [x] `copy_for_injection` 的行号校验改为一条 `SELECT row_id`，放在复制之前。
- [x] `_copy_run` 的 docstring 写明会新建 `runs` 行。
- 不改（记录）：`check_rescorable` 在事务外执行，检查与复制之间若正好保存一次复核会漏过；改动前即如此，要严格需 `BEGIN IMMEDIATE`，对实验命令不值得。
- 最终：fake 模式 `uv run pytest` 627 passed；ruff、`mypy src`、Dashboard mypy 无问题；三个注入运行重新评估，数字不变。
