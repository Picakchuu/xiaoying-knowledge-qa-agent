# 企业知识问答 Agent — 方案设计（v2 终版）

> 考核题目：小影校招 AI Agent 开发岗 · 项目题 3「企业知识问答 Agent」
> 本文件是**实现依据**（工作文档，交付时移入 `docs/00-plan.md`）。
> v1：2026-11-11 初稿。**v2：2026-11-11 按考卷逐条对账后补齐 P0/P1 缺口**，改动见文末「v2 变更记录」。

---

## 0. 一句话结论

选**题 3**：四题中唯一「模型输出质量本身被检验」的题——引用是否支持答案、冲突是否如实呈现、检索是否迭代改进，都是 AI Agent 岗的日常核心。

交付目标不是「跑通」，而是：**6 个必验点全覆盖 + 真实模型两条走向的 trace + 内容可替换的回归证据 + 可复现的 examples**。

---

## 1. 选题与需求

### 1.1 为什么是题 3

| 维度 | 题 1 素材排期 | 题 2 内容规划 | **题 3 知识问答（选）** | 题 4 短视频脚本 |
|---|---|---|---|---|
| 模型判断力被检验的程度 | 低（状态机） | 中（重规划） | **高** | 中 |
| 岗位匹配度 | 中 | 中 | **高（RAG 落地最广）** | 中 |
| 关键缺陷风险 | 虚报保存 | 状态错乱 | **无依据回答/错误引用** | 编造成效 |

不选的理由备查：题 1 偏业务系统工程、展示面窄；题 2 易退化为串行调用；题 4 最容易踩编造事实的致命线。

### 1.2 用户与目标

- **用户**：企业员工（新员工 / HR / 销售），不愿翻 20 个文档，但**怕被 AI 编答案害了自己**。
- **目标**：给出可追溯到原文的答案；查不到就说查不到；文档打架就说打架并请求确认。

### 1.3 输入

- 问题 + 可选对话历史；
- 合成制度/FAQ 文档库（`doc_id` / `title` / `version_date` / `doc_type` / `status` / `chunks[]`）；
- 工具：文档检索、片段读取、版本反查（+ 引用校验器）。

### 1.4 交付结果（缺一不可）

1. 回答正文（或无法回答的原因）
2. **引用：`doc_id` + `chunk_id` + `quote`（原文逐字片段）**，逐条对应，不得拼凑
3. 资料缺口（`gaps[]`：缺什么信息才能回答）
4. **任务状态**（见 2.6）
5. 执行 trace（步骤 / 工具参数 / 返回摘要 / 后续动作 / 一句话决策依据）

### 1.5 红线

- 不凭空选一个答案：冲突未解时列冲突 + 请求确认；
- 不无依据回答：结论必须被 quote 支持，校验器不过就重打回或降级；
- 不越权：只读文档库，不修改、不对外发布；
- 不静默失败：工具报错必须体现在最终状态与 `gaps` 里。

### 1.6 任务状态（五态，终版）

| 状态 | 含义 | 判定者 |
|---|---|---|
| `answered` | 有 ≥1 条通过校验的引用，且回答中所有数值断言被 quote 覆盖 | 代码 |
| `conflict_pending` | 存在 ≥2 个互斥来源且无充分依据仲裁 → 列冲突 + 请求确认 | 代码 + 模型 |
| `insufficient` | 检索穷尽仍无支撑材料，或校验重试后仍无法给出有据答案 | 代码 |
| `failed` | 工具持续失败且无降级路径 | 代码 |
| `incomplete` | 达到 `MAX_STEPS` / `MAX_TOOL_CALLS` 上限 | 代码 |

> v1 的 §2.4 只列 4 态、§4.4 多出 `incomplete`，v2 统一为五态。

---

## 2. 核心挑战清单

按实现顺序打勾：

