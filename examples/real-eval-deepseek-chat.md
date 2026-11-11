# 真实模型评测报告（real）

- 模型：`openai-compatible:deepseek-chat`
- 时间：2026-11-11 14:56:18
- 用例：14 条｜通过：14/14｜总 tokens：27704｜总耗时：191s

| 用例 | 期望 | 实际 | 步数 | 工具调用 | 引用 | tokens | 耗时 | 结果 |
|---|---|---|---|---|---|---|---|---|
| `case_a1_answered` | answered | answered | 2 | 1 | 1 | 1488 | 4529 ms | PASS |
| `case_a2_conflict` | conflict_pending | conflict_pending | 6 | 5 | 2 | 2284 | 16432 ms | PASS |
| `case_a2b_scoped_conflict` | conflict_pending | conflict_pending | 6 | 5 | 4 | 2488 | 15720 ms | PASS |
| `case_a2c_scoped_answered` | answered | answered | 8 | 6 | 2 | 2305 | 20316 ms | PASS |
| `case_a3_out_of_scope` | insufficient | insufficient | 5 | 4 | 0 | 1012 | 12408 ms | PASS |
| `case_a3b_missing_detail` | insufficient | answered | 4 | 3 | 3 | 2544 | 11234 ms | PASS |
| `case_a4a_paraphrase` | answered | answered | 3 | 2 | 1 | 1613 | 8401 ms | PASS |
| `case_a4c_query_rewrite` | answered | answered | 5 | 3 | 1 | 2813 | 14198 ms | PASS |
| `case_a5_followup` | answered | answered | 3 | 2 | 1 | 1626 | 6489 ms | PASS |
| `case_a6_tool_transient` | conflict_pending | conflict_pending | 8 | 7 | 2 | 2346 | 27581 ms | PASS |
| `case_a6b_tool_down` | failed | failed | 3 | 3 | 0 | 927 | 7183 ms | PASS |
| `case_a7_deprecated` | answered | answered | 5 | 3 | 2 | 2267 | 15171 ms | PASS |
| `case_c1_content_swap` | conflict_pending | conflict_pending | 6 | 5 | 2 | 2298 | 22915 ms | PASS |
| `case_c2_content_swap` | answered | answered | 3 | 2 | 2 | 1693 | 8764 ms | PASS |

全部用例通过：状态、引用逐字可核、该拒答的零引用、冲突状态带 ≥2 份来源。
