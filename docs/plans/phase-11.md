# Phase 11 — README + design-notes 模板

分支：`phase-11-readme`（从 main 2358fbd 建）

## 目标与验收（spec §16、§17）
`README.md`：项目简介、架构图、快速开始（fake 模式 3 条命令跑通）、配置说明、评估结果摘要。`docs/design-notes.md`：只生成模板，内容由用户本人填写。
验收：新环境按 README 三条命令能跑通 fake 模式。

## 开工前查到的事实（2026-10-12，临时目录全新 clone，fake 模式）
- `data/samples/rfq_sample_20.xlsx` 已入库；`data/generated/n200_s42/`（内部库、模拟页面、ground truth）被 git 忽略，全新 clone 必须先生成数据。
- `uv sync` 约 1 秒（uv 缓存已有），`rfq data generate` 约 6 秒，`rfq run` 20 行约 4 秒：101 次 fake 调用、$0，绿 11 / 黄 5 / 红 4，`row correct 20/20`，产出 `data/runs/run_<id>.xlsx` 与 `.jsonl`。
- `rfq data generate` 会重写已入库的样例 xlsx：内容不变，但 `docProps/core.xml` 的修改时间变成当前时间（`writer.py` 设了固定时间戳，被 openpyxl 保存时覆盖），全新 clone 跑完快速开始后 `git status` 显示该文件被修改。

## /grilling 决定（2026-10-12）
Q1 起用户授权全部按推荐方案定，计划整体交用户审阅。