1. **引用可验证**：回答中每个数字/期限/条件都能映射到某段原文 → 两级校验器（§4.6）。
2. **冲突不越权**：D01「30 天」vs D02「15 天」且都无适用范围 → 列冲突 + `ask_user`，状态 `conflict_pending`。
3. **冲突可升级为可解**：D02b 明确「仅适用总部」→ 查证后给条件化答案（总部 15 天 / 其他 30 天），而不是一律甩锅。两条路径都要有 trace。
4. **检索迭代**：首轮召回不相关 → 模型改写 query（同义词 / 去修饰 / 换 doc_type / 版本反查）→ 再检索。
5. **多轮指代**：「那年假可以留到明年吗」→「那试用期员工呢？」要承接上文主题，不丢上下文重开。
6. **工具失败降级**：超时 → 换 query 重试 → 仍失败则 `failed`/`insufficient`，`gaps` 说明卡在哪。
7. **步数与调用上限**：`MAX_STEPS=12`、`MAX_TOOL_CALLS=8`，超限返回 `incomplete` + 已完成动作列表。
8. **离线可验证**：无 Key 也能跑通全部用例（mock 决策器），README 标清边界。
9. **内容替换鲁棒（v2 新增）**：改写文档内容后答案与动作必须随之改变 → `--docs-dir` 叠加 + 回归夹具。
10. **可审计但不越界（v2 新增）**：trace 只记「步骤 / 参数 / 返回摘要 / 后续动作 / 一句话理由」，**不记录模型内部思维过程**。

---

## 3. 技术选型

### 3.1 语言与框架

**原生 Python 3.13 + `openai` SDK（OpenAI 兼容接口），不使用 ADK / AgentScope / LangGraph。**

取舍理由（写进 `docs/02-solution.md`）：

- 本题被检验的核心恰恰是**决策循环本身**；框架会把它封装掉，等于把评分点藏起来；
- 动作空间需要严格枚举校验，自建 60 行的 `actions.py` 比框架的 schema 更可控；
- 零重依赖 → 评委一条命令能跑；离线 mock 不需要任何外部服务；
- 代价：没有中间件生态，trace 与重试需自写（约 80 行），已在预算内。

### 3.2 模型选型

| 角色 | 模型 | 理由 |
|---|---|---|
| **主模型** | `deepseek-chat`（DeepSeek-V3，OpenAI 兼容） | 中文制度文本理解强、JSON 输出稳定、单价低（20 条评测集全跑约 ¥0.1 量级）、国内直连稳定 |
| 对照模型（可选） | `qwen-plus` / `glm-4.6` / `moonshot-v1-8k` / `gpt-4o-mini` | 证明「决策来自模型而非针对某模型调参」，只需换 `MODEL_NAME` |
| 明确不使用 | `deepseek-reasoner`、`o1` 类 | trace 只记一句话理由，思维链不入库；且延迟/成本高、`reasoning_content` 易泄漏 |

调用参数：`temperature=0.1`、`response_format={"type":"json_object"}`、`max_tokens=1200`、超时 60s、失败重试 1 次（网络层）。

`MODEL_*` 全部走环境变量，任何 OpenAI 兼容端点可直接替换（见 `.env.example`）。

### 3.3 运行方式

- **主入口：CLI**（考卷三选一）；
- **附加：极简 HTTP**（`python -m src.http_app --port 8000`，仅标准库 `http.server`，零依赖），展示服务化封装，不承担验收。

---

## 4. 系统设计

### 4.1 总体流程

```
问题(+history) → [ReAct 循环]
  模型输出动作 JSON（含 why 一句话理由）
    ├─ search_docs ──► 工具 ──► observation(+conflict_hint)
    ├─ read_chunk ───► 工具 ──► observation
    ├─ list_versions ► 工具 ──► observation
    ├─ citation_check ► 校验器 ─► pass / fail(打回，模型改引用或改答案)
    ├─ ask_user ─────► 输出待确认项 + 状态 conflict_pending
    └─ answer ───────► 代码终审：引用校验 + 冲突校验 → answered / 打回
  上限：MAX_STEPS=12 / MAX_TOOL_CALLS=8 → incomplete
→ [JSON 输出] + [Markdown 渲染] + [trace.jsonl 落盘]
```

### 4.2 动作空间（枚举化，禁止自由文本解析）

```json
{"action": "search_docs", "args": {"query": "string", "top_k": 5, "doc_type": "policy|faq|any", "include_deprecated": false}}
{"action": "read_chunk",  "args": {"doc_id": "string", "chunk_id": "string"}}
{"action": "list_versions","args": {"topic": "string"}}
{"action": "citation_check","args": {"answer_text": "string", "citations": [{"doc_id": "","chunk_id": "","quote": ""}]}}
{"action": "ask_user",    "args": {"question": "string", "missing": ["string"], "options": ["string"]}}
{"action": "answer",      "args": {"answer_text": "string", "citations": [{"doc_id": "","chunk_id": "","quote": ""}], "confidence": "high|medium|low"}}
{"action": "finish",      "args": {"status": "answered|conflict_pending|insufficient|failed", "summary": "string"}}
```

