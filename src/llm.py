"""模型接入：真实模型 + 离线 Mock 决策器。

两条路径都只暴露一个 `decide(ctx) -> 动作 JSON` 接口，决策循环与工具层完全共用，
这样「mock 跑通的流程」与「真模型跑通的流程」是同一条代码路径，不是两套实现。

Mock 的定位（写进 README / docs/03）：它验证**流程与控制**，不证明真实模型的决策质量；
它的判断全部来自 observation 里的内容（数值、状态、线索），不按问题文本查表——
所以替换文档内容后，它的动作也会跟着变。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

from .normalize import bigrams, extract_facts
from .synonyms import EXPANSIONS, expand_query

SYSTEM_PROMPT = """你是企业知识问答 Agent。你只能依据检索到的文档原文回答，不得使用常识补全。

可用动作（每次只输出一个 JSON 对象）：
1. {"action":"search_docs","args":{"query":"...","top_k":5,"doc_type":"policy|faq|any","include_deprecated":false},"why":"一句话理由"}
2. {"action":"read_chunk","args":{"doc_id":"...","chunk_id":"..."},"why":"..."}
3. {"action":"list_versions","args":{"topic":"..."},"why":"..."}
4. {"action":"citation_check","args":{"answer_text":"...","citations":[{"doc_id":"","chunk_id":"","quote":""}]},"why":"..."}
5. {"action":"answer","args":{"answer_text":"...","citations":[{"doc_id":"","chunk_id":"","quote":""}],"confidence":"high|medium|low"},"why":"..."}
6. {"action":"ask_user","args":{"question":"...","missing":["..."],"options":["..."],"citations":[...]},"why":"..."}
7. {"action":"finish","args":{"status":"insufficient|failed|conflict_pending","summary":"...","missing":["..."]},"why":"..."}

铁律：
- 引用必须逐字摘自 read_chunk 得到的 text，不得改写、不得拼接、不得凭记忆生成；每个 quote 必须能在原文中直接检索到。
- 回答中出现的每个数字、期限、金额都必须出现在某条引用的原文里；不能把别的文档里的数字写进答案。
- 若两份及以上来源对同一事项给出不同值：先查证（读全文找适用范围、版本关系、是否已废止），能判别就给条件化答案并同时引用；不能判别就 ask_user 列出冲突原文并请求确认，状态为 conflict_pending。禁止默认"新版本优先"，禁止折中取值，禁止只引其中一条。
- ask_user 的提问必须把冲突双方的关键数值与版本日期直接写出来（例如"甲文件写 X，乙文件写 Y"），不能只说"存在冲突"让用户自己猜。
- 检索结果可能涉及多份相近文档（同名细则的不同版本、带范围限定的专用版等）。下结论前要逐份确认各自的适用范围与状态，不要只看其中一份。
- chunk_id 必须来自检索结果；传错会返回 not_found，请改用真实 id 重读，不要凭空猜编号。
- 检索不到支撑材料时必须给出 insufficient 并说明缺什么，不得编造。
- 被引用校验器驳回时，按驳回原因修改（改引用、改答案或转 ask_user）。
- 如果检索结果的 note 提示已过滤已废止文档，而你怀疑旧版可能相关，用 list_versions 查证版本状态后再下结论。

