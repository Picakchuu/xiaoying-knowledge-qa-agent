# 03 · 评测：预期、实际、失败与限制

## 1. 怎么复核（三步）

```powershell
python -m pytest                                  # 101 passed, 6 skipped，原始输出见 examples/test-results.txt
python scripts/selfcheck.py                       # 12/12 验收行为（mock 决策器，秒级）
MODEL_NAME=deepseek-chat python scripts/real_eval.py   # 真模型评测，报告见 examples/real-eval-deepseek-chat.md
python -m src.cli --input examples/case_a2_conflict/input.json --model mock --json
```

真实模型的三条记录是**静态产物**（评审没有 key 也能看）：`examples/case_a2*`，每条都带 `trace.jsonl`。
重跑它们需要 `.env` 里的 `MODEL_API_KEY`，命令：`python scripts/make_examples.py --mode real`。

## 2. 结果矩阵（15 组，全部可复现）

| 用例 | 模式 | 状态 | 步数 | 工具调用 | 引用数 | tokens |
|---|---|---|---|---|---|---|
| `case_a1_answered` | mock | answered | 4 | 3 | 1 | - |
| `case_a2_conflict` | **real** | conflict_pending | 5 | 4 | 2 | 7055 |
| `case_a2b_scoped_conflict` | **real** | conflict_pending | 6 | 5 | 4 | 6789 |
| `case_a2c_scoped_answered` | **real** | answered | 5 | 3 | 1 | 2776 |
| `case_a3_out_of_scope` | mock | insufficient | 4 | 3 | 0 | - |
| `case_a3b_missing_detail` | mock | insufficient | 4 | 3 | 0 | - |
| `case_a4a_paraphrase` | mock | answered | 4 | 3 | 1 | - |
| `case_a4c_query_rewrite` | mock | answered | 4 | 3 | 1 | - |
| `case_a5_followup` | mock | answered | 5 | 4 | 1 | - |
| `case_a6_tool_transient` | mock | conflict_pending（故障恢复） | 8 | 7 | 2 | - |
| `case_a6b_tool_down` | mock | failed | 4 | 3 | 0 | - |
| `case_a7_deprecated` | mock | answered（不采信已废止） | 4 | 3 | 1 | - |
| `case_b1_limit` | mock | incomplete | 2 | 1 | 0 | - |
| `case_c1_content_swap` | mock | conflict_pending（30→45） | 6 | 5 | 2 | - |
| `case_c2_content_swap` | mock | answered（不得结转→可结转） | 4 | 3 | 1 | - |

- **引用可核率：17/17 = 100%**（每条 `quote` 都能在对应 `chunk.text` 中逐字找到，按 `input.json` 的 `docs_dir` 叠加校验，含内容替换夹具）。
- 状态分布：answered 7 / conflict_pending 4 / insufficient 2 / failed 1 / incomplete 1。
- 无依据回答数：**0**（`insufficient` 与 `failed` 都不带引用）。

## 3. 真实模型的三条路径（本次实测 `deepseek-v4-pro`）

模型：`openai-compatible:deepseek-v4-pro`；`json_mode=true`；文件在 `examples/case_a2*/trace.jsonl`。

### 3.1 A2 纯冲突 → 查证两轮后追问（`case_a2_conflict`）

```text
step1 search_docs  查询="报销期限"                      → 命中 D01#c2(1.00)、D02#c2(0.53)，note 给出两份文档
step2 search_docs  查询="费用报销管理细则 适用范围 差旅 替代"  → 想找 D02 是否覆盖差旅、是否替代 D01
step3 search_docs  查询="以本细则为准 不一致 冲突 …"      → 找优先级条款，没有找到
step4 list_versions 主题="差旅报销制度"                   → 确认 D01 未废止
step5 ask_user     同时写出 30 天/15 天 与两份版本日期     → conflict_pending
```

决策不是预设的：**第 2、3 步是模型看到第 1 步的 `涉及文档` 与冲突线索后自己发起的二次检索**，第 4 步是它自己想到查版本。最终回答里并列了两份原文数值与版本日期，引用 2 条，`gaps=["报销类型/适用文件"]`。