- 每次模型输出形如 `{"action": "...", "args": {...}, "why": "一句话理由"}`；`why` **只**作为 trace 的「决策依据」字段，不要求长篇推理；
- 模型只能「提议」，**最终状态由代码判定**：模型说 `answered` 但校验器不过 → 强制降级。

### 4.3 Observation 协议

```json
{
  "ok": true,
  "tool": "search_docs",
  "data": [{"doc_id":"D01","chunk_id":"D01#c2","score":0.82,"heading":"报销期限",
            "text":"员工应在费用发生后 30 天内提交报销申请，逾期不予受理。",
            "version_date":"2025-03-01","status":"active"}],
  "error_code": null,
  "note": "命中 2 段；已过滤 1 篇已废止文档(D08)",
  "hints": {"conflict_hint": {"unit":"天","candidates":[{"doc_id":"D01","doc_id_chunk":"D01#c2","value":"30"},
                                                         {"doc_id":"D02","doc_id_chunk":"D02#c2","value":"15"}]}}
}
```

- 失败：`ok=false, error_code ∈ {timeout, internal_error, bad_args, not_found}`，`note` 给人看；
- **工具内部捕获所有异常**，绝不把栈抛给模型；
- `conflict_hint` 是**工具给线索，不是结论**：同一次检索中，两段以上来自不同文档、都含「数字+时间单位」、且正文至少共享 2 个字符 2-gram（即确实在谈同一件事）时给出。模型仍需自己判断是否追问。

### 4.4 主循环（伪代码，已修 v1 的 history bug）

```python
history = []                                   # 全量 observation，不截断最后一条
for step in range(MAX_STEPS):
    action = model.decide(question, history, schema)   # 失败→按 bad_model_output 重试 1 次
    rec = trace.start_step(step, action)
    if action.name in {"answer", "finish", "ask_user"}:
        verdict = validate_final(action, history, doc_index)   # 代码兜底
        if verdict.ok: return finalize(action, verdict)
        obs = obs_fail(verdict)                # 打回：模型必须改引用/改答案/转 ask_user
        history.append(obs); trace.end_step(rec, obs, next_action=None); continue
    if tool_calls >= MAX_TOOL_CALLS:
        return incomplete(reason="tool_call_limit", done_actions=trace.actions())
    if tool_calls >= MAX_TOOL_CALLS - 0 and action.name == "search_docs":  # 预算保护：留 1 次给 read_chunk
        ...
    result = TOOLS[action.name](**action.args)  # 内部吞异常
    tool_calls += 1
    history.append(result)
    trace.end_step(rec, result)
return incomplete(reason="step_limit", done_actions=trace.actions())
```

Token 预算：`build_model_input()` 对 history 做「最近 3 条 observation 全文 + 更早条目压缩为一行摘要」，防止 12 步后爆上下文。

### 4.5 Citation 校验器（本项目护城河）

**共用归一化 `src/normalize.py`**（检索与校验必须用同一套，否则会自相矛盾）：

- `norm_text`：NFKC（全角→半角）→ 小写 → 去所有空白 → 中文数字在位单位前转阿拉伯（`三十天`→`30天`，`两`→`2`）→ 去引号/顿号噪声；
- `facts(text)`：抽取 `(数值, 单位)`，单位含 天/日/个工作日/工作日/个月/月/年/元/%/次/人/位/小时/分钟/周/万行，日期按 `date` 处理；
- `bigrams`：字符 2-gram + ASCII 词，用于检索打分。

**两级校验**（`validator.check`）：

1. **quote 存在性**：`norm(quote)` 必须是 `norm(chunk.text)` 的子串 → 否则 `citation_not_found`（拦「差一点点凭记忆编的原文」）；
2. **数值 grounding**：回答里每个 `(数值, 单位)` 必须能在**某条 cite 的 quote** 里找到同值同（或等价）单位 → 否则 `missing_number_grounding`（拦「引 D01 却说 15 天」）；
3. **未处理冲突（v2 新增）**：若模型自己引用了 ≥2 段、来自不同文档、时间类数值不同，而回答里只出现其中一个值 → `unhandled_conflict`，打回要求并列或转 `ask_user`。