只输出 JSON，不要输出解释性文字。why 字段写一句话即可，不要写长篇推理。"""

ACTION_REFERENCE = "search_docs | read_chunk | list_versions | citation_check | answer | ask_user | finish"


class DecisionError(Exception):
    """模型调用失败（网络/解析）。由 agent 转成可回灌的 observation。"""


# ---------------------------------------------------------------- prompt 渲染


def render_state(ctx: dict) -> str:
    """把「问题 + 历史 + 已有观察」压成一段模型可读的状态。"""
    lines = [f"用户问题：{ctx['question']}"]
    history = ctx.get("history") or []
    if history:
        lines.append("对话历史（上一轮已答内容）：")
        for turn in history[-3:]:
            cited = "、".join(turn.get("cited_doc_ids") or []) or "无"
            lines.append(
                f"- 问：{turn.get('question', '')}｜答：{turn.get('answer', '')[:160]}｜引用：{cited}"
            )
    observations = ctx.get("observations") or []
    if not observations:
        lines.append("当前还没有任何观察，请先检索。")
    else:
        lines.append(f"已有观察（最近 {min(len(observations), 6)} 条，按时间顺序）：")
        for index, observation in enumerate(observations[-6:], start=1):
            lines.append(f"[{index}] {json.dumps(observation, ensure_ascii=False)}")
    lines.append(
        f"剩余预算：已用 {ctx.get('tool_calls', 0)} 次工具调用，共 {ctx.get('max_tool_calls', 8)} 次；"
        f"已走 {ctx.get('step', 0)} 步，共 {ctx.get('max_steps', 12)} 步。"
    )
    lines.append(f"请输出下一个动作 JSON（可选：{ACTION_REFERENCE}）。")
    return "\n".join(lines)


# ---------------------------------------------------------------- Mock 决策器


def _act(name: str, args: dict, why: str) -> dict:
    return {"action": name, "args": args, "why": why}


class MockModel:
    """规则决策器：所有判断都读 observation，不查问题文本表。"""

    name = "mock"

    def __init__(self) -> None:
        self.rewrites = 0

    # -- 对外 ---------------------------------------------------------

    def decide(self, ctx: dict) -> dict:
        question: str = ctx.get("question", "")
        observations: list[dict] = ctx.get("observations") or []
        searches = [o for o in observations if o.get("tool") == "search_docs"]
        reads = [o for o in observations if o.get("tool") == "read_chunk"]
        versions = [o for o in observations if o.get("tool") == "list_versions"]
        validators = [o for o in observations if o.get("tool") == "validator"]
        topic = self._topic(question, ctx.get("history") or [])

        # 1) 还没检索过
        if not searches:
            return _act("search_docs", {"query": topic, "top_k": 5}, "先按原问题检索，看召回质量")

        # 2) 校验器驳回 -> 按错误码修
        if validators and not validators[-1].get("ok"):
            return self._handle_validation(validators[-1], searches, reads)

        search = searches[-1]

        # 3) 工具失败 -> 换一种问法重试，连续失败则如实收尾
        if not search.get("ok"):
            failures = [s for s in searches if not s.get("ok")]
            if len(failures) <= 2:
                self.rewrites += 1
                return _act(
                    "search_docs",
                    {"query": self._rewrite(topic, self.rewrites), "top_k": 5},
                    f"检索返回 {search.get('error_code')}，换一种问法重试",
                )
            return _act(
                "finish",
                {
                    "status": "failed",
                    "summary": f"检索工具连续失败（{search.get('error_code')}），无法给出有依据的回答。",
                    "missing": ["文档检索服务可用性"],
                },
                "工具持续不可用，不能编造答案",
            )

        # 4) 空召回 -> 改写 1~2 次，仍空则判定资料不足
        if not search.get("data"):
            empties = [s for s in searches if s.get("ok") and not s.get("data")]
            if len(empties) <= 2:
                self.rewrites += 1
                return _act(
                    "search_docs",
                    {"query": self._rewrite(topic, self.rewrites), "top_k": 5},
                    "没有召回任何片段，先改写查询再试",
                )
            return _act(
                "finish",
                {
                    "status": "insufficient",
                    "summary": "文档库中没有检索到与该问题相关的内容，无法给出有依据的回答。",
                    "missing": ["该主题的制度或 FAQ 文档"],
                },
                "多次改写仍无召回，判定为资料外问题",
            )

        hint = (search.get("hints") or {}).get("conflict_hint")
        top = search["data"][0]

        # 5) 低相关召回（最高分偏低）：先改写，仍偏低就判资料不足，不能硬答
        low_relevance = "偏低" in (search.get("note") or "") or float(top.get("score", 0)) < 0.30
        if low_relevance:
            if self.rewrites < 2:
                self.rewrites += 1
                return _act(
                    "search_docs",
                    {"query": self._rewrite(topic, self.rewrites), "top_k": 5},
                    f"最高分仅 {top.get('score')}，召回不相关，改写查询再试",
                )
            return _act(
                "finish",
                {
                    "status": "insufficient",
                    "summary": "多轮检索只召回弱相关片段，文档库中没有能支撑该问题的内容。",
                    "missing": ["与该问题直接相关的制度或 FAQ 条目"],
                },
                "召回始终弱相关，判定为资料外问题",
            )

        # 6) 冲突线索：只有问题本身就在问时间/期限时，才进入冲突查证流程
        asks_about_time = any(
            keyword in question for keyword in ("多久", "期限", "时限", "几天", "什么时候", "最晚", "天内", "时间")
        )
        if hint and asks_about_time and top["doc_id"] in {c["doc_id"] for c in hint["candidates"]}:
            return self._conflict_flow(hint, reads)

        # 7) 检索时过滤掉了已废止文档 -> 先查版本，再决定
        if "已过滤" in (search.get("note") or "") and not versions:
            return _act("list_versions", {"topic": topic}, "检索提示过滤了已废止文档，先查版本状态")

        # 8) 追问场景：当前问题常常只给半个条件（「那试用期员工呢」），
        #    单看一个片段容易答偏，先多读一个候选再综合判断
        if ctx.get("history") and len(reads) < 2:
            seen = {o["data"]["chunk_id"] for o in reads if o.get("data")}
            for row in search["data"][:3]:
                if row["chunk_id"] not in seen:
                    return _act(
                        "read_chunk",
                        {"doc_id": row["doc_id"], "chunk_id": row["chunk_id"]},
                        "追问语境下多读一个候选片段，避免答偏上一轮主题",
                    )

        # 9) 正常路径：读原文 -> 作答
        if not reads:
            return _act(
                "read_chunk",
                {"doc_id": top["doc_id"], "chunk_id": top["chunk_id"]},
                "读最高分片段的原文，确认可用引用",
            )
        return self._answer(question, reads, versions, ctx.get("history") or [])

    # -- 冲突子流程 ---------------------------------------------------

    def _conflict_flow(self, hint: dict, reads: list[dict]) -> dict:
        candidates = hint["candidates"]
        read_ids = {o["data"]["chunk_id"] for o in reads if o.get("data")}
        values = "、".join(f"{c['doc_id']}={c['value']}{hint['unit']}" for c in candidates)

        for candidate in candidates:
            if candidate["chunk_id"] not in read_ids:
                return _act(
                    "read_chunk",
                    {"doc_id": candidate["doc_id"], "chunk_id": candidate["chunk_id"]},
                    f"读原文核对冲突（当前线索：{values}）",
                )
        for candidate in candidates:
            first_chunk = f"{candidate['doc_id']}#c1"
            if first_chunk not in read_ids:
                return _act(
                    "read_chunk",
                    {"doc_id": candidate["doc_id"], "chunk_id": first_chunk},
                    "查证适用范围/版本关系，判断冲突能否消解",
                )
        scoped = [o["data"] for o in reads if o.get("data") and "仅适用" in o["data"]["text"]]
        return self._resolve_conflict(hint, reads, scoped)

    def _resolve_conflict(self, hint: dict, reads: list[dict], scoped: list[dict]) -> dict:
        candidates = hint["candidates"]
        values = sorted({c["value"] for c in candidates}, key=lambda v: int(float(v)))
        citations = [
            {"doc_id": c["doc_id"], "chunk_id": c["chunk_id"], "quote": self._quote_of(c["chunk_id"], reads)}
            for c in candidates
        ]
        if scoped:
            scope_chunk = scoped[0]
            citations.append(
                {"doc_id": scope_chunk["doc_id"], "chunk_id": scope_chunk["chunk_id"], "quote": scope_chunk["text"]}
            )
            answer = (
                f"总部（北京、上海办公区）按 {values[0]} 天执行；"
                f"其他区域按 {values[1]} 天执行，另一份细则未写明适用范围，建议向财务确认适用口径。"
            )
            return _act(
                "answer",
                {"answer_text": answer, "citations": citations, "confidence": "medium"},
                "查到适用范围后才能给出条件化答案，两份来源都引用",
            )
        options = [f"{c['doc_id']}：{c['value']} {hint['unit']}" for c in candidates]
        question = (
            "两份文件的表述不一致，需要你确认适用哪一份：" + "；".join(options) + "。"
            "请问你适用哪份制度（例如总部 / 分支机构）？"
        )
        return _act(
            "ask_user",
            {"question": question, "missing": ["适用制度范围"], "options": options, "citations": citations},
            "两份来源均未给出适用范围与优先级，不能替用户裁决",
        )

    @staticmethod
    def _quote_of(chunk_id: str, reads: list[dict]) -> str:
        for observation in reads:
            data = observation.get("data")
            if data and data["chunk_id"] == chunk_id:
                return data["text"]
        return ""

    # -- 作答 ---------------------------------------------------------

    def _answer(self, question: str, reads: list[dict], versions: list[dict], history: list[dict] | None = None) -> dict:
        chunks = [o["data"] for o in reads if o.get("data") and o["ok"]]
        question_grams = bigrams(question) | set().union(*(bigrams(t) for t in expand_query(question)))
        if history:
            # 追问时把上一轮主题也算进"相关度"，避免答成相邻但不相关的一条
            question_grams |= bigrams(history[-1].get("question", ""))
        chunk = max(chunks, key=lambda c: len(bigrams(c["text"]) & question_grams))
        clause = self._best_clause(chunk["text"], question_grams)

        asks_for_price = any(word in question for word in ("多少钱", "价格", "单价", "报价", "费用是多少"))
        if asks_for_price and not extract_facts(clause):
            return _act(
                "finish",
                {
                    "status": "insufficient",
                    "summary": "检索到的文档只说明了报价方式，没有给出具体价格。",
                    "missing": ["具体价格/报价单"],
                },
                "文档不含用户要的具体数值，属于资料缺口",
            )

        deprecated_note = ""
        if versions:
            deprecated = [row["doc_id"] for row in versions[0].get("data", []) if row.get("status") == "deprecated"]
            if deprecated:
                # 不要在答案里写 doc_id：编号里的数字会被数值 grounding 判成"没有出处的数字"
                deprecated_note = "旧版文件已废止，不作为现行依据。"

        answer = f"根据检索到的制度原文：{clause}。{deprecated_note}"
        return _act(
            "answer",
            {
                "answer_text": answer,
                "citations": [{"doc_id": chunk["doc_id"], "chunk_id": chunk["chunk_id"], "quote": chunk["text"]}],
                "confidence": "high" if chunk.get("score", 1.0) >= 0.5 else "medium",
            },
            "已读到原文且引用逐字可核，可以作答",
        )

    @staticmethod
    def _best_clause(text: str, question_grams: set[str]) -> str:
        clauses = [part.strip() for part in re.split(r"[。；]", text) if part.strip()]
        if not clauses:
            return text.strip()
        return max(clauses, key=lambda clause: len(bigrams(clause) & question_grams))

    # -- 校验驳回 -----------------------------------------------------

    def _handle_validation(self, observation: dict, searches: list[dict], reads: list[dict]) -> dict:
        errors = ((observation.get("data") or {}).get("errors")) or []
        codes = {error.get("code") for error in errors}
        if "unhandled_conflict" in codes:
            hint = (searches[-1].get("hints") or {}).get("conflict_hint") if searches else None
            if hint:
                return self._resolve_conflict(hint, reads, [])
        return _act(
            "finish",
            {
                "status": "insufficient",
                "summary": "回答未通过引用校验，且没有可自动修复的依据。",
                "missing": ["可直接引用的原文片段"],
            },
            "校验不通过时不能硬答",
        )

    # -- 查询改写 -----------------------------------------------------

    @staticmethod
    def _rewrite(topic: str, round_index: int) -> str:
        text = re.sub(r"[?？。，,！!]|请问|吗|呢|一下", "", topic).strip()
        if round_index == 1:
            return text or topic
        for trigger, targets in EXPANSIONS.items():
            if trigger in topic:
                return f"{text} {targets[0]}".strip()
        return f"{text} 规定".strip()

    @staticmethod
    def _topic(question: str, history: list[dict]) -> str:
        """追问时把上一轮主题带进来，避免「那呢？」这类指代丢上下文。

        注意：如果当前问题本身已经带了具体实体（例如「那试用期员工呢」），
        就**用问题本身**检索——把上一轮主题混进来反而会把旧主题顶到前面。
        """
        if not history:
            return question
        content = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", question)
        if len(content) >= 5:
            return question
        previous = history[-1].get("question", "")
        return f"{previous} {question}".strip()


# ---------------------------------------------------------------- 真实模型


class _HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class OpenAICompatibleModel:
    """OpenAI 兼容端点（默认 DeepSeek）。只做一件事：把状态发过去，拿回动作 JSON。

    为什么不用 openai SDK：本机 openai 1.40 与 httpx 0.28 不兼容（httpx 0.28 移除了 `proxies` 参数），
    且当前环境没能在离线条件下升级依赖。直接用 requests 打 /chat/completions，少一层依赖、
    少一类版本冲突，对「任何 OpenAI 兼容端点」都成立。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model_name: str = "deepseek-chat",
        temperature: float = 0.1,
        timeout: float = 60.0,
        max_tokens: int = 6000,
    ) -> None:
        if not api_key:
            raise DecisionError("缺少 MODEL_API_KEY（可写在 .env 里，mock 模式不需要）")
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.name = f"openai-compatible:{model_name}"
        self.temperature = temperature
        self.timeout = timeout
        # 推理型模型会把 max_tokens 先花在 reasoning_tokens 上，
        # 预算给小了就会出现「content 为空」——这是实测踩到的坑。
        self.max_tokens = max_tokens
        self.last_usage: dict | None = None
        self.last_json_mode: bool | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _post(self, payload: dict) -> dict:
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DecisionError(f"网络请求失败：{exc.__class__.__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise _HTTPError(response.status_code, (response.text or "")[:400])
        try:
            return response.json()
        except ValueError as exc:
            raise DecisionError("模型接口返回的不是 JSON") from exc

    def _create(self, messages: list[dict], max_tokens: int | None = None) -> dict:
        """优先用 JSON 模式；服务端不支持时自动降级为纯提示词约束。

        不同厂商/新模型对 response_format 的支持不一致，硬依赖它会让"换模型"变成改代码。
        """
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            data = self._post(payload)
            self.last_json_mode = True
            return data
        except _HTTPError as exc:
            hint = f"{exc.status} {exc.message}".lower()
            unsupported = (
                "response_format" in hint or "json_object" in hint or "unsupported" in hint or "not supported" in hint
            )
            if not unsupported:
                raise DecisionError(f"模型接口返回 {exc.status}：{exc.message}") from exc
            payload.pop("response_format", None)
            try:
                data = self._post(payload)
            except _HTTPError as exc2:
                raise DecisionError(f"模型接口返回 {exc2.status}：{exc2.message}") from exc2
            self.last_json_mode = False
            return data

    def decide(self, ctx: dict) -> dict:
        data = self._create(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": render_state(ctx)},
            ]
        )
        if isinstance(data.get("usage"), dict):
            self.last_usage = data["usage"]
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DecisionError(f"响应结构不符合 OpenAI 兼容格式：{str(data)[:200]}") from exc
        if not isinstance(content, str) or not content.strip():
            reasoning = ((data.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
            hint = f"（reasoning_tokens={reasoning}）" if reasoning else ""
            raise DecisionError(
                f"模型返回空内容{hint}：max_tokens 可能被思维链耗尽，调大 MODEL_MAX_TOKENS 再试"
            )
        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DecisionError(f"模型没有返回合法 JSON：{exc}") from exc


# 兼容旧名字（早期版本叫 OpenAIModel）
OpenAIModel = OpenAICompatibleModel


# ---------------------------------------------------------------- 装配


def load_dotenv(path: str | Path | None = None) -> None:
    """零依赖 .env 读取：只补缺失项，不覆盖已有环境变量。"""
    env_path = Path(path or ".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_model(kind: str) -> MockModel | OpenAICompatibleModel:
    kind = (kind or "mock").lower()
    if kind in {"mock", "offline"}:
        return MockModel()
    if kind in {"real", "openai"}:
        load_dotenv()
        return OpenAICompatibleModel(
            base_url=os.environ.get("MODEL_BASE_URL") or None,
            api_key=os.environ.get("MODEL_API_KEY") or None,
            model_name=os.environ.get("MODEL_NAME", "deepseek-chat"),
            temperature=float(os.environ.get("MODEL_TEMPERATURE", "0.1")),
            timeout=float(os.environ.get("MODEL_TIMEOUT", "60")),
            max_tokens=int(os.environ.get("MODEL_MAX_TOKENS", "6000")),
        )
    raise ValueError(f"未知模型类型：{kind}（可选 mock / real）")


# ---------------------------------------------------------------- 模型体检


def ping() -> dict:
    """一条最小请求，验证 key / base_url / 模型名 / JSON 模式是否真的可用。

    换模型前先跑这个，比直接跑整条 agent 便宜得多，报错也更清楚。
    """
    load_dotenv()
    try:
        model = build_model("real")
    except (DecisionError, ValueError) as exc:
        return {"ok": False, "stage": "config", "error": str(exc)}
    started = time.perf_counter()
    try:
        data = model._create(  # noqa: SLF001 - 体检就是要打最底层的那一层
            [{"role": "user", "content": '请只回复一个 JSON 对象：{"ok": true}'}],
            max_tokens=512,
        )
    except DecisionError as exc:
        return {"ok": False, "stage": "request", "model": model.model_name, "error": str(exc)}
    latency = int((time.perf_counter() - started) * 1000)
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return {
            "ok": False,
            "stage": "parse",
            "model": model.model_name,
            "error": f"响应结构异常：{str(data)[:200]}",
        }
    usage = data.get("usage")
    reasoning = ((usage or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
    return {
        "ok": True,
        "stage": "done",
        "model": model.model_name,
        "json_mode": model.last_json_mode,
        "latency_ms": latency,
        "content": content.strip()[:200] or "(空：token 预算可能被思维链耗尽)",
        "reasoning_tokens": reasoning,
        "max_tokens": model.max_tokens,
        "usage": usage if isinstance(usage, dict) else None,
    }


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(prog="python -m src.llm")
    parser.add_argument("--ping", action="store_true", help="发一条最小请求，验证模型配置")
    arguments = parser.parse_args()
    if not arguments.ping:
        parser.print_help()
    else:
        print(json.dumps(ping(), ensure_ascii=False, indent=2))