### 3.2 A2b 语料含总部专用版 → 追查范围后追问（`case_a2b_scoped_conflict`）

```text
step1 search_docs → read D02#c1（总则，没有适用范围）
step3 search_docs "本细则适用于 费用报销" → 命中 D02b#c1(0.88)「仅适用于总部（北京、上海办公区）」
step4 search_docs "…施行范围 与差旅报销制度关系" → 命中 D02b#c3(0.73)
step5 read_chunk D02#c3
step6 ask_user：D01 30 天 / D02、D02b 均 15 天，且 D02b 自 2026-02-01 在总部范围内优先
```

和 3.1 的差别来自**检索结果不同**：这里语料里有总部专用版，模型查到它之后把结论改成"条件化 + 仍缺用户范围"，引用 4 条（含 D02b#c1/c3）。

### 3.3 A2c 用户给了范围 → 条件满足后直接作答（`case_a2c_scoped_answered`）

```text
step1 search_docs "总部员工 报销期限" → D01#c2(0.68)、D02b#c1(0.50)、D02b#c2(0.42)
step2 read_chunk D02b#c2
step3 read_chunk D02b#c3
step4 answer  ← 被校验器打回：回答里写了 2026-02-01，但引用原文里没有
   obs: [missing_number_grounding] 回答里的 2026-02-01 没有出现在任何引用原文中
step5 answer  ← 改成只保留有出处的表述 → answered，引用 D02b#c2
```

**这是本项目最想展示的一步**：模型自己写的日期不在任何引用里，代码当场拦下，模型在下一步改成了可核版本。没有这一步，"引用校验"就只是文档里的一句话。

### 3.4 三条路径的对比

| | A2 | A2b | A2c |
|---|---|---|---|
| 触发差异 | 语料只有两份且都无范围 | 语料含范围限定版 | 用户在问题里给了范围 |
| 动作序列 | 3 次检索 + 版本反查 | 检索 + 读原文 + 3 次检索 | 检索 + 2 次读原文 |
| 结果 | 追问 | 追问（引用更全） | 直接作答 |
| 说明什么 | 模型会为了"能不能裁决"去反复取证 | 模型会读检索结果里的文档标题，去追"总部专用版" | 条件一旦成立，模型愿意下结论而不是永远追问 |

### 3.5 强弱模型对照：一次真实的退化（也是护栏的由来）

换了便宜的 `deepseek-chat` 重跑同样三条（用环境变量覆盖，不改 `.env`）：

| 用例 | `deepseek-v4-pro`（强） | `deepseek-chat`（弱，加固前） | `deepseek-chat`（加固后） |
|---|---|---|---|
| A2 纯冲突 | conflict_pending，5 步 | conflict_pending，6 步 ✅ | 同左 |
| A2b 含总部版 | conflict_pending，6 步 | **answered：说"两份细则一致 15 天"，把 D01 的 30 天丢掉了** ❌ | **conflict_pending，7 步，引用 5 条** ✅ |
| A2c 给了范围 | answered，5 步 | answered，5 步 ✅ | 同左 |

弱模型那次"挑自洽来源"的回答里，两份引用都写 15 天，`unhandled_conflict` 抓不到——
这是校验器的语义盲区。于是补了第四级 `cherry_picked_conflict`：只要上一轮检索给过
`conflict_hint`（库里确有 15 天 vs 30 天），而回答只出现其中一个值、又没写适用范围，就打回。
加固后同一个弱模型在 A2b 上的输出变成"三份文件分别怎么写 + 请确认所属范围"，和强模型同档。

结论：**这类任务里"弱模型 + 强护栏"比"只看模型名"更可靠**；延迟也从 2.5 分钟降到 20 秒量级。
演示/自测建议用 `deepseek-chat`，把 `deepseek-v4-pro` 的记录保留为"强模型基线"。

## 4. 关键验收点实测

