# Phase 1 — 合成数据、ground truth、内部库、MCP server、mock 工具

> 补写于 P2 开始时，依据 P1 计划阶段的对话。提交：54f2253。

## 目标与验收（spec §17）
生成 200 行数据，各难点比例符合 §6.3（误差 ±3%）；MCP Inspector 能调用全部工具。

## 已定业务规则
| # | 问题 | 决定 |
|---|---|---|
| Q1 | 一对多的正确答案 | ground truth 记可接受集合 `accepted_oes` + 主号 `primary_oe`；输出 multiple 且候选覆盖集合，或 resolved 且等于主号，都算对 |
| Q2 | 内部库与外部页面不一致时谁对 | 内部库对；外部页面被篡改（重量偏 25–40% 或材质写错）。后已写入 spec §11.4 |
| Q3 | 零售离群 | 录错/套装价：某站为其他站中位数 ×0.3–0.5 或 ×1.8–3，超出 ±40% 阈值；不能被选中 |
| Q4 | 难点配额 | 每个难点按 round(比例×N) 精确分配，可叠加；找不到 OE 的行不参与依赖 OE 的难点；分母为全部行 |
| Q5 | 本公司件号覆盖 | 询价单给出 25%，对照表可查约 50%，其余约 25% 查不到；主数据另加干扰零件 |
| Q6 | MSRP 特殊情况 | 停产 5%（页面仍有最后价格）；替代链行中 30% 当前号无价格页，需回退到旧号；约 3% 完全没有 MSRP |
| Q7 | 价格量级 | MSRP：节温器 $20–90、水泵 $60–320；零售原价为 MSRP 的 55–95%；30% 有 5–25% 折扣；约 10% 无货；landed cost 约为零售价的 25–45% |
| Q8 | 20 行样例 | 分层抽样，覆盖每种难点，多对一成组入选；提交到 `data/samples/` |
| Q9 | 件号标准化 | NFKC → 去首尾空白 → 大写 → 去掉空格和 `- _ . /`；生成器保证标准化后不碰撞 |
| Q10 | 客户 | 单一虚构客户，对照表按客户件号查 |
| Q11 | ground truth 成本 | P1 先写纯公式模块 `costing/formula.py`（逐项测试），P5 复用 |

## 查证的事实
- `langchain-mcp-adapters` 0.3.2 要求 `mcp<2`，所以锁定 `mcp` 1.x，server 用 `mcp.server.fastmcp.FastMCP`（2.x 已改名为 MCPServer）
- 本机有 Node，可用 MCP Inspector

## 实施中的理解（汇报时请你确认过）
- §11.3 选价规则：先排除离群项 → 有 exact 匹配就只看 exact → 只看有货的（全部无货则不限）→ 取 effective price 最低；实现为 `expected_selected_site`
- cycle time：零件自身值优先，品类默认值兜底
- 数据目录：按数据集分目录 `data/generated/n{N}_s{seed}/`（spec 原写 `data/generated/rfq_200.xlsx`）

## 交付
`data_gen/{params,world,render,truth,writer,audit}.py`、`costing/formula.py`、`partno.py`、`tools/`（9 个 mock 工具，干扰结果与故障注入）、`mcp_server/server.py`（4 个工具）、CLI `rfq data generate|stats`、`rfq tools search`、`rfq mcp serve`；84 个离线测试；你的独立审计 `tests/audit_p1.py` 全部通过；Inspector 已逐个调用 4 个工具。
