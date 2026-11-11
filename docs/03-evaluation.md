# 03 · 评测：预期、实际、失败与限制

## 1. 怎么复核（三步）

```powershell
python -m pytest                                  # 71 passed，原始输出见 examples/test-results.txt
python scripts/make_examples.py --mode mock       # 重跑 12 组离线用例（秒级）
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

## 5. 失败与已知限制（如实）

1. **真实模型不稳定**：同一问题多次跑，步数不同（曾出现 4/3 与 5/4 两种）。这是模型采样带来的正常差异，文档里给的是其中一次的记录。
2. **推理预算会吃掉输出**：`deepseek-v4-pro` 会把 token 先花在 `reasoning_tokens` 上。第一次跑 A2b 时 `max_tokens=3000` 被思维链占满，模型返回空内容，白丢一步。已改为默认 6000 并在客户端明确报错。
3. **模型会猜 chunk 编号**：一次 trace 里模型直接读 `D02#c0`（不存在），拿到 `not_found` 后自己改用真实 id。为减少这种情况，prompt 里已写明"chunk_id 必须来自检索结果"。
4. **校验器只做字面核对**：如果模型只引用冲突来源中的一份，代码无法判断语义上"少了一个来源"。兜底手段是 `search_docs` 的 `conflict_hint` + 评测断言，不追求形式化完备。
5. **Mock 不代表真实决策质量**：mock 是规则决策器，只验证流程与控制（上限、打回、状态判定）。真实质量看 §3 的三条记录。
6. **2-gram 召回有天花板**：语料里没有的说法（例如"违约金"）召不回来，同义词表只有 12 条。
7. **延迟偏高**：推理型模型单次作答 1~2.5 分钟（A2 实测 5 步 7055 tokens）。交互场景需要换非推理模型或加流式。

## 6. 被替换内容后会不会"照旧回答"？（评审可自行验证）

```powershell
# 把 D01 的 30 天改成 45 天（夹具已在仓库里）
python -m src.cli --question "报销期限多久？" --corpus conflict_min --docs-dir data/docs --docs-dir data/regression/d01_45days --json
# 期望：冲突值变成 45 vs 15，且 status 仍为 conflict_pending
```

夹具与断言见 `data/regression/`、`tests/test_agent_flows.py::test_c1_content_swap_changes_conflict_values`。