返回：

```json
{"ok": false, "errors": [{"code": "missing_number_grounding", "value": "15", "unit": "天",
                          "message": "回答中的 15 天 未出现在任何引用原文中"}]}
```

失败原因直接回灌给模型 → **这就是一次真实的「观察结果 → 调整动作」**，也是 trace 里最漂亮的一段。

**已知边界（要在 03 写清）**：若模型只引用互相冲突来源中的**一份**，代码层无法判定语义冲突——此时靠 `search_docs` 的 `conflict_hint` + system prompt + 评测集断言兜底。

### 4.6 Trace 协议（v2 新增，JSONL）

```json
{"type":"run_start","run_id":"20261111T132600-ab12","model":"deepseek-chat","provider":"deepseek",
 "question":"报销期限多久？","history_len":0,"docs_dir":["data/docs"],"corpus":"conflict_min",
 "faults":["search_docs:timeout:1"],"limits":{"max_steps":12,"max_tool_calls":8},"ts":"..."}
{"type":"step","step":1,"ts":"...","rationale":"先用原句检索，看召回质量",
 "action":"search_docs","args":{"query":"报销期限多久","top_k":5},
 "observation_summary":"ok：命中 2 段 D01#c2(0.82) / D02#c2(0.79)；conflict_hint 提示 30 天 vs 15 天",
 "ok":true,"error_code":null,"next_action":"read_chunk"}
{"type":"decision","step":3,"final_action":"ask_user","status":"conflict_pending",
 "reason":"两份制度对同一事项给出不同期限且均无适用范围，按红线不得自行仲裁"}
{"type":"run_end","status":"conflict_pending","steps":3,"tool_calls":2,"duration_ms":4210,
 "answer":"...","citations":[...],"gaps":["适用制度范围（总部/分支机构）"]}
```

- `next_action` 在下一步决策到达时回填到上一条记录（trace writer 缓冲一步）；
- **只有 `rationale` 一句话**，明确写「非模型内部思维过程」。

### 4.7 冲突仲裁策略（prompt + 代码双保险）

- 多来源对同一事实给出不同值 → **先查证**：`list_versions` 看版本与 `status`，`read_chunk` 读全文找适用范围/生效范围；
- 能判别 → 条件化答案（「总部员工 15 天；其他区域 30 天」）+ 引用全部相关来源 + `confidence: medium`；
- 不能判别 → `ask_user`，列两条原文与版本日期，`gaps:["适用制度范围"]`，状态 `conflict_pending`；
- **禁止**（评测里记为关键缺陷）：只引一条、默认「新版本优先」、把 30 与 15 折中成 22。

### 4.8 检索改进策略（行为提示，不写死顺序）

首轮空召回或低相关时，模型自行选择：同义词/口语化改写（`打车`→`市内交通费`、`攒`→`结转`）、去修饰只留实体、`doc_type` 收窄/放宽、`list_versions` 反查、`read_chunk` 读全文。

### 4.9 多轮上下文

- `history` 作为输入（`--input` 文件或 `--session-file`），保留最近 N 轮 `(question, answer, cited_doc_ids)`；
- 追问时注入「当前主题 + 上一轮引用 doc_id」作为检索偏好，而非重问；
- 评测默认单轮，避免互相污染；追问用例带显式 `history`。

### 4.10 CLI / HTTP 接口面（v2 新增，examples 必须能一键复现）

```bash
# 最小运行
python -m src.cli --question "报销期限多久？" --model mock

# 用输入文件（含 history/语料/故障注入覆盖）
python -m src.cli --input examples/case_a2_conflict/input.json --model real \
  --trace-out examples/case_a2_conflict/trace.jsonl --json --pretty

# 语料子集（同一问题、不同语料 → 不同决策，A2 vs A2b 的关键）
python -m src.cli --question "报销期限多久？" --corpus data/corpora/conflict_min.json --model mock

# 内容替换：后给的目录覆盖先给的 doc_id
python -m src.cli --question "报销期限多久？" --docs-dir data/docs --docs-dir data/regression/d01_45days

# 工具故障注入：search_docs 前 2 次超时（第 3 次恢复）
python -m src.cli --question "报销期限多久？" --fault search_docs:timeout:2

# 上限演示
python -m src.cli --question "年假能攒吗？" --max-steps 3 --max-tool-calls 1 --model mock

# HTTP
python -m src.http_app --port 8000   # POST /ask {"question":"...","history":[]}
```

