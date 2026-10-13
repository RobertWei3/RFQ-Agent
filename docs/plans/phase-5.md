# Phase 5 — `should_cost` agent

分支：`phase-5-should-cost`

## 目标与验收（spec §11.4、§17）
按本公司件号经 MCP 取内部规格，按 `current_oe` 查外部规格页核对或补缺，确定性代码按 `cost_model.yaml` 计算完整成本栈，输出 `ShouldCostResult`。
验收：fake 模式测试通过；公式逐项单元测试通过；真实 LLM 跑 20 行输出 JSONL 供人工检查（先报成本、经同意）。

## 已定决策
业务规则已同步写入 RFQ-SPEC.md §11.4、§7、§10、§18。

| # | 问题 | 决定 |
|---|---|---|
| Q1 | 本公司件号查不到的行（200 行中约 25%） | 只用外部页面估算（原系统做法，领导当时的要求）；cycle time 用品类默认值，reasoning 注明 |
| Q2 | 本公司件号未知时品类从哪来 | 按 `current_oe` 查 OEM 目录页，与规格页放在同一次 LLM 调用中抽品类原文，代码映射（含 `thermostat` → thermostat，含 `pump` → water_pump）；目录页没有再看询价单 description（同一映射）；都没有 → `insufficient_data` |
| Q3 | OE 为 multiple / not_found / 上游失败 | 以本公司件号为主：已知就取内部规格，与 OE 状态无关；外部查询只在 OE `resolved` 时做（multiple 不选主号，not_found 无号可查，reasoning 注明）；上游失败 → `RowFailure(stage=upstream_oe)`（对照表查到的本公司件号在 OEResolution 里） |
| Q4 | 内部完整时是否查外部 | OE `resolved` 就查：内部完整时核对（`both_agree` / `conflict`，计算用内部值），缺字段时补；每行最多 1 次 LLM 调用 |
| Q5 | `spec_source` 合成 | 逐字段（材质、重量）判 both_agree / conflict / internal / external / missing；任一 missing → `missing` + `insufficient_data`；否则取最弱：`both_agree > internal > external > conflict` |
| Q6 | 重量一致的容差 | 换算为 kg 后相对误差 ≤ 5%，配置 `pipeline.spec_weight_tolerance: 0.05` |
| Q7 | 材质写法归类 | LLM 抽材质原文并从已知 key 中选一个（都不像填 `other`），代码核对原文在页面上；已知 key 由新增 MCP 工具 `list_materials` 从内部库取 |
| Q8 | 其他计算输入缺失 | 材质 `other`、材质单价查不到或非 USD、品类未知 → `insufficient_data`，`breakdown` 为空，`spec_source` 照常给出；零件无 cycle time → 品类默认值 |
| Q9 | 外部规格页查哪些号 | 只查 `current_oe`，不查替代链旧号（替代件常改设计，旧号规格不能代表当前件） |
| Q10 | 防编造核对 | page_id 在搜索结果中且原文提到 `current_oe`；重量“数字 + 单位”紧挨着出现在原文中，单位只认 kg / g / lb（含 kgs、lbs，不分大小写），换算后 > 0；材质原文、品类原文须出现在页面中。不通过的字段视为外部没有，记 trace |
| Q11 | 多个外部页面彼此不一致 | 一致则合并（数值取 page_id 最小的页，evidence 保留全部）；不一致则该字段外部值不可用：内部有值 → `conflict` 用内部值；内部没有 → `missing`，reasoning“外部页面之间不一致，需人工确认”；目录页品类不一致 → 品类未知 |
| Q12 | `ShouldCostResult` 加字段 | 新增 `category`、`cycle_time_min`、`evidence: list[Evidence] = []`，schema_version 升到 1.1；`material` / `weight_kg` 填实际用于计算的值，`insufficient_data` 时已知字段照填 |
| Q13 | 失败处理 | 外部仅用于核对时，搜索 / 抽取失败不算行失败，用内部值（`spec_source=internal`），reasoning 注明；外部用于补数据时失败 → `RowFailure(stage=search_spec / extract_spec)`；MCP 出错 → `RowFailure(stage=internal_spec)` |
| Q14 | CLI 与冒烟输入 | `rfq agent should-cost`，`--from-oe <P2 JSONL>`（经上游 `.meta.json` 找到原始询价单，重跑 intake 得到 `RFQLine`；找不到就报错提示加 `--input`）或 `--input <xlsx>`；输出 `data/runs/should_cost_<时间>.jsonl` + `.trace.jsonl` + `.meta.json`；冒烟复用 `data/runs/oe_interchange_20261004T193845Z.jsonl` |
| Q15 | 人工检查对照表 | 每行查：status、品类、材质 key、重量（≤1%）、`spec_source`（对照脚本按 ground truth 独立推导预期值）、FOB 与 landed cost（零件 cycle time 的行 ≤1% 算对；品类默认 cycle time 的行只报误差不判对错）。汇总：全对行数、规格正确数、spec_source 正确数、成本正确数、landed cost MAPE（两组分开）、insufficient_data 数、失败数 |

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| I1 | 冒烟输入的 P2 运行早于 `.meta.json`，没记录用的哪份询价单 | `should-cost` 新增 `--rfq <xlsx>`：与 `--from-oe` 一起用时显式指定询价单（优先于 meta 记录），按 row_id 配对，配不上报错；与 `--input` 同用报错；写入 meta 的 `rfq` 字段，未给 `--truth` 时据此找 ground truth |
| I2 | ground truth 没有询价单 description，推导不出“description 兜底品类” | 对照表在预期品类为空时不判品类（agent 可能从 description 取到）；OE 为 multiple 的行即使带 `spec_conflict` 标签，也不查外部页，预期为 `internal` |
| I3 | 对照表成本正确率的分母 | 与 P4 I3 一致，失败行计入分母 |

