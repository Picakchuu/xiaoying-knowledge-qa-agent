# 企业知识问答 Agent（题目三）

> 小影校招 AI Agent 开发岗 · 项目题 3
> 给员工一个**可追溯到原文**的答案；查不到就说查不到；文档打架就说打架并请求确认。

## 3 步跑通（无需 key）

```powershell
pip install -r requirements.txt
python -m pytest                                   # 75 passed（代码级断言）
python scripts/selfcheck.py                        # 12/12 验收行为自测（状态/引用/不变量）
python -m src.cli --question "报销期限多久？" --corpus conflict_min
```

第三条命令会输出 `conflict_pending`：两份制度（30 天 / 15 天）都没有写适用范围，Agent **不会擅自选一个**，而是列出两份原文并请求确认。

想直接看真实模型的执行记录（无需 key）：

```powershell
python -m src.cli --input examples/case_a2_conflict/input.json --model mock --json
type examples\case_a2_conflict\output.md
type examples\case_a2c_scoped_answered\output.md
```

## 关键设计（为什么值得看）

| 设计 | 一句话 |
|---|---|
| **模型只提议，代码终审** | 模型说 `answered` 不算数：quote 必须逐字命中原文、回答里每个数字都要有出处、引了两份互斥来源就必须并列说明，否则打回 |
| **弱模型也会被纠偏** | 检索线索显示 15 天 vs 30 天时，回答只写一个值又不说适用范围 → `cherry_picked_conflict` 打回；实测同一个弱模型因此从"擅自答 15 天"变成"列三份原文并追问" |
| **冲突不擅自裁决** | 两份来源冲突且无适用范围 → `ask_user` 列出双方原文与版本日期；能查到适用范围就条件化回答 |
| **检索线索给模型看** | `search_docs` 会返回「涉及文档」清单与 `conflict_hint`（同单位不同数值），让模型知道"还有一份总部专用版" |
| **观察 → 调整** | 工具失败/校验驳回都变成可回灌的 observation，模型下一步自己换 query、改引用或转追问 |
| **双上限 + 五态** | `MAX_STEPS=12` / `MAX_TOOL_CALLS=8`；超限返回 `incomplete`，绝不编答案 |

## 目录

```text
src/          agent 主循环、动作 schema、工具、校验器、模型接入、CLI、HTTP
data/         9 篇合成文档 + 3 套语料 + 3 个内容替换夹具
tests/        75 条测试（检索/校验/主循环/上限/内容替换/HTTP/模型客户端）
examples/     15 组「输入 + 输出 + 逐步 trace」，含 3 条真实模型记录
docs/         需求、方案、评测、AI 使用、复盘、原始方案
scripts/      make_examples.py（批量生成 examples）
```

## 评审导航

| 想确认什么 | 看哪里 |
|---|---|
| 需求与验收点 | [docs/01-requirements.md](docs/01-requirements.md) |
| 决策循环与技术取舍 | [docs/02-solution.md](docs/02-solution.md) |
| 预期 vs 实际、失败与限制 | [docs/03-evaluation.md](docs/03-evaluation.md) |
| 测试原始输出 | [examples/test-results.txt](examples/test-results.txt) |
| 真实模型三条路径 | [examples/case_a2_conflict](examples/case_a2_conflict)、[case_a2b_scoped_conflict](examples/case_a2b_scoped_conflict)、[case_a2c_scoped_answered](examples/case_a2c_scoped_answered) |
| AI 用在哪、改了什么 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 最难的问题与下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |

## 两种运行模式（边界很重要）

| 模式 | 需要什么 | 证明什么 |
|---|---|---|
| `--model mock` | 什么都不需要 | 验证**流程与控制**：动作选择、检索改进、校验打回、上限收尾、内容替换 |
| `--model real` | `.env` 里的 API key | 验证**真实模型决策**：三条真实记录见 `examples/case_a2*` |

Mock 是规则决策器，**不代表真实模型质量**；真实记录是静态产物（评审无 key 也能看），重跑需要 `.env`：

```ini
MODEL_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
MODEL_API_KEY=sk-...
MODEL_MAX_TOKENS=6000      # 推理型模型要给足，否则思维链会吃光输出预算
```

先体检再跑：

```powershell
python -m src.llm --ping          # 看 ok / json_mode / latency / usage
python -m src.cli --question "报销期限多久？" --model real --corpus full --json
```

## 常用命令

```powershell
# 内容替换复核：D01 的 30 天改成 45 天，冲突值应随之改变
python -m src.cli --question "报销期限多久？" --corpus conflict_min --docs-dir data/docs --docs-dir data/regression/d01_45days --json

# 工具故障注入：search_docs 前两次超时
python -m src.cli --question "报销期限多久？" --fault search_docs:timeout:2 --json

# 上限演示
python -m src.cli --question "年假可以攒到明年吗？" --max-steps 3 --max-tool-calls 1 --json

# 多轮（追问）
python -m src.cli --input examples/case_a5_followup/input.json --json

# HTTP 封装 + 浏览器演示页（零依赖，无需构建）
python -m src.http_app --port 8000
#   → 浏览器打开 http://127.0.0.1:8000 直接点示例提问
#   → 或 curl 调 POST /ask：{"question": "报销期限多久？"}

# 重新生成全部 examples
python scripts/make_examples.py --mode mock
```

## 环境

- Python ≥ 3.10（本机 3.13.5）
- 依赖：`requests`、`pytest`（`requirements.txt`）
- 不依赖任何 Agent 框架；真实模型走任意 OpenAI 兼容端点

## 已知限制（详见 docs/03-evaluation.md §5）

- 校验器只做字面核对，不做语义等价判断；
- 2-gram 检索对长尾表述有天花板（同义词表 12 条）；
- 推理型模型单次 1~2.5 分钟，交互场景需要换非推理模型或加流式；
- Mock 只验证流程，不证明真实决策质量。