参数表：`--question | --input | --model mock|real | --docs-dir(可重复，后者覆盖) | --corpus | --fault tool:error[:n] | --trace-out | --json | --pretty | --max-steps | --max-tool-calls | --session-file`。

---

## 5. 数据设计

### 5.1 文档清单（合成，无任何真实企业信息）

| doc_id | 标题 | 版本 | 类型 | status | 用途 |
|---|---|---|---|---|---|
| D01 | 差旅报销制度（2025 版） | 2025-03-01 | policy | active | 冲突 A：30 天 |
| D02 | 费用报销管理细则（2026 修订） | 2026-01-15 | policy | active | 冲突 A：15 天，**故意不写适用范围** |
| D02b | 费用报销管理细则（总部专用版） | 2026-02-01 | policy | active | 冲突 B 变体：**明确「仅适用总部」** → 条件化答案 |
| D03 | 年假与调休规定 | 2025-07-01 | policy | active | 明确答案 + 改写问法 |
| D04 | 试用期管理说明 | 2025-09-01 | policy | active | 多轮追问 |
| D05 | 产品 FAQ：导出与权限 | 2026-03-10 | faq | active | 明确答案 |
| D06 | 产品 FAQ：计费说明 | 2026-04-02 | faq | active | 资料缺口（只有套餐名，无单价） |
| D07 | 信息安全红线 | 2025-11-20 | policy | active | 无关干扰项 |
| D08 | 旧版报销说明（已废止） | 2024-06-01 | policy | **deprecated** | 版本意识干扰项 |

### 5.2 语料子集（`data/corpora/*.json`）

| 文件 | doc_ids | 用途 |
|---|---|---|
| `full.json` | 全部 9 篇 | 默认 |
| `conflict_min.json` | D01, D02 | **A2 纯冲突**（无 D02b，冲突不可解 → 必须 ask_user） |
| `no_claim.json` | D05, D06, D07 | 资料外用例，确认不瞎答 |

> 同一问题在不同语料下给出不同决策，本身就是检索/仲裁能力的证据。

### 5.3 切片与检索

- 按语义段落切 chunk，`chunk_id = doc_id#c{n}`，保留 `heading`；
- 检索：共用归一化 → 字符 2-gram 覆盖率打分 + 标题/小标题加权 + 同义词扩展表（`src/synonyms.py` 内联小表：打车→市内交通费、攒/留到明年→结转、报下来→报销、单价→价格/报价单）；
- `include_deprecated=false` 默认过滤（`status=deprecated`），`note` 里说明过滤了几篇；
- 预留 `EmbeddingRetriever` 接口，README 说明升级路径（不实现）。

### 5.4 内容替换夹具（`data/regression/`，v2 新增）

| 目录 | 改动 | 期望 |
|---|---|---|
| `d01_45days/` | D01#c2「30 天」→「45 天」 | 冲突值变为 45 vs 15，答案必须跟着变 |
| `d02b_scope_removed/` | D02b#c1 删掉「仅适用于总部」 | 失去判别依据 → 退化为 `conflict_pending` |
| `d03_carryover_allowed/` | D03#c2「不得结转」→「可结转，最多 5 天」 | A1 答案反转 |

每个目录只放**被改动的单篇 JSON**，靠 `--docs-dir` 叠加覆盖，避免整库复制。

### 5.5 评测集（`data/eval_set.jsonl`，15–20 条）

字段：`id, question, history, corpus, expected_status, must_contain[], must_not_contain[], expected_doc_ids[], note`。
指标定义（写进 03）：

- `状态准确率` = 状态匹配用例数 / 总数
- `引用正确率` = quote 全部命中原文的引用条数 / 引用总条数
- `冲突检出率` = 冲突用例中输出 `conflict_pending` **或**带并列引用的条件化答案的比例
- `平均步数/平均工具调用`、`无依据回答数`（必须为 0）

---

## 6. 验收点 → 用例 → 断言 → 材料