## 实施中的理解（不需另行决定）
- 公式逐项单元测试：P1 的 `test_formula.py` 把各项写在一个测试里，P5 拆成每项一个参数化用例，满足 §17“公式逐项”。
- `breakdown` 各项保留原始浮点值不取整（取整留给 P8 Excel 显示）。
- FakeLLM 新增规格页 / 目录页处理函数：正则抽取，材质用简单关键词归类，不读 ground truth。

## review 后的修正（第一次 review，2026-10-07）
| # | review 发现 | 处理 |
|---|---|---|
| R1 | 询价单给出的本公司件号在内部库查不到，spec 未定，代码自行当作“件号未知” | 你确认按“件号未知”处理（选 A），写入 §11.4，补测试 |
| R2 | Q8 的“材料价格查不到 / 非 USD”缺 agent 层测试，也没测 `spec_source` 是否照常给出 | 补测试（品类未知已有测试，补上 `spec_source` 断言） |
| R3 | 重量“数字 + 单位”的正则允许换行；`1,200 g` 这类千分位写法核对不上 | 只允许空格 / tab；支持千分位 |
| R4 | `PartSpec` / `MaterialPrice` / `MaterialList` 从 `mcp_server` 导入，不在 §7 | 移到 `schemas.py`，写入 §7；MCP 返回因此多了 `schema_version` 字段 |
| R5 | 来源标签、强弱顺序、“缺失优先否则取最弱”在规则层和对照脚本各写一份 | `schemas.SpecSource` + `weakest_spec_source` 统一，两处共用 |
| R6 | fake handler 另写了一份重量正则 | 共用 `WEIGHT_PATTERN` |
| R7 | 材料价格查询出错记成 `internal_spec` | 改为 `material_price` |
| R8 | `1e-9` 重复出现；`_spec_notes` 按字段名取外部值；`category` 字段说明漏了“询价单描述” | 全部处理 |
| R9 | （自查）insufficient_data 的行仍写“category default cycle time … used” | 没算成本时不写这句，`cycle_time_min` 也不填默认值 |
| R10 | （人工检查）换算出的重量带浮点尾数（如 `0.41200000000000003`） | 换算成 kg 后立即 ROUND_HALF_UP 到 3 位小数（克），比较、计算、输出都用取整值（你选 A）；写入 §11.4 |
| — | `run_price_agent` 背着 should_cost 专用分支；入口签名与其他 agent 不同；`mcp_our_part_lookup` 是薄包装 | 维持现状：P7 接 orchestrator 时统一整理；入口多了 RFQ 行这个输入，签名不同是合理的 |
| — | reasoning 是英文、spec 原文是中文 | 沿用 P3/P4 的英文惯例 |