| # | 问题 | 决定 |
|---|---|---|
| Q1 | 三条命令 | ① `uv sync` ② `uv run rfq data generate` ③ `RFQ_LLM_PROVIDER=fake uv run rfq run --input data/samples/rfq_sample_20.xlsx`。环境变量只加在调 LLM 的那条上，不需要 `.env`。README 在命令下贴实际输出，并说明产物（`data/runs/run_<id>.xlsx` 着色工作表、`.jsonl`）。前置条件只写一行：已装 uv（Python 由 uv 按 `.python-version` 管理） |
| Q2 | 架构图 | Mermaid flowchart（GitHub 直接渲染），粒度同 spec §4：intake → oe_interchange → [oe_msrp ∥ retail_price ∥ should_cost] → 实体消解 → 置信度 → 复核 → 输出；旁边画出 mock 工具、MCP server（内部库 SQLite）、结果库、FastAPI → Dashboard；用两个子图框标出“批图（每批 100 行）”与“收尾图（全表）”。图下 3–4 句文字说明分批与收尾的分工 |
| Q3 | 配置说明 | 一张 `settings.yaml` 各节的表（llm / pipeline / confidence / storage / data / tools，每节一行“管什么 + 最常改的键”），`cost_model.yaml` 一句（费率均为示意值）；环境变量表：`RFQ_LLM_PROVIDER`、`DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `DASHSCOPE_API_KEY`、`RFQ_CONFIG_DIR`、`RFQ_SKIP_DOTENV`、`RFQ_API_URL`（Dashboard）；真实 provider 一小节：`cp .env.example .env` 填 key，命令加 `--allow-real`（run / resume / rerun-failed / rescore / api serve），运行前会按角色打印 provider / model 与预计调用数；贴一段**不加 `--allow-real` 时被拦下**的实际输出（不调 LLM）。不逐键复述 §18，详细规则指向 RFQ-SPEC.md |
| Q4 | 评估结果摘要 | 只用 docs/plans 已记录的真实数字：① P10 200 行三策略表（`phase-10.md`：绿/黄/红、行正确、可直接使用、ECE、AUROC、本运行与合计成本、每千行成本），并写明这份合成数据各阶段全对、比不出策略；② 错误注入实验表（`phase-10-llm-checks.md`：evidence、新 self、旧 judge、新 judge 的被拦下比例、留在绿档的错行、注入 AUROC、本运行成本）；③ 默认策略为何是 `evidence_plus_judge`（40 行注入错行只有 6 行留在绿档、压到红档的全是注入行、每千行约多 $1.65）；④ 局限：合成数据、单一模型、样本 200 行。每张表注明出处文件与运行 id 所在位置 |
| Q5 | 其他用法要不要写 | 写一节“更多用法”，每项一个小标题 + 一条 fake 模式实际跑过的命令 + 一两句说明（输出只贴关键几行）：评估（`rfq eval --run`）、策略对比（`rfq rescore` + `rfq eval compare`）、错误注入（`rfq eval inject`）、人工复核（`rfq run --review`，只写说明不贴交互）、resume / rerun-failed、单个 agent（`rfq agent ...`）、API（`rfq api serve` + `curl /runs` + OpenAPI 地址）、Dashboard（`uv run streamlit run dashboard/app.py`）、MCP server（`rfq mcp serve`）。另有“开发”一节：`uv run pytest`（写实际通过数）、ruff、mypy |
| Q6 | README 语言 | 中文（与 RFQ-SPEC、GLOSSARY、plans 一致），命令、字段名、文件名保持英文原样；术语用 GLOSSARY 的叫法 |
| Q7 | README 其他内容 | 项目简介（只写 spec §1 已有的话，不补原系统细节；后由 I5 改写）、非目标（数据源全是 mock）、技术栈一行、仓库导览（RFQ-SPEC.md、GLOSSARY.md、docs/plans/、docs/design-notes.md、目录结构简表） |
| Q8 | design-notes 模板 | spec §16 的 8 个章节标题，每节下一两句“这里写什么”的提示（引导性问题，如“原系统在平台上由哪些步骤组成、各用什么工具”），不写任何原系统内容；第 6 节提示可引用 README 的评估摘要；第 7 节列出 spec 给的 6 个维度作为小标题提示。文件顶部注明“本文由作者本人填写” |
| Q9 | 样例 xlsx 被改写 | 修（按 /tdd，属于 P1 已有意图的 bug：`writer.py` 设了固定时间戳但没生效）：写出后把 `docProps/core.xml` 的修改时间改回固定值，zip 条目时间也固定，使同参数重复生成的 xlsx 字节相同。测试：生成两次字节相同；生成的样例与入库文件字节相同。入库的样例 xlsx 若因此变化则一并提交（内容不变） |
| Q10 | 验收 seam | 自动测试 `tests/test_readme_quickstart.py`：README 中用 `<!-- quickstart:begin -->` / `<!-- quickstart:end -->` 包住一个 ```bash 代码块，测试抽出其中三条命令（断言恰好三条）；把仓库的“全新 clone 内容”（`git ls-files --cached --others --exclude-standard`，即提交后 clone 会有的文件，含未提交的 README）复制到 `tmp_path`，并在其中 `git init` + 提交；按顺序用 `bash -c` 执行，环境去掉 `VIRTUAL_ENV`、各 API key 与 `RFQ_LLM_PROVIDER`，设 `RFQ_SKIP_DOTENV=1`（不读仓库外的 `.env`；第三条自带 fake）。断言：每条退出码 0；`data/generated/n200_s42/internal.db` 存在；`data/runs/run_*.xlsx` 与 `.jsonl` 各一个；输出含 `row correct 20/20`；`git status --porcelain` 为空（Q9）。没有 `uv` 或 `git` 时 skip。约 15 秒，放在默认 `uv run pytest` 里 |
| Q11 | 手工验证 | 提交后在临时目录 `git clone` 分支，照 README 原样执行三条命令，完整输出贴进本文件“全新环境验证”一节 |
| Q12 | README 其他命令输出的真实性 | “更多用法”与配置一节的每条命令都在上面那个全新 clone 里 fake 模式实际执行，README 只贴实际输出的节选（run_id 等随时间变的值照实际），执行记录（命令 + 退出码）记进本文件。真实 provider 只演示被拦下的输出 |
| Q13 | design-notes 测试 | `tests/test_docs.py`：`docs/design-notes.md` 含 §16 的 8 个章节标题且顺序一致 |
| Q14 | 写回文档 | spec §16 写明快速开始的三条命令与 README 各节；§5 目录树的 `SPEC.md` 改为 `RFQ-SPEC.md`；GLOSSARY 无新术语；`docs/plans/README.md` 加 P11 一行 |

## TDD 测试接口（seam）
1. `data_gen.writer`（Q9）：同参数生成两次，样例 xlsx 字节相同。
2. `tests/test_readme_quickstart.py`（Q10）：README 三条命令在全新副本中跑通。
3. `tests/test_docs.py`（Q13）：design-notes 模板章节。