| 验收点 | 证据 | 结论 |
|---|---|---|
| 明确答案 | `case_a1_answered`：引用 D03#c2，quote 含「不得结转」 | 通过 |
| 文档冲突 | `case_a2_conflict`（real）+ 单测 `test_unhandled_conflict_is_rejected` | 通过；代码会拦"只答一个值" |
| 改写问法 | `case_a4a_paraphrase`（同源 D03#c2）、`case_a4c_query_rewrite`（打车→市内交通费，命中 D02#c4） | 通过 |
| 上下文追问 | `case_a5_followup`：history 承接年假 → 引用 D04#c3「试用期不享受年假」 | 通过 |
| 工具失败（瞬时） | `case_a6_tool_transient`：前 2 次 `search_docs` 超时 → 换 query → 恢复 | 通过 |
| 工具失败（持续） | `case_a6b_tool_down`：`status=failed`，零引用，说明卡在检索 | 通过 |
| 资料外 | `case_a3_out_of_scope`：`insufficient`，无引用 | 通过 |
| 资料不足 | `case_a3b_missing_detail`：`insufficient` + `gaps=["具体价格/报价单"]` | 通过 |
| 版本意识 | `case_a7_deprecated`：检索时过滤 D08 并说明「旧版文件已废止」 | 通过 |
| 步数上限 | `case_b1_limit`：`incomplete`，`reason` 含 `tool_call_limit`，已完成动作可查 | 通过 |
| 内容替换 | `case_c1_content_swap`：D01 改 45 天 → 冲突值随之变 45 vs 15；`case_c2_content_swap`：D03 改可结转 → 答案反转 | 通过 |

## 5. 真实模型评测（可重复，`scripts/real_eval.py`）

前面 §2 的矩阵是 **mock 决策器**跑出来的（证明流程与控制），下面这份是**真模型**跑的同一批用例：
`MODEL_NAME=deepseek-chat python scripts/real_eval.py`，报告落盘 `examples/real-eval-deepseek-chat.md`。

最近一次结果（2026-11-11，14 条，跳过 `b1_limit` 这类纯控制流用例）：

| 用例 | 期望 | 实际 | 步数 | 引用 | tokens | 耗时 |
|---|---|---|---|---|---|---|
| a1_answered | answered | answered | 2 | 1 | 1488 | 4.5s |
| a2_conflict | conflict_pending | conflict_pending | 6 | 2 | 2284 | 16.4s |
| a2b_scoped_conflict | conflict_pending | conflict_pending | 6 | 4 | 2488 | 15.7s |
| a2c_scoped_answered | answered | answered | 8 | 2 | 2305 | 20.3s |
| a3_out_of_scope | insufficient | insufficient | 5 | 0 | 1012 | 12.4s |
| a3b_missing_detail | answered/insufficient | answered（带缺口） | 4 | 3 | 2544 | 11.2s |
| a4a_paraphrase | answered | answered | 3 | 1 | 1613 | 8.4s |
| a4c_query_rewrite | answered | answered | 5 | 1 | 2813 | 14.2s |
| a5_followup | answered | answered | 3 | 1 | 1626 | 6.5s |
| a6_tool_transient | conflict_pending | conflict_pending | 8 | 2 | 2346 | 27.6s |
| a6b_tool_down | failed | failed | 3 | 0 | 927 | 7.2s |
| a7_deprecated | answered | answered | 5 | 2 | 2267 | 15.2s |
| c1_content_swap | conflict_pending | conflict_pending | 6 | 2 | 2298 | 22.9s |
| c2_content_swap | answered | answered | 3 | 2 | 1693 | 8.8s |

**14/14 通过**，总 tokens 27704，总耗时 191 秒（单条 4.5–27.6 秒）。另有三条 `deepseek-v4-pro` 的记录在 `examples/case_a2*`（强模型基线，单条 33–146 秒）。

### 真模型测试抓到的三个问题（都已修）

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | a3b 回答「文档未提供具体价格」但 `gaps` 为空 | 模型不填 `missing` | 代码兜底：回答含「未提供/未写明」且缺口为空 → 按问题类型回填（`_infer_gaps`） |
| 2 | a6b 持续故障时模型一路重试到步数上限，返回 `incomplete` 而不是 `failed` | 没有任何熔断 | 同一工具连续失败 3 次 → 代码直接 `failed`，不再让模型空转 |
| 3 | a4c / a7 间歇性 `failed`（`模型连续输出不可用`） | `deepseek-chat` 会在 JSON 后面再跟一段说明，严格 `json.loads` 整段直接报错 | 只取输出里第一个完整 JSON 对象（`parse_first_json`），并容忍代码围栏 |