## review 后的修正（第二次 review，2026-10-07）
| # | review 发现 | 处理 |
|---|---|---|
| R11 | 内部记录存在但品类为空的情况没决定过，代码直接 insufficient_data | 你确认按“内部缺什么就从外部补什么”处理（选 A）：再查目录页，取不到看 description，其余用内部值；写入 §11.4，补测试 |
| R12 | 内部材料价格缺币种时默认当 USD，这条规则只为外部页面定过 | 去掉默认值：内部价格必须标明 USD（DB 列 NOT NULL，实际行为不变），写入 §7 注释 |
| R13 | §7 的 `PartSpec` 等三个模型没写默认值，与代码不一致 | 补上 `= None` |
| R14 | 外部页面之间不一致且内部也没有值时，又追加一句“not available internally or externally” | 只写“disagree … needs manual review” |
| R15 | Standards：比较规则写两遍；`_category` / `_breakdown` 往传入的 notes 里追加；`FieldSource`、`_n_errors` 只是转手；`SPEC_WEIGHT_TOLERANCE` 与 agent 配置同名易混；fake 与生产都有 `_material_key` | 比较规则统一为 `values_agree` / `weights_agree`；两个函数改为返回 (值, notes)；删别名；改名 `EVAL_WEIGHT_TOLERANCE`、`_guess_material_key` |
| — | `part` 与 `external` 总是一起传；`hits` 与 `pages` 存了两份页面；`PriceAgentRun` 名字已不准、带 should_cost 专用分支；fake 复用生产的重量正则，测不出编造 | 维持现状：前两项是轻微写法问题（`hits` 渲染 prompt 时要用标题）；命名与分支留到 P7 接 orchestrator 时统一整理；防编造由规则层的手写用例覆盖，fake 只负责产出能通过核对的结果 |

## 真实 LLM 冒烟测试（deepseek-flash，2026-10-07）
输入：P2 真实运行输出 `oe_interchange_20261004T193845Z.jsonl`，`--rfq data/generated/n200_s42/rfq_sample_20.xlsx`，`--truth` 指定样例 ground truth。
输出：`data/runs/should_cost_20261007T050824Z.jsonl`。
结果：全对 20/20；规格 20/20；spec_source 20/20（both_agree 7、internal 4、external 4、conflict 2、missing 3）；成本 15/15（零件 cycle time 的行 landed cost 误差 0.0%）；本公司件号未知的 2 行用品类默认 cycle time，landed cost 误差 20.7% 和 13.4%（MAPE 17.0%，符合预期，不判对错）；insufficient_data 3 行（1 行本公司件号未知且无外部规格页，2 行 OE multiple 且本公司件号未知）；0 失败、0 重试、代码拒收的抽取结果 0。14 次调用，约 1.09 万输入 / 0.24 万输出 token，约 $0.006（运行前估计 ~$0.005，上限 ~$0.02）。
review 修正后重跑：`should_cost_20261007T053823Z`，结果相同——全对 20/20、成本 15/15、0 失败、0 重试、0 拒收；insufficient_data 行不再写默认 cycle time；14 次调用、约 1.09 万输入 / 0.24 万输出 token、约 $0.006。
重量取整与第二次 review 修正后再跑：`should_cost_20261007T055911Z`（供人工检查的最终版本），结果相同——全对 20/20、成本 15/15、0 失败、0 重试、0 拒收；外部补来的重量均为 3 位小数；14 次调用、约 $0.006。
LLM 正确归类了 `GG25 iron`、`grey cast iron` 等写法，磅换算由代码完成。
同 P2–P4：合成页面格式规整，结果只说明流程通，不代表真实网页上的准确率。

## 子图流程
`prepare`（上游失败短路；取本公司件号：询价单给出或 OEResolution）→ `internal_spec`（MCP `get_part_spec`、`list_materials`）→ `search_external`（OE resolved 时并发查 `external_spec_search`，本公司件号未知时再查 `oem_catalog_search`；过滤干扰页）→ `extract_spec`（1 次 LLM，没有页面就跳过）→ `estimate`（纯函数：核对 → 逐字段合并 → spec_source → 品类 / cycle time / 材质单价 → `compute_breakdown`）

## 实施顺序
1. 用 `/tdd`：纯函数（单位换算与核对、逐字段合并、spec_source、外部页合并）→ MCP `list_materials` → 子图 → fake handler → runner / CLI → `quick_compare` 成本对照（含独立推导的预期 spec_source）。
2. 报冒烟测试的调用次数和成本，经同意后跑真实 LLM。
