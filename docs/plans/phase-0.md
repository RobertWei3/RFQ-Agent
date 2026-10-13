# Phase 0 — 项目骨架、配置、LLM 层、CLI

> 补写于 P2 开始时，依据 P0 计划阶段的对话。提交：c7295b0。

## 目标与验收（spec §17）
项目骨架、配置加载、LLM factory、FakeLLM、调用追踪、CLI 框架。
验收：`uv run pytest` 通过；`rfq --help` 可用；FakeLLM 能返回结构化输出。

## 已定决策
| # | 问题 | 决定 |
|---|---|---|
| Q1 | schemas.py 写到什么程度 | 写全 §7，WorksheetRow 推迟到 P6/P8 |
| Q2 | FakeLLM 接入方式 | 继承 LangChain `BaseChatModel` 并实现 `bind_tools`，按输出 schema 名注册处理函数，agent 在 fake/真实下走同一路径 |
| Q3 | 结构化输出重试与并发 | P0 放进 LLM 层：`invoke_structured`（失败把校验错误附回 prompt，最多重试 2 次；最终失败返回失败结果、不抛异常）+ 全局并发信号量 8 |
| Q4 | 依赖 | 每个 Phase 按需加 |
| Q5 | DeepSeek 模型 | agent 用 `deepseek-flash`，judge 用 `deepseek-v4-pro` |
| Q6 | thinking 模式 | 默认关闭（`extra_body={"thinking": {"type": "disabled"}}`），按角色可配 |
| Q7 | 成本计算 | 单一保守价（高峰、缓存未命中）；`llm_calls` 另记缓存 token 数 |
| Q8 | CLI 范围 | `rfq --help`、`rfq config show`（key 只显示 set/missing）、`rfq llm ping`（真实 provider 需 `--allow-real`） |
| Q9 | Python | 固定 3.12，`requires-python>=3.11` |
| Q10 | 数据库 | 业务库 `data/rfq.db`；checkpoint（P7）单独文件；测试用临时目录 |
| Q11 | Qwen | 支持，默认新加坡 `dashscope-intl` 地址、`qwen-plus`，key 为 `DASHSCOPE_API_KEY` |
| Q12 | git | 验收后在 main 一次性提交，不 push |

## 查证的文档事实（2026-10-03）
- DeepSeek：`https://api.deepseek.com`，模型 `deepseek-flash` / `deepseek-v4-pro`；`deepseek-chat` 已不在文档中
- `langchain-deepseek` 的 `ChatDeepSeek` 默认用 function calling 做结构化输出
- `BaseChatModel.with_structured_output` 默认实现依赖 `bind_tools`
- 追踪回调：`on_chat_model_start` 拿 metadata，`on_llm_end` 读 `usage_metadata`

## 与 spec 的差异
- §18 的单价改为按模型 ID 配置（judge 用不同模型）
- 未核实价格的模型（Anthropic、Qwen）成本记 NULL；fake 记 0

## 交付
`config.py`、`schemas.py`、`llm/{factory,fake,structured,tracking,ping}.py`、`store/db.py`、`cli.py`；46 个离线测试。