## 实施顺序
1. 写回 spec §16、§5，单独提交。
2. seam 1（修 xlsx 时间戳）。
3. 写 `docs/design-notes.md` 模板 → seam 3。
4. 写 README 快速开始节 → seam 2 跑通。
5. 在全新 clone 里实际跑“更多用法”与配置的命令，写 README 其余各节（架构图、配置、评估摘要、更多用法、开发）。
6. ruff、mypy、全量 `uv run pytest`；提交后按 Q11 手工验证，输出贴进本文件。
7. 停下汇报，等 review。

不调用真实 LLM。

## 实施中追加的决定
| # | 问题 | 决定 |
|---|---|---|
| I1 | 验收测试要不要设 `RFQ_SKIP_DOTENV=1`（Q10） | 不设。副本只含不被忽略的文件，`.env` 不在其中，和真正的全新 clone 一样；测试只去掉开发者 shell 里可能带的 `VIRTUAL_ENV`、`UV_PROJECT_ENVIRONMENT`、`RFQ_LLM_PROVIDER`、`RFQ_CONFIG_DIR` 与三个 API key |
| I2 | `rfq data generate --ratio ...` 不换 seed 会覆盖默认数据集 `n200_s42` 和入库样例（输出目录只按行数和 seed 命名） | README 的例子改为 `--seed 43 --ratio supersession=0.3`，并写明调整比例要换 seed 或用 `--out`。不改代码（不在本 Phase 范围），是否让生成器拒绝覆盖由用户决定 |
| I3 | Q9 的实现方式 | `writer._save_reproducibly`：经 `ExcelWriter` 写到内存，再把每个 zip 条目的时间固定为 `_FIXED_TIMESTAMP` 重新打包（`Workbook.save` 会把保存时间写进 `docProps/core.xml` 和 zip 条目）。入库样例按新写法重新生成，单元格内容不变（已逐格比较）。测试 `test_regenerating_the_sample_leaves_the_committed_workbook_unchanged`（修复前失败） |

## 完成记录（2026-10-12，fake 模式）
- `uv run pytest`：638 passed（P10 合并时 635；新增样例逐字节相同、design-notes 章节、README 快速开始验收各 1 个），约 4 分钟；`ruff check src tests dashboard`、`ruff format --check`、`mypy src`、`mypy --ignore-missing-imports dashboard` 无问题。
- `tests/test_readme_quickstart.py` 约 10 秒（uv 缓存已有依赖时）。
- README 中的命令都在全新 clone（`scratchpad/fresh`）中用 fake 实际执行过，退出码均为 0（真实 provider 被拦下那条为 2，按预期）：`rfq run`（不带 fake，被拦下）、`rfq config show`、`rfq runs show`、`rfq eval --run` ×6、`rfq rescore`（evidence、evidence_plus_self、在注入运行上 evidence_plus_judge）、`rfq eval compare` ×2、`rfq eval inject --rows 6 --seed 7`、`rfq rerun-failed`（nothing to rerun）、`rfq agent oe-interchange --limit 3`、`rfq run --review`（输入 `q` 暂停）与 `--resume`、`rfq api serve`（8000 端口：`GET /runs`、`GET /runs/{id}`、`GET /docs` 200、`POST /runs` 202 后 4 秒内 completed、`GET rows?band=red`、`GET worksheet.xlsx` 200）、`streamlit run dashboard/app.py`（`/_stcore/health` 为 ok）、`rfq mcp serve`（stdin 关闭后退出 0）、`rfq data generate --n-parts 8000 --seed 42`、`--seed 43 --ratio supersession=0.3`、`rfq run --dataset data/generated/n8000_s42 --limit 20`。README 只贴输出节选，绝对路径缩写为相对路径。
- 评估摘要的数字逐项取自 `phase-10.md`、`phase-10-llm-checks.md`；没有调用真实 LLM。

## 全新环境验证（Q11）
提交 7aee948 后 `git clone -b phase-11-readme` 到临时目录，从 README 的快速开始块中原样取出三条命令依次执行（去掉 `VIRTUAL_ENV`、`RFQ_LLM_PROVIDER` 与 API key；路径缩写为相对路径，`uv sync` 的逐包安装行省略）：

