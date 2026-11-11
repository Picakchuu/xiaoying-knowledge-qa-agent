# 01 · 需求与范围

> 题目三「企业知识问答 Agent」· 小影校招 AI Agent 开发岗

## 1. 用户与真实场景

**用户**：企业员工（新员工 / HR / 销售）。他们的共同点是：**不愿翻 20 个文档，但更怕被 AI 编一个答案害自己**。

三个典型场景：

| 场景 | 用户行为 | 期望系统行为 |
|---|---|---|
| 报销 | 「报销期限多久？」 | 如果两份制度说法不同，必须指出冲突、列出原文、请求确认；不能随便挑一个 |
| 年假 | 「年假能攒到明年吗？」→「那试用期员工呢？」 | 第一问给有出处的事实；第二问要接住上文主题，不能重新从零开始 |
| 产品 | 「专业版一年多少钱？」 | 文档里没有价格就直说没有，并告诉用户缺什么（报价单/销售联系方式） |

## 2. 输入与材料

- **问题**：单轮自由文本；
- **对话历史**（可选）：最近若干轮 `{question, answer, cited_doc_ids}`，用于指代消解；
- **知识库**：合成的制度 / FAQ 文档，每篇带 `doc_id`、`title`、`version_date`、`doc_type`、`status`（active / deprecated）、`chunks[]`（`chunk_id`、`heading`、`text`）；
- **工具**：`search_docs`（检索）、`read_chunk`（读原文片段）、`list_versions`（按主题反查版本与状态）、`citation_check`（引用自检）。

语料为**纯合成数据**，不含任何真实企业信息、真实人名或隐私数据。

## 3. 交付结果（契约）

一次运行必须给出：

```json
{
  "question": "...",
  "status": "answered | conflict_pending | insufficient | failed | incomplete",
  "answer": "回答正文 / 追问内容 / 无法回答的原因",
  "citations": [{"doc_id": "D01", "chunk_id": "D01#c2", "quote": "逐字原文片段"}],
  "gaps": ["缺什么信息才能回答"],
  "steps": 4, "tool_calls": 3, "model": "mock | openai-compatible:xxx",
  "reason": "为什么得到这个状态（一句话）",
  "trace_path": "examples/.../trace.jsonl"
}
```

**引用规则**：`quote` 必须是 `chunk.text` 的逐字片段，回答里出现的每个数字/期限/金额都必须能在某条 `quote` 里找到出处。

## 4. 范围与非目标

**范围内**

- 单进程 CLI 为主入口，另附一个零依赖 HTTP 薄封装（`POST /ask`）；
- 一套可离线跑的 Mock 决策器，用于复现流程与跑测试；
- 至少一个真实模型的执行记录，证明决策确实由模型驱动；
- 内容替换回归（改文档内容 → 答案与动作必须跟着变）。

**明确不做**（考卷允许，且时间应花在决策质量上）

- 图形界面、线上部署、鉴权、并发限流；
- 多 Agent 协作、生产级 Runtime、长任务编排；
- 向量检索 / rerank（预留接口，不实现，见 `docs/02-solution.md`）；
- 真实企业文档、真实用户数据。

## 5. 成功标准（可量化）

| 指标 | 目标 | 现状 |
|---|---|---|
| 六项必验收覆盖 | 100%（明确答案 / 冲突 / 资料外 / 改写 / 追问 / 工具失败） | 15 组用例，见 `examples/` |
| 无依据回答数 | **0**（关键缺陷，宁可判 insufficient） | 0（校验器拦截 + 评测断言） |
| 引用可核率 | 100%（每条 quote 都能在原文中逐字找到） | 100%（`docs/03-evaluation.md` 有统计） |
| 冲突检出 | 冲突语料下必须 `conflict_pending` 或带并列引用的条件化答案 | 达成（见真实与 mock 记录） |
| 内容替换鲁棒 | 换 D01 的 30 天→45 天、D03 的「不得结转」→「可结转」后答案反转 | 达成（C1/C2） |
| 上限行为 | 超出步数/调用上限必须返回 `incomplete` 且说明已完成动作 | 达成（B1） |
| 测试 | `pytest` 全绿 | 101 passed, 6 skipped（HTTP 端到端在本机沙箱被跳过） |

## 6. 验收点对照（考卷六项 + 附加）

| 编号 | 验收点 | 用例目录 |
|---|---|---|
| A1 | 明确答案 | `examples/case_a1_answered/` |
| A2 | 文档冲突（不可解） | `examples/case_a2_conflict/` |
| A2b | 冲突查证（可解但缺用户范围） | `examples/case_a2b_scoped_conflict/` |
| A2c | 冲突可解（条件已给） | `examples/case_a2c_scoped_answered/` |
| A3 | 资料外问题 | `examples/case_a3_out_of_scope/` |
| A3b | 资料不足（缺具体数值） | `examples/case_a3b_missing_detail/` |
| A4a/A4c | 改写后的相似问题 / 口语改写 | `examples/case_a4a_paraphrase/`、`case_a4c_query_rewrite/` |
| A5 | 上下文追问 | `examples/case_a5_followup/` |
| A6/A6b | 工具失败（瞬时 / 持续） | `examples/case_a6_tool_transient/`、`case_a6b_tool_down/` |
| A7 | 版本意识（已废止文档） | `examples/case_a7_deprecated/` |
| B1 | 步数/调用上限 | `examples/case_b1_limit/` |
| C1/C2 | 内容替换回归 | `examples/case_c1_content_swap/`、`case_c2_content_swap/` |