这三条如果只跑 mock，永远发现不了——mock 是我写的规则，它不会"多说一句话"，也不会"一直重试到底"。

### 评测的边界

- 真模型有随机性：同一用例两次运行步数会不同（a2c 出现过 5、6、8 步），所以断言的是**行为区间**而不是精确步数；
- 一条用例跑一次通过不代表稳定，量产应该跑 3–5 次取多数；
- 本次矩阵用的是 `deepseek-chat`（快、便宜）；`deepseek-v4-pro` 的三条强模型记录单独保留在 `examples/case_a2*`。

### 延迟为什么高（实测拆解）

一次运行 = **多次模型调用**（每次决策一次），每一步都要重新发送系统提示 + 历史观察。以 `deepseek-v4-pro` 跑 `case_a2_conflict` 为例（trace 时间戳计算）：

| 步骤 | 动作 | 距上一步 |
|---|---|---|
| 1 | search_docs | — |
| 2 | search_docs（自己追加的查证） | **+25.0s** |
| 3 | search_docs（查优先级条款） | **+32.0s** |
| 4 | list_versions | **+37.0s** |
| 5 | ask_user | **+60.0s** |
| 合计 | 5 步 | **约 154 秒** |

原因有三层：
1. `deepseek-v4-pro` 是**推理模型**：每步先产生 `reasoning_tokens`（实测整轮 3000~3800），思考本身就要十几秒；
2. 步数多：冲突场景要反复取证，5~9 步意味着 5~9 次调用；
3. 上下文重发：每步都把系统提示与最近的观察整段发回去。

对比同一批用例用 `deepseek-chat`（非推理）：**单条 4.5~27.6 秒，14 条合计 191 秒**。

**因此**：演示/自测建议 `deepseek-chat` 或 `--model mock`；`deepseek-v4-pro` 的记录保留为"强模型基线"。
页面在等待超过 2 分钟时也会给出这条建议。

## 6. 失败与已知限制（如实）


1. **真实模型不稳定**：同一问题多次跑，步数不同（曾出现 4/3 与 5/4 两种）。这是模型采样带来的正常差异，文档里给的是其中一次的记录。
2. **推理预算会吃掉输出**：`deepseek-v4-pro` 会把 token 先花在 `reasoning_tokens` 上。第一次跑 A2b 时 `max_tokens=3000` 被思维链占满，模型返回空内容，白丢一步。已改为默认 6000 并在客户端明确报错。
3. **模型会猜 chunk 编号**：一次 trace 里模型直接读 `D02#c0`（不存在），拿到 `not_found` 后自己改用真实 id。为减少这种情况，prompt 里已写明"chunk_id 必须来自检索结果"。
4. **校验器只做字面核对**：`cherry_picked_conflict` 能在"检索线索已暴露冲突"时拦住"悄悄挑一个"，但如果检索压根没召回另一份来源，代码仍然判断不了——这属于检索覆盖率问题，靠 `conflict_hint` + 评测断言兜底，不追求形式化完备。
5. **Mock 不代表真实决策质量**：mock 是规则决策器，只验证流程与控制（上限、打回、状态判定）。真实质量看 §3 的三条记录。
6. **2-gram 召回有天花板**：语料里没有的说法（例如"违约金"）召不回来，同义词表只有 12 条。
7. **延迟偏高**：推理型模型单次作答 1~2.5 分钟（A2 实测 5 步 7055 tokens）。交互场景需要换非推理模型或加流式。

## 7. 被替换内容后会不会"照旧回答"？（评审可自行验证）

```powershell
# 把 D01 的 30 天改成 45 天（夹具已在仓库里）
python -m src.cli --question "报销期限多久？" --corpus conflict_min --docs-dir data/docs --docs-dir data/regression/d01_45days --json
# 期望：冲突值变成 45 vs 15，且 status 仍为 conflict_pending
```

夹具与断言见 `data/regression/`、`tests/test_agent_flows.py::test_c1_content_swap_changes_conflict_values`。
