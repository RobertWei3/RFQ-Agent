# Phase 2 — intake + `oe_interchange` agent

## 目标与验收（spec §11.1、§17）
客户件号 → OE 号（含新旧替代链），输出 `OEResolution`。
验收：fake 模式测试通过；真实 LLM 跑 20 行样例输出 JSONL 供人工检查（运行前报调用次数和成本，经同意再跑）。

## 已定决策
| # | 问题 | 决定 |
|---|---|---|
| Q1 | 计划保存 | 存进仓库 `docs/plans/`，补写 phase-0/1，之后每个 Phase 都随代码提交 |
| Q2 | LLM 的角色 | 代码按 §11.1 编排工具调用，LLM 只用 `invoke_structured` 从页面文本抽取，不做自主调用 |
| Q3 | 本公司件号放哪 | `OEResolution` 新增 `our_part_no`、`our_part_source`（given / xref / none），该模型 schema_version 升到 1.1 |
| Q4 | 替代链有环 | 记为行失败 `{row_id, ok: false, stage, error}`，不输出 OEResolution；分叉冲突同样处理 |
| Q5 | status 与 match_type | 每个候选沿链走到链尾，按链尾去重：0 → not_found，1 → resolved，≥2 → multiple（current_oe 为空、链为空、候选的 oe_part_no 为各自当前号）。match_type：客户件号直接命中目录且写法相同 = exact，标准化后相同 = normalized，来自互换页 = interchange，页面原文找不到该号 = llm_inferred（保留但不参与判定） |
| Q6 | 形式 | LangGraph 子图 |
| Q7 | 补链方向 | 双向：向前追 superseded by，向后追 replaces，每方向最多 4 跳，输出完整的旧 → 新链 |
| Q8 | 抽取粒度 | 互换页 1 次调用；之后每跳把该跳搜到的全部目录页合并成 1 次调用，按页输出主体件号与替代关系；代码只保留主体件号等于所查号码的页，以过滤干扰页 |
| Q9 | 子图粒度 | 一行一次子图；P7 主图用 Send 按行 fan-out |
| Q10 | 文档语言 | 计划文档中文，代码注释英文 |
| Q11 | 人工检查辅助 | 真实 LLM 跑完后生成与 ground truth 的逐行对照表和合计（P10 正式评估另写） |
| Q12 | 输出 | `data/runs/oe_interchange_<时间>.jsonl`（git 忽略），每行 `{"row_id","ok":true,"result":…}` 或 `{"row_id","ok":false,"stage","error"}`，按 row_id 排序 |

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| Q13 | 怎么看完整推理过程 | 加本地 trace：每次运行写 `<输出>.trace.jsonl`，记录每个节点的输入输出（搜索、过滤掉的干扰页、丢弃的关系、加的边、判定）和每次 LLM 调用的完整 prompt / 原始回复；用 `rfq trace show <文件> [--row N] [--full]` 查看；数据只留在本地。LangSmith 等 RFQ 完善后再考虑 |
| Q14 | 测试与 `.env` | 测试设置 `RFQ_SKIP_DOTENV=1`，不读项目 `.env`，避免真实 key 和 provider 设置进入测试 |

## 真实 LLM 冒烟测试（deepseek-flash，20 行样例）
两次运行结果一致：OE 20/20（resolved 16/16、multiple 2/2、not_found 2/2），链 16/16，本公司件号 20/20，0 失败；每次 42 次调用、约 2.96 万输入 / 0.73 万输出 token、约 $0.018（按高峰美元单价估算）。
注意：合成页面格式规整，且干扰页在送给 LLM 前已被代码过滤，这个结果只说明流程通，不代表真实数据上的准确率。

## 查证的文档事实（2026-10-04）
- `langgraph` 1.2.12：TypedDict state，async 节点返回部分更新，`add_conditional_edges` 做循环，依赖通过 `context_schema` + `Runtime` 注入
- `langchain-mcp-adapters` 0.3.2：`MultiServerMCPClient` 不是 context manager；常驻会话用 `client.session(...)` + `load_mcp_tools`；以 ToolCall 形式调用才能拿到 `artifact["structured_content"]`

## 子图流程
`lookup_our_part`（给出的件号或 MCP 对照表）→ `search`（互换搜索 + 用客户件号查目录；超时重试 1 次）→ `extract_candidates`（LLM）→ `expand_chain`（并发搜索待查号码 → 1 次 LLM → 过滤 → 加边）↺（还有新号码且未满 4 跳）→ `build_resolution`（确定性建链与判定）

## 交付
`intake/reader.py`、`agents/{oe_interchange,fake_handlers,runner}.py`、`eval/quick_compare.py`、CLI `rfq agent oe-interchange`，以及对应测试。
