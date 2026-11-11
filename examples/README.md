# examples/ 索引

每组用例一个目录，包含四类文件：

| 文件 | 内容 |
|---|---|
| `input.json` | 输入：问题、对话历史、语料、故障注入、上限、期望 |
| `output.json` | 该次运行的结构化结果（status / answer / citations / gaps / steps / trace_steps …） |
| `output.md` | 同内容的人读版本 |
| `trace.jsonl` | 逐步执行记录：`action`、`args`、`rationale`（一句话决策依据）、`observation_summary`、`next_action` |

`test-results.txt` 是 `pytest -v` 的原始输出；`real-eval-deepseek-chat.md/.json` 是真模型批量评测报告。

## 用例一览（mock 12 组 + 真模型 3 组）

| 目录 | 验收点 | 输入要点 | 状态 | 模式 |
|---|---|---|---|---|
| `case_a1_answered` | 明确答案 | 年假能否结转 | answered | mock |
| `case_a2_conflict` | 文档冲突 | 报销期限，语料仅 D01/D02 | conflict_pending | **real** |
| `case_a2b_scoped_conflict` | 冲突查证 | 同上，语料含总部专用版 | conflict_pending | **real** |
| `case_a2c_scoped_answered` | 冲突可解 | 用户已限定"总部员工" | answered | **real** |
| `case_a3_out_of_scope` | 资料外 | 公司明年会裁员吗 | insufficient | mock |
| `case_a3b_missing_detail` | 资料不足 | 专业版多少钱（文档无单价） | insufficient | mock |
| `case_a4a_paraphrase` | 改写问法 | 年假没用完能留到明年吗 | answered | mock |
| `case_a4c_query_rewrite` | 检索改写 | 打车（→市内交通费） | answered | mock |
| `case_a5_followup` | 上下文追问 | 那试用期员工呢（带历史） | answered | mock |
| `case_a6_tool_transient` | 工具失败（瞬时） | 前两次检索超时 | conflict_pending | mock |
| `case_a6b_tool_down` | 工具失败（持续） | 检索始终超时 | failed | mock |
| `case_a7_deprecated` | 版本意识 | 纸质发票（旧版已废止） | answered | mock |
| `case_b1_limit` | 步数/调用上限 | `--max-steps 3 --max-tool-calls 1` | incomplete | mock |
| `case_c1_content_swap` | 内容替换 | D01 的 30 天改为 45 天 | conflict_pending | mock |
| `case_c2_content_swap` | 内容替换 | D03 的"不得结转"改为"可结转" | answered | mock |

> 标注 **real** 的三组由真实模型（`deepseek-v4-pro`）产生，是静态记录，评审无 key 也能看。
> 其余为离线 mock 决策器产物，用于复现流程与控制行为（不代表真实模型质量）。

## 重新生成

```powershell
# 离线用例（秒级，无需 key）
python scripts/make_examples.py --mode mock

# 真模型用例（需要 .env 里的 MODEL_API_KEY）
python scripts/make_examples.py --mode real --only a2

# 一次性：全部离线用例 + 真模型用例
python scripts/make_examples.py --mode all
```

## 怎么读 trace

```powershell
python -m src.cli --input examples/case_a2_conflict/input.json --model mock --json --with-trace
```

或直接看 `trace.jsonl`：每条 `type=step` 记录依次给出「动作 → 参数 → 看到了什么 → 下一步」，
`rationale` 是模型自述的一句话理由（不包含内部思维过程）。