| # | 必验点 | 用例 | 语料 | 预期 | 关键断言 | 材料 |
|---|---|---|---|---|---|---|
| A1 | 明确答案 | 年假可以攒到明年吗？ | full | answered | 引用 D03#c2；quote 含「不得结转」 | `examples/case_a1_answered/` |
| A2 | 文档冲突 | 报销期限多久？ | conflict_min | conflict_pending | 并列 D01(30 天)/D02(15 天)；**不得**只给单一数字 | `examples/case_a2_conflict/` |
| A2b | 冲突可解 | 报销期限多久？ | full | answered/medium | 条件化：总部 15 天、其他 30 天；引用 2–3 条 | `examples/case_a2b_scoped/` |
| A3 | 资料外 | 公司明年会裁员吗？ | no_claim | insufficient | 无 citation；`gaps` 说明资料不含 | `examples/case_a3_out_of_scope/` |
| A3b | 资料不足 | 专业版一年多少钱？ | full | insufficient | 引 D06#c3（联系销售）；`gaps:["具体单价/报价单"]` | `examples/case_a3b_missing_detail/` |
| A4a | 改写问法 | 年假没用完能留到明年吗？ | full | answered | 与 A1 同源 D03#c2 | `examples/case_a4a_paraphrase/` |
| A4b | 改写问法 | 报销单最晚什么时候交？ | conflict_min | conflict_pending | 召回集合与 A2 相同 | 同上目录内 |
| A4c | 检索改进 | 出差打车一天最多能报多少？ | full | answered | 需改写（打车→市内交通费）→ D02#c4「200 元」 | `examples/case_a4c_query_rewrite/` |
| A5 | 上下文追问 | （history 承接年假）那试用期员工呢？ | full | answered | 引用 D04#c3；不重复问工资/时长 | `examples/case_a5_followup/` |
| A6 | 工具失败(瞬时) | 报销期限多久？ | conflict_min | conflict_pending | 前 2 次 search 超时→换 query 成功；trace 有 error_code | `examples/case_a6_tool_transient/` |
| A6b | 工具失败(持续) | 报销期限多久？ | conflict_min | failed | 全程超时；状态如实，不编答案 | `examples/case_a6b_tool_down/` |
| A7 | 版本意识 | 报销需要交纸质发票吗？ | full | answered/insufficient | 不把 D08(已废止) 当现行；说明 D08 状态 | `examples/case_a7_deprecated/` |
| B1 | 步数上限 | 年假能攒吗？（限 1 步 1 调用） | full | incomplete | 返回 `reason=step_limit/tool_call_limit` + 已完成动作 | `examples/case_b1_limit/` |
| B2 | 引用校验 | 构造错误引用（单测） | — | 打回 | `citation_not_found` / `missing_number_grounding` / `unhandled_conflict` | `tests/` + `examples/test-results.txt` |
| C1 | 内容替换 | 同 A2，换 `d01_45days` | regression | 冲突值变 45 | 答案随之改变 | `examples/case_c1_content_swap/` |
| C2 | 内容替换 | 同 A1，换 `d03_carryover_allowed` | regression | answered 反转 | 「可结转」且引用新原文 | `examples/case_c2_content_swap/` |

**材料要求**：`examples/<case>/` 下 `input.json` + `output.json`（机读）+ `output.md`（人读）+ `trace.jsonl`；`examples/test-results.txt` 为 `pytest -v` 原始输出。

---

## 7. 目录结构（v2 修正：补 `.gitignore`、`src/eval.py`、`src/http_app.py`、`src/normalize.py`、`data/regression/`、`examples/test-results.txt`）

