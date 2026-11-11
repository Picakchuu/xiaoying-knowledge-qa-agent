# 真实模型评测报告（real）

- 模型：`openai-compatible:deepseek-chat`
- 时间：2026-11-11 15:21:54
- 用例：14 条｜通过：14/14｜总 tokens：30692｜总耗时：259s

| 用例 | 期望 | 实际 | 步数 | 工具调用 | 引用 | tokens | 耗时 | 结果 |
|---|---|---|---|---|---|---|---|---|
| `case_a1_answered` | answered | answered | 2 | 1 | 1 | 1486 | 5299 ms | PASS |
| `case_a2_conflict` | conflict_pending | conflict_pending | 6 | 5 | 2 | 2407 | 18908 ms | PASS |
| `case_a2b_scoped_conflict` | conflict_pending | conflict_pending | 10 | 8 | 5 | 3370 | 31641 ms | PASS |
| `case_a2c_scoped_answered` | answered | answered | 5 | 2 | 3 | 2808 | 17660 ms | PASS |
| `case_a3_out_of_scope` | insufficient | insufficient | 5 | 4 | 0 | 1014 | 14492 ms | PASS |
| `case_a3b_missing_detail` | insufficient | answered | 4 | 3 | 3 | 2526 | 13016 ms | PASS |
| `case_a4a_paraphrase` | answered | answered | 4 | 2 | 1 | 1831 | 11359 ms | PASS |
| `case_a4c_query_rewrite` | answered | answered | 5 | 3 | 1 | 3292 | 15842 ms | PASS |
| `case_a5_followup` | answered | answered | 3 | 2 | 1 | 1621 | 9408 ms | PASS |
| `case_a6_tool_transient` | conflict_pending | conflict_pending | 8 | 7 | 2 | 2442 | 20929 ms | PASS |
| `case_a6b_tool_down` | failed | failed | 4 | 3 | 0 | 1043 | 48497 ms | PASS |
| `case_a7_deprecated` | answered | answered | 5 | 3 | 2 | 2491 | 14042 ms | PASS |
| `case_c1_content_swap` | conflict_pending | conflict_pending | 6 | 5 | 2 | 2438 | 20536 ms | PASS |
| `case_c2_content_swap` | answered | answered | 5 | 3 | 1 | 1923 | 17569 ms | PASS |

全部用例通过：状态、引用逐字可核、该拒答的零引用、冲突状态带 ≥2 份来源。
