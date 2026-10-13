# Phase 3 — `oe_msrp` agent

分支：`phase-3-oe-msrp`

## 目标与验收（spec §11.2、§17）
对 `OEResolution.current_oe` 查 `msrp_lookup`，LLM 抽取 MSRP 并判断是否停产；当前号无价格时沿替代链往回试旧号并在 reasoning 注明。输出 `MSRPResult`。
验收：fake 模式测试通过；真实 LLM 跑 20 行输出 JSONL 供人工检查（先报成本、经同意）。

## 已定决策
业务规则已同步写入 RFQ-SPEC.md §11.2。

| # | 问题 | 决定 |
|---|---|---|
| Q1 | 输入 | 两种都支持：`--from-oe <P2 输出 JSONL>`（复用已有 OEResolution）或 `--input <xlsx>`（先跑 oe_interchange 再跑 oe_msrp） |
| Q2 | OE 为 multiple 的行 | 不查，`status=not_found`、`oe_part_no=null`，reasoning 写“OE 未唯一确定，待人工选择” |
| Q3 | 回退到旧号拿到价格 | `oe_part_no=current_oe`；`quote.matched_part_no`=给出价格的旧号；reasoning 写明回退；status 按该页是否标停产判为 found / discontinued |
| Q4 | LLM 粒度与防编造 | 并发搜整条链上所有号码 → 过滤干扰页 → 1 次 LLM 按页抽 {主体件号, 价格, 币种, 是否停产, 跳转到}；代码从链尾往回取第一个有价号码；价格数字必须出现在该页原文，否则丢弃并记入 trace |
| Q5 | 上游失败 / not_found | P2 行失败 → MSRP 也输出行失败（stage=upstream_oe）；P2 not_found → 直接输出 not_found，不调工具和 LLM |
| Q6 | 同号多个不同价格 | 取较高价，evidence 保留全部页面，reasoning 标“价格冲突” |
| Q7 | 非 USD | ~~原样保留币种不换算~~ → review 后改为：只采用美元价格；页面未标币种视为 USD，明确为其他币种的不采用并继续沿链回退，reasoning 注明（原系统业务全是美元） |
| Q8 | 冒烟测试输入 | 复用 P2 最近一次真实运行输出 `data/runs/oe_interchange_20261004T193845Z.jsonl` |

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| Q9 | not_found 结果的原因写在哪 | `MSRPResult` 原本没有 reasoning 字段，为落实 Q2/Q5 新增 `reasoning`，该模型 schema_version 升到 1.1（与 P2 给 OEResolution 加字段的做法一致） |
| Q10 | `--from-oe` 时怎么找 ground truth | 每次 agent 运行写 `<输出>.meta.json`（agent、run_id、input、dataset_dir、provider、model、行数）；`--from-oe` 读上游的 meta 找到原始输入，再定位 ground truth；找不到时提示用 `--truth` |
| Q11 | 共享代码 | P2 的上下文、trace、页面过滤、搜索重试、行失败抽到 `agents/common.py`，两个 agent 共用 |

## review 后的修正（第一次 review，2026-10-05）
| # | review 发现 | 处理 |
|---|---|---|
| R1 | spec §7 的 `MSRPResult`（以及 P2 的 `OEResolution`）与代码不一致 | §7 补上新增字段并标注 1.1；默认版本说明改为“有增改的模型单独升版本” |
| R2 | `RowFailure` 不在 §7 却在模块间传递 | 作为正式契约移到 `schemas.py`，写入 §7 |
| R3 | 停产判断用了该号所有价格页 | 改为只看给出所选价格的那一页 |
| R4 | 缺币种时默默当 USD | 按新的 Q7 规则：缺币种 = USD 是明确规则；非 USD 不采用 |
| R5 | 价格未通过核对时 reasoning 仍说“当前号没有价格页” | 区分“没有价格页”“页面无价格”“价格未通过原文核对”“非 USD 未采用” |
| R6 | 短路测试没断言“未调用工具和 LLM” | 补上计数工具和 `llm_calls` 计数断言，并覆盖 not_found 短路 |
| R7 | `MSRPPage.redirect_to` 没被使用 | 删除，省输出 token |
| R8 | 冲突时被拒的页不进 evidence | 维持现状并在 spec §11.2 写明：证据只含可采用的冲突价格页，未通过核对的页只记在 trace |
| — | 代码风格类（函数偏长、两个 agent 重复的路由/失败包装、对照表解析字符串、cli.py 职责多） | `select_msrp` 已拆成小函数；其余留到 P4 加第三个 agent 时统一重构 |

另：修复过程中发现一个与测试顺序有关的旧问题——`mcp.client.stdio` 在首次 import 时把 `errlog` 绑定到当时的 `sys.stderr`，若首次导入发生在 CliRunner 内会拿到没有 `fileno()` 的替身，导致启动 MCP 失败。已在 `tests/conftest.py` 提前导入；真实 CLI 运行不受影响。

## 人工检查（2026-10-05，P3 人工检查通过）
检查了 `oe_msrp_20261005T232129Z` 的第 1（普通）、3（回退）、6（OE multiple 短路）、8（停产）、10（链上无价格）行，结论都正确。据此做了两处修改：
| # | 发现 | 处理 |
|---|---|---|
| M1 | 回退时 reasoning 把“当前号没有价格页”说了两遍 | 每个号码的问题只说一次；回退说明只写“用了旧号 X 的 MSRP（supersession fallback）” |
| M2 | 停产行的 evidence 只有价格那一行，看不到停产依据 | 抽取 schema 增加 `discontinued_snippet`，停产时把该行作为额外 evidence（同一页面） |
multiple 行维持 Q2“不查价”的决定。
M1/M2 后真实 LLM 重跑：`oe_msrp_20261005T234019Z`，MSRP 20/20、0 失败、0 重试；第 8 行 evidence 含停产那一行；15 次调用、约 $0.006。

## 子图流程
`prepare`（multiple / not_found / 上游失败的短路）→ `search_chain`（并发搜链上所有号码，过滤干扰页）→ `extract_prices`（1 次 LLM）→ `select`（纯函数 `select_msrp`：从链尾往回取第一个有可信价格的号码）

## 真实 LLM 冒烟测试（deepseek-flash）
输入：P2 真实运行输出 `oe_interchange_20261004T193845Z.jsonl`（20 行样例）。
结果：MSRP 20/20（found 14/14、discontinued 1/1、not_found 5/5，其中 2 行为 OE multiple 按规则跳过），回退行 1/1；0 失败、0 重试、代码丢弃的页面 0；15 次调用、约 1.17 万输入 / 0.23 万输出 token、约 $0.006。
review 修正后（prompt 删除 redirect_to、说明未标币种填 null）重跑：`oe_msrp_20261005T232129Z`，结果相同——MSRP 20/20、回退 1/1、0 失败、0 重试、0 丢弃；15 次调用、约 1.09 万输入 / 0.23 万输出 token、约 $0.006。

同 P2：合成页面格式规整，结果只说明流程通，不代表真实网页上的准确率。

## 过程说明
CLAUDE.md 在 P3 实施中途更新为“分支 + `/tdd`”流程。更新前 `oe_msrp.py`、`select_msrp` 和 runner 已先写好、测试后补；更新后的 CLI 部分按测试先行完成。

## 交付
`agents/common.py`、`agents/oe_msrp.py`、`fake_handlers.py` 的 MSRP 处理函数、runner 的 `run_oe_msrp` / `read_oe_outcomes` / meta、`quick_compare` 的 MSRP 对照、CLI `rfq agent oe-msrp`、`tests/test_oe_msrp.py`。