```
xiaoying-knowledge-qa-agent/
├── README.md                  # 首屏：3 步跑通 + 评审导航
├── PLAN.md                    # 本文件（交付时移至 docs/00-plan.md）
├── requirements.txt
├── .env.example
├── .gitignore
├── src/
│   ├── __init__.py
│   ├── cli.py                 # CLI 入口
│   ├── http_app.py            # 极简 HTTP（stdlib）
│   ├── agent.py               # ReAct 主循环 + 上限 + 状态判定
│   ├── actions.py             # 动作 schema + 参数校验
│   ├── llm.py                 # real(OpenAI 兼容) / mock(规则决策器)
│   ├── tools.py               # search_docs / read_chunk / list_versions + 故障注入
│   ├── normalize.py           # 归一化 / 中文数字 / n-gram / 数值事实
│   ├── validator.py           # 两级校验 + 未处理冲突
│   ├── trace.py               # JSONL trace
│   └── eval.py                # 评测集跑分
├── tests/
│   ├── test_normalize.py
│   ├── test_tools.py
│   ├── test_validator.py
│   ├── test_agent_flows.py    # mock 端到端
│   ├── test_acceptance.py     # 对 §6 逐条断言
│   ├── test_content_swap.py   # 内容替换回归
│   └── test_http.py           # HTTP smoke
├── scripts/make_examples.py   # 批量生成 examples（可复现）
├── data/
│   ├── docs/D01..D08.json
│   ├── corpora/{full,conflict_min,no_claim}.json
│   ├── regression/{d01_45days,d02b_scope_removed,d03_carryover_allowed}/D0x.json
│   └── eval_set.jsonl
├── examples/
│   ├── <case>/{input.json,output.json,output.md,trace.jsonl}
│   └── test-results.txt
└── docs/{01-requirements,02-solution,03-evaluation,04-ai-usage,05-retrospective}.md
```

---

## 8. 测试计划

| 层 | 文件 | 覆盖 |
|---|---|---|
| 单元 | `test_normalize.py` | NFKC、全半角、中文数字、bigram、fact 抽取 |
| 单元 | `test_tools.py` | 命中/空/废止过滤/非法参数/故障注入/conflict_hint |
| 单元 | `test_validator.py` | 三类错误码 + 归一化边界（中文数字引用阿拉伯原文） |
| 端到端(mock) | `test_agent_flows.py` | A1/A2/A2b/A3/A4/A5/A6/B1 全路径 |
| 验收 | `test_acceptance.py` | §6 表逐条断言（材料文件存在性 + 内容断言） |
| 回归 | `test_content_swap.py` | C1/C2：换内容 → 答案与动作改变 |
| 冒烟 | `test_http.py` | POST /ask 返回结构正确 |

`pytest -v` 输出重定向到 `examples/test-results.txt`（考卷要求测试结果落在 examples/）。

---

## 9. 文档计划

| 文档 | 必写 |
|---|---|
| `01-requirements` | 用户画像、3 个真实场景、范围与非目标、可量化成功标准 |
| `02-solution` | 调用流程、动作空间、动态决策点、**为什么不用框架/为什么 n-gram/为什么校验器做成工具**、模型与版本、框架声明（本项目：无框架） |
| `03-evaluation` | 预期 vs 实际、逐条引用 `examples/` 与 `test-results.txt`、失败案例、**已知限制**（mock 边界、只引一条时的冲突盲区、n-gram 召回上限） |
| `04-ai-usage` | AI 辅助内容（本对话/编码助手）、个人决策与修改、如何验证（pytest + 真模型 trace 对比） |
| `05-retrospective` | 最难的三件事、实际用时、未完成部分、下一步（embedding、rerank、权限隔离、评测扩量） |

---

## 10. 里程碑与计时

| 阶段 | 内容 | 产出 | 实际用时（待填） |
|---|---|---|---|
| P1 地基 | docs 数据 + normalize + tools + validator + 单测 | pytest 绿 | |
| P2 闭环 | mock 决策器 + agent 主循环 + trace + CLI | 6 用例离线跑通 | |
| P3 真模型 | 接 OpenAI 兼容 API，产出 A2/A2b/A6 真 trace，对比 mock | `examples/` 真记录 | |
| P4 材料 | examples 全套 + test-results + 内容替换回归 | 可复核材料 | |
| P5 文档与提交 | 5 篇文档 + README + git 提交与 SHA | 交付 | |

**验收标准**：a) `pytest` 全绿；b) 一条命令看到 `conflict_pending`；c) ≥3 组（实际 14 组）输入/输出/trace 齐全且 03 引用得到；d) 无密钥入库；e) 真模型两条不同走向 trace。

