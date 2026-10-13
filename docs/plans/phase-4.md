# Phase 4 — `retail_price` agent

分支：`phase-4-retail-price`

## 目标与验收（spec §11.3、§17）
对 `current_oe` 和替代链上的全部号码并发查询 5 个零售站点，抽取价格，代码做匹配、离群检测和选价，输出 `RetailResult`。
验收：fake 模式测试通过；离群检测单元测试通过；真实 LLM 跑 20 行输出 JSONL 供人工检查（先报成本、经同意）。

## 已定决策
业务规则已同步写入 RFQ-SPEC.md §11.3 和 §7。

| # | 问题 | 决定 |
|---|---|---|
| Q1 | OE 为 multiple / not_found / 上游失败的行 | 与 P3 相同：multiple 不查（reasoning“OE 未唯一确定，待人工选择”）；not_found 直接输出空结果，不调工具和 LLM；上游失败记为 `RowFailure(stage=upstream_oe)`。短路逻辑抽到 `agents/common.py` 与 oe_msrp 共用 |
| Q2 | LLM 的角色与调用粒度 | LLM 只抽取页面事实，代码决定匹配。代码并发搜 5 个站点 × 链上每个号码，过滤掉没提到链上号码的干扰页 → 每行 1 次 LLM 调用，按页抽 {page_id, 标题件号原文, 原价, 折扣价, 币种, 是否有货, 价格片段} → 代码按件号比对定 `match_quality` 并为每个站点选一条。`PriceQuote.reasoning` 由代码拼出匹配结论并附上 LLM 理由 |
| Q3 | `match_quality` 定义 | exact = 写法与 `current_oe` 完全相同；normalized = 标准化后相同；fuzzy = 挂在替代链旧号下（reasoning 注明旧号）。标准化后不在链上的件号丢弃 |
| Q4 | 同一站点多条匹配 | match_quality（exact > normalized > fuzzy）→ 有货优先 → `effective_price` 最低 → page_id；reasoning 注明“同站点有 N 条匹配，选了 X”，落选页面记入 trace |
| Q5 | 离群检测 | 比较 `effective_price`；中位数取该行全部报价（含无货、fuzzy）；`\|p − m\| / m > retail_outlier_threshold`（严格大于）；报价少于 3 条不检测，reasoning 注明 |
| Q6 | `selected` 规则 | 排除离群 → 按 match_quality 分层取最高的非空层 → 有货优先（全部无货则不限）→ 最低 `effective_price` → 站点名。同步修改 P1 的 `expected_selected_site`；agent 与 ground truth 分开实现，加一致性测试 |
| Q7 | 价格核对、币种、折扣 | 原价、折扣价都须出现在页面原文，否则整页丢弃并记 trace；只采用 USD（未标币种视为 USD），不换算；`discount_pct` = (原价 − 折扣价) / 原价 × 100，保留 1 位小数；折扣价 ≥ 原价时忽略折扣价；只有折扣价时原价、折扣率为空，effective 取折扣价 |
| Q8 | 整行结论写在哪 | `RetailResult` 新增 `reasoning: str = ""`，schema_version 升到 1.1，更新 §7。不加 `status`（quotes 为空即未找到） |
| Q9 | 输入与冒烟测试 | CLI `rfq agent retail-price`，支持 `--from-oe <P2 JSONL>` 或 `--input <xlsx>`；输出 `data/runs/retail_price_<时间>.jsonl` + `.trace.jsonl` + `.meta.json`。冒烟输入复用 `data/runs/oe_interchange_20261004T193845Z.jsonl`；代码写完后报调用次数和成本，经同意再跑 |
| Q10 | P3 遗留的重构 | 在 retail agent 之前单独提交，行为不变、测试断言不变：(a) `fail` / `ok_or_end` / 子图运行包装 / 上游短路抽到 `common.py`；(b) `cli.py` 拆成 `cli/` 包，每组命令一个模块；(c) `quick_compare` 的汇总改用结构化字段（status、failed、tags 列表），不再解析显示字符串；(d) 长函数改到哪拆到哪 |
| Q11 | 人工检查对照表 | 每行查六项：站点有无报价、`effective_price`（相对误差 ≤1%）、`in_stock`、`match_quality`、离群站点集合、`selected` 站点。OE 跳过的行单独计数。汇总：全对行数、站点价格正确数、离群正确行数、selected 正确数、跳过数、失败数 |
| Q12 | 单个站点搜索失败 | 只缺该站点，其他站点照常出结果，reasoning 注明并记 trace；同一站点任一号码重试后仍失败则整站视为失败；5 个站点全部失败才记为行失败 |
| Q13 | 页面未写库存 | `in_stock` 输出 null（不改成 false）；选价时视为无货；reasoning 注明“页面未写库存状态” |

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| I1 | 零售标题把件号按空格拆成三段（如 `6F8 766 376W`），`page_mentions` 只认 1–2 个相邻 token，导致页面被当成干扰页过滤掉 | `common.page_mentions` 扩到最多 4 个相邻 token 拼接（P2/P3 共用，全部原有测试通过） |
| I2 | LLM 的 reasoning 放哪 | 放在整行的 `RetailResult.reasoning`（末尾“llm: …”），不在每条 `PriceQuote.reasoning` 里重复 5 次；每条报价的 reasoning 只写代码的匹配结论。与 Q2 原文略有出入，已确认 |
| I3 | 对照表的站点价格分母 | 失败行的预期站点也计入分母，失败会拉低站点价格正确率 |
| I4 | `expected_selected_site` 改分层 | ground truth 的 `TruthListing.match_quality` 只有 exact / normalized，没有 fuzzy，所以改动对数据无影响；重新生成 200 行数据，`ground_truth.jsonl` 逐字节相同 |
| I5 | 项目 `.env` 写了 `RFQ_LLM_PROVIDER="fake"` | 第一次运行因此实际用了 FakeLLM（输出已删除）；真实运行用命令行 `RFQ_LLM_PROVIDER=deepseek` 覆盖（`.env` 以 override=False 加载） |
| I6 | 第 8 行“Ships today”被 LLM 判为未写库存 | “Ships today”算有货：写入 spec §11.3，并加进抽取 prompt |

