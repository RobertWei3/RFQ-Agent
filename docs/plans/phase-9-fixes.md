# Phase 9 — 审查后修复清单

来源：review session（2026-10-08），`/code-review a5e9c15`。§17 P9 四项验收全部通过；dashboard 测试 53 passed。

## 使用方式
- 按 `/tdd` 修，每条 bug 先写在现有代码上会失败的测试；完成后打勾并注明测试名。
- 文本替换每处断言匹配一次。

## A. Spec
- [x] **A1 改 OE 号的轮询超过 120 秒不停**（`review.py` 的定时 fragment 一旦启动就一直跑，超时后每 2 秒继续请求 API，只有整页重跑才停）。修法：每次检查得出状态（完成 / 后台已停止 / 超时 / 处理中）；fragment 内只要不是“处理中”就整页重跑，整页渲染时只有“处理中”才建 fragment，其余状态静态显示，不再轮询。（`review.pending_phase`；测试 `test_a_pending_new_oe_number_stops_polling_once_it_is_settled`。AppTest 不按时触发 fragment，“fragment 内转为整页重跑”由代码结构保证，并在浏览器里走了一次改 OE 号：处理中 → 已完成）
- [x] **A2 先读行、后读租约，顺序反了**：worker 在两次请求之间完成并退出时，会误报一次“后台已停止”，点“重新提交”得到 409。修法：先读租约，再读行。（测试 `test_a_worker_finishing_between_the_two_reads_is_not_reported_as_stopped`，修复前失败）
- [x] **A3 替代链没有高亮当前 OE 号**（计划 Q11）。当前号加粗。（`test_a_priced_row_shows_its_chain_prices_costs_signals_and_reasoning`，修复前失败）
- [x] **A4 `show_row` 的 docstring 说“也给复核队列用”**，实际没有：改正。

## C. Standards 与代码坏味道
- [x] **C1** `app.py` 的 `_page` 用 `# type: ignore` 代替返回类型（§0 第 7 条）。改为 `-> Callable[[], None]`。
- [x] **C2 重复**：金额格式化两处（`row_detail` / `review`）；“每 2 秒轮询”的 fragment 写法两处；`Json = Any` 定义两次。（`common.money`、`common.poll`；`charts` 从 `api_client` 取 `Json`）
- [x] **C3 用字符串 / 临时 dict 表示领域概念**：复核表单的状态键散落多处；“等待中的改 OE 号”是临时 dict，改成 dataclass。（表单键集中为模块常量 `ROW / ACTION / OE / MSRP / RETAIL / NOTE`；`PendingOE`）
- [x] **C4 死代码**：`review._summary` 的 `if "worksheet" in w` 分支永远不会走到。已删除。
- 不改（判断题，记录理由）：
  - `views/common.py` 管的事较多：约 130 行，都是多页共用的小件，拆开收益小。
  - `_page → render_view → 页面` 多一层：`render_view` 是 AppTest 跑单页的入口，`_page` 只是给 `st.Page` 一个有名字的函数，保留。
  - 页面之间传 `dict[str, Any]`、dashboard 不定义自己的类型：§15.2 不允许 import 后端 schema，dashboard 按 OpenAPI 读 JSON；字段改名由页面测试（注入进程内真实 API）暴露。

## 完成标准
- A、C1–C4 完成；fake 模式 `uv run pytest` 全部通过；ruff、`mypy src`、`mypy --ignore-missing-imports dashboard` 无问题。

## 完成记录（2026-10-08）
- A1–A3 的新测试先在修复前的代码上失败（A1 的状态函数原来不存在），修复后通过。
- fake 模式 `uv run pytest`：530 passed；ruff、`mypy src`、`mypy --ignore-missing-imports dashboard` 无问题。
- 浏览器（fake）：复核队列改 OE 号 → “后台处理中” → “改 OE 号已完成”，表单重置为“接受”、OE 输入框收起。