---

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| 只有 mock 被判「预设决策」 | mock 决策器**从 observation 抽数值判断**（不查问题文本表），且真模型 trace 必须齐 |
| 真模型不稳定 / 无 Key | `--model mock` 全链路可跑；文档写明 Mock 边界；真模型 trace 单独标注模型名与时间 |
| 内容替换后答案不变（硬编码嫌疑） | `--docs-dir` 叠加 + `data/regression/` + `test_content_swap.py` + 评测断言 |
| prompt 里泄漏答案事实 | **system prompt 禁止出现任何具体数字/期限样例**，只写规则 |
| n-gram 召回差 | 同义词表 + 评测集量化 + 失败样本驱动改 query；预留 embedding 接口 |
| 校验器误杀 | 只校验 quote 子串 + 数值 grounding，不做语义一致性 |
| 步数上限 demo 在真模型下不可控 | 用 `--max-steps/--max-tool-calls` 强制构造，文档标注「构造用例」 |
| GitHub `api.github.com` 直连不通 | 仅本地 `git init` + commit；push 由本人执行或走代理 |
| 隐私/密钥 | `.gitignore` 排除 `.env`、`*.trace.jsonl` 里的 key 永不落盘 |

---

## 12. 提交 checklist

- [ ] 公开 GitHub 仓库（`Pickachuu/<repo>`，待定）；README 首屏 3 步跑通
- [ ] 最终 commit SHA 写入提交说明
- [ ] 无 `.env` / 真密钥；`.env.example` 完整
- [ ] `examples/` ≥3 组（计划 14 组）+ `test-results.txt`
- [ ] `docs/03-evaluation.md` 每条结论指向具体文件
- [ ] 真模型 ≥2 条不同走向 trace（冲突追问 vs 条件化；失败降级 vs 正常）
- [ ] 步数上限可演示（`incomplete` + 已完成动作）
- [ ] 内容替换回归（C1/C2）通过
- [ ] 实际用时与未完成部分写在 05

---

## 13. 未决事项（需本人确认）

1. **仓库地址**：`Pickachuu` 是 GitHub 用户名还是组织？仓库名建议 `xiaoying-knowledge-qa-agent`，公开还是私有（私有需给评审账号读权限）。
2. **推送通道**：本机无 `gh`，`api.github.com` 直连被 TLS 掐断 → 需要 PAT + 代理，或由本人手动 push。
3. **模型 Key**：推荐主模型 `deepseek-chat`；Key 放 `.env`（不入库），由本人填写。

---

## v2 变更记录（相对 v1）

| # | 改动 | 原因 |
|---|---|---|
| 1 | 五态统一，补 `incomplete` | v1 §2.4 与 §4.4 不一致 |
| 2 | 新增 §4.6 Trace 协议（字段级） | 考卷要求「步骤/参数/返回摘要/后续动作/决策依据」，v1 未定义 |
| 3 | 新增 §3.2 模型选型与 §3.3 运行方式 | 本人提问；同时避免用 reasoner 泄漏思维链 |
| 4 | 新增 §4.10 CLI 接口面（`--docs-dir/--fault/--corpus/--trace-out`） | 无这些参数 examples 无法一键复现 |
| 5 | 新增 §5.4 内容替换夹具 + C1/C2 用例 | 考卷审核要点「替换文档内容核对答案是否改变」v1 完全缺失 |
| 6 | 新增 §5.2 语料子集（`conflict_min`） | 同一问题才能在「纯冲突」与「可解冲突」两条路径下都被验证 |
| 7 | 修 §4.4 `history[-1]` bug | v1 只喂最后一条 observation，多轮会丢 |
| 8 | 校验器新增 `unhandled_conflict` + 共用 normalizer | 防止「引用两份冲突来源却只答一个值」；避免检索与校验归一化不一致 |
| 9 | `conflict_hint` 进 observation | 检索改进证据 + 代码层无法判语义冲突时的兜底线索 |
| 10 | §6 矩阵补齐 A3b/A4a/A4b/A4c/A6b/A7/C1/C2 | v1 只有 6 条，未覆盖「版本意识」「瞬时 vs 持续失败」 |
| 11 | §7 目录树修正 | 补 `.gitignore`、`src/eval.py`、`src/normalize.py`、`src/http_app.py`、`data/regression/`、`examples/test-results.txt` |
| 12 | §10 计时表 | 考卷要求记录实际用时 |

---

## 14. 新对话开场指令（v2）

> 按 `D:\project\xiaoying-knowledge-qa-agent\PLAN.md`（v2）执行。当前进度：P1 进行中。
> 先把 `data/docs/D01..D08.json`、`data/corpora/*.json`、`src/normalize.py`、`src/tools.py`、`src/validator.py`、对应 tests 写完并跑绿；再进 P2。
> 硬约束：工具返回统一 observation 且内部吞异常；system prompt 不得出现任何具体数字样例；所有数值断言必须能被 quote 支持。