## review 后的修正（第一次 review，2026-10-06）
| # | review 发现 | 处理 |
|---|---|---|
| R1 | 价格核对只要页面任意位置有该数字就通过，件号里的数字、“12,000 miles”都能让编造价格过关（P3 同样） | 共用核对改为“金额形式”：带货币标记，或价格行中的独立数字；件号内、带单位的数字不算。oe_msrp 同步收紧。写入 §11.2、§11.3 |
| R2 | 中位数为 0 时除零 | 价格必须 > 0，≤ 0 的页不采用（MSRP 同样，继续回退）；`detect_outliers` 另加中位数 ≤ 0 不检测的兜底 |
| R3 | 全部离群时 `selected` 规则未定 | 不选，reasoning 写明无可靠参考价、需人工确认。写入 §11.3 |
| R4 | `discount_pct` 用 `round()`（银行家舍入 + 浮点误差） | 改为 Decimal ROUND_HALF_UP。写入 §11.3 |
| R5 | Standards：与 oe_msrp 重复、用字符串表示失败、`MatchQuality` 重复定义、`Dropped` / `_empty` 命名、站点名字符串拼接、函数偏长；两个 CLI 命令重复 | 全部处理（函数偏长部分处理），只改写法不改行为，单独提交 |
| — | exact 拿 `chain[-1]` 比，而不是 `current_oe` | 改为与 `current_oe` 比较 |
| — | 缺“同站点只有一个号码失败，整站算失败”的测试 | 补测试 |
| — | spec 之外的两条丢弃规则（未知 page_id、标题件号不在原文） | 写入 §11.3 |

## 真实 LLM 冒烟测试（deepseek-flash，2026-10-06）
输入：P2 真实运行输出 `oe_interchange_20261004T193845Z.jsonl`（20 行样例），`--truth` 指定样例 ground truth。
输出：`data/runs/retail_price_20261006T081732Z.jsonl`。
结果：全对 19/20；站点价格 78/78；离群 20/20；selected 20/20；OE 未唯一确定跳过 4 行；0 失败、0 重试。16 次调用，约 2.09 万输入 / 0.81 万输出 token，约 $0.016（运行前估计 ~$0.011，上限 ~$0.03）。
唯一不全对的是第 8 行：retail_2、retail_5 页面写的是“Ships today”，LLM 判为“未写库存”（null），ground truth 为有货；selected 仍然正确（retail_3 明确有货且最低价）。其他行的“Ships today”判对了，属于 LLM 对模糊措辞判断不一致。
同 P2/P3：合成页面格式规整，结果只说明流程通，不代表真实网页上的准确率。

## 子图流程
`prepare`（共用的上游短路）→ `search_sites`（5 站点 × 链上号码并发搜索，过滤干扰页，记录失败站点）→ `extract_listings`（1 次 LLM）→ `select`（纯函数：核对价格 → 每站点选一条 → 离群检测 → `select_retail`）

## 实施顺序
1. 重构提交（Q10），全部现有测试通过。
2. 用 `/tdd` 实现：纯函数（匹配、折扣、离群、选价）→ 子图 → fake handler → runner / CLI → `quick_compare` 零售对照；修改 `expected_selected_site` 并加一致性测试。
3. 报冒烟测试的调用次数和成本，经同意后跑真实 LLM。