```text
$ uv sync
Using CPython 3.12.15 interpreter at: /opt/homebrew/opt/python@3.12/bin/python3.12
Creating virtual environment at: .venv
Resolved 122 packages in 2ms
   Building rfq-agent @ file:///.../verify
      Built rfq-agent @ file:///.../verify
Prepared 1 package in 350ms
Installed 115 packages in 222ms
[exit 0]
$ uv run rfq data generate
Dataset written to data/generated/n200_s42
Sample written to data/samples/rfq_sample_20.xlsx
rows=200  (ratios measured from pages, internal DB and workbook)
difficulty                  target  measured   ratio    diff
supersession                 15.0%        30   15.0%   +0.0%
one_to_many                  10.0%        20   10.0%   +0.0%
many_to_one                  10.0%        20   10.0%   +0.0%
oe_not_found                  8.0%        16    8.0%   +0.0%
retail_missing               15.0%        30   15.0%   +0.0%
retail_outlier               10.0%        20   10.0%   +0.0%
retail_variant_spelling      20.0%        40   20.0%   +0.0%
internal_spec_missing        15.0%        30   15.0%   +0.0%
spec_conflict                 5.0%        10    5.0%   +0.0%
dirty_input                   5.0%        10    5.0%   +0.0%
extra tags: {'msrp_discontinued': 10, 'msrp_fallback': 9, 'msrp_missing': 6}
our part reachability: {'given': 50, 'unreachable': 50, 'xref': 100}
corpus sizes: {'external_spec': 146, 'interchange': 188, 'msrp': 214, 'oem_catalog': 250, 'retail_1': 166, 'retail_2': 158, 'retail_3': 169, 'retail_4': 159, 'retail_5': 161}
[exit 0]
$ RFQ_LLM_PROVIDER=fake uv run rfq run --input data/samples/rfq_sample_20.xlsx
LLM calls: oe_interchange (fake/fake) expected ~60, upper bound 300
LLM calls: oe_msrp (fake/fake) expected ~20, upper bound 60
LLM calls: retail_price (fake/fake) expected ~20, upper bound 60
LLM calls: should_cost (fake/fake) expected ~20, upper bound 60
LLM calls: judge (fake/fake) expected ~20, upper bound 60
rows=20 strategy=evidence_plus_judge
run_id=run-20261013T053116Z-5ca616  (resume with: rfq run --resume run-20261013T053116Z-5ca616)
batches: completed 1
rows 20: green 11, yellow 5, red 4
LLM usage: calls=101 errors=0 input_tokens=35890 output_tokens=7515 cost_usd=0.0
worksheet: data/runs/run_run-20261013T053116Z-5ca616.xlsx
rows: data/runs/run_run-20261013T053116Z-5ca616.jsonl
row correct 20/20 (by band: green 11/11, yellow 5/5, red 4/4)
[exit 0]
$ git status --porcelain
（无输出：工作区干净）
```

## 审查后修复（review session 2026-10-12，`/code-review 2358fbd`，没有阻塞项；用户同意按表处理）
- [x] **F1** `_save_reproducibly` 用了 openpyxl 没有公开文档的 `ExcelWriter(...).write_data()`（`Workbook.save` 内部就是这样调用的）。不改代码：完全用公开 API 的替代写法是保存后用正则改 `core.xml`，更脆弱。风险：升级 openpyxl 后它可能失效或时间戳回到保存时间，由 `test_regenerating_the_sample_leaves_the_committed_workbook_unchanged` 发现。
- [x] **F2** Q9 只写了“与入库文件字节相同”这一项测试，没写“生成两次字节相同”：入库文件是在另一个时间生成的，与它逐字节相同，已经说明结果和生成时间无关，第一项是多余的。
- [x] **F3** `outputs: list[str]` 补了类型注解。
- [x] **F4** 本 Phase 新增的测试（`test_readme_quickstart.py`、`test_docs.py`、`test_data_gen.py` 新增的那条）改用 `rfq_agent.config.PROJECT_ROOT`；已有的测试不动。
- [x] **F5** 变量改名：`run_id` → `match`（Match 对象），`done` → `result`（`subprocess.run` 的结果）。
- 不改：**F6** writer 先压缩、再解压重压是为了固定 zip 条目时间，docstring 已说明。
- [x] **S1** spec §5 目录树：`cli.py` 改为 `cli/` 包，补上 `costing/`、`partno.py`、`pricing.py`、`trace.py`。
- [x] **S2** 记录：README 的“运行环境要求 SQLite ≥ 3.51.2”取自 spec §18；“生成更大的数据”一节对应 I2（调整比例要换 seed）。都保留。
- [x] **S3** 复核的位置按 §13（P8 修订）改：spec §4 的流程图改为 置信度 → 输出（收尾完成即可下载）→ 复核（仅 `--review`，在已收尾的运行上进行，每条决定后回到实体消解重新汇总）；README 架构图与图下说明同步。
- [x] **S4** README 的自评一句改为“自评保留加问 MSRP，但没有改成只降分（同意时仍按权重加分）”。
- 不改：**S5** 验收测试复制的是工作区而不是 HEAD，uv 缓存也让“全新”打了折扣；这与 Q10、I1 的决定一致，Q11 已用真实 `git clone` 补验。

## review 后用户追加（2026-10-12）
| # | 问题 | 决定 |
|---|---|---|
| I4 | README 语言（改 Q6） | 中英文分开：`README.md` 英文（GitHub 默认显示），`README.zh-CN.md` 中文，顶部互相链接，内容一致。快速开始验收照旧读 `README.md`；新增测试 `test_the_english_and_chinese_readmes_share_the_quickstart`（中文版命令改动时失败，已验证）。英文版注明 spec、GLOSSARY、plans 为中文 |
| I5 | 简介中的个人背景（改 Q7） | 删去作者个人背景的表述。改为：目前是学习与交流用的项目，关注的设计问题；后续计划发展为对接真实企业数据的版本；有意合作通过 GitHub Issue 联系（不放邮箱等个人联系方式）。仓库导览中 design-notes 写“模板，待填写”。spec §16 已写回 |
| I6 | 其他文件中的个人背景 | 用户同意改成中性说法：spec §1 改为“参考的原系统是一个已在生产中使用、基于 agent 平台搭建的 RFQ 自动报价流程”，评估结果的用途改为“检验设计选择”；spec §16 与 `docs/design-notes.md` 第 1 节标题改为“原系统概述（平台版本）”（`test_docs.py` 同步）；本文件 Q7、Q8 同步。git 历史中的旧文本在公开时处理（见 I7） |

- `uv run pytest`：639 passed。

## 开源发布（用户同意按推荐，2026-10-12）
| # | 问题 | 决定 |
|---|---|---|
| I7 | 公开方式 | 当前仓库（含全部历史）留在本地做私有存档；P11 合并后用 `git archive main` 的快照新建 GitHub 仓库 `RobertWei3/RFQ-Agent`，只有一个初始提交，以后在新仓库开发。不改写现有历史 |
| I8 | 许可证 | MIT（`LICENSE`，版权人写 GitHub 用户名 `RobertWei3`）；`pyproject.toml` 加 `license`；两版 README 末尾加许可证一节 |
| I9 | 公开仓库的提交者身份 | 新仓库本地配置用 GitHub 用户名与 noreply 邮箱（`110293175+RobertWei3@users.noreply.github.com`），不用个人邮箱 |

## 开源后追加：spec 英文译本（用户同意按推荐，2026-10-13）
| # | 问题 | 决定 |
|---|---|---|
| I10 | spec 要不要中英文 | 中文 `RFQ-SPEC.md` 保持唯一权威，新增英文译本 `RFQ-SPEC.en.md`：顶部互相链接，英文版开头注明以中文版为准；代码块中的注释翻译、代码原样保留（§18 的两个中文占位值保留并在块后说明）；末尾附中英术语对照表（取自 GLOSSARY.md）。spec §16 写明规则；CLAUDE.md 加同步要求（改 spec 时同一提交同步英文版）；测试 `test_the_english_spec_follows_the_chinese_spec` 检查章节编号、代码块数量与语言、python / yaml 代码块中注释以外的代码一致（改英文版一个配置值或删一个小节标题时失败，已验证）。顺带：spec 开头“按第 15 节的 Phase 顺序”改为第 17 节（原笔误）；§5 目录树补上 README.zh-CN.md、RFQ-SPEC.en.md、GLOSSARY.md、LICENSE、CLAUDE.md |
| I11 | GLOSSARY 要不要中英文 | 同 I10：中文 `GLOSSARY.md` 唯一权威，新增 `GLOSSARY.en.md`，顶部互相链接；词条标题为“英文术语（中文原词）”，_Avoid_ 行保留中文原词并附英文释义；CLAUDE.md 加同步要求；spec §5、§16（中英两份）记录；测试 `test_the_english_glossary_has_every_term_of_the_chinese_glossary` 检查各节词条数一致（删掉英文版一个词条时失败，已验证） |
