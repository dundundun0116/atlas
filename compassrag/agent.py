"""agent 主循环。

两种形态：
- scripted（消融关闭态，默认）：固定单轮——检索（多路 RRF）→ 证据 → 生成 → 收手；
- use_decision（① 决策层，S5）：控制流交给 LLM——路由（direct/single/multi）+ 多跳
  子问题计划 → 逐轮检索 + 充分度早停（预算 = max_rounds）→ 生成。

② 改写（use_rewrite）在两种形态下都作用于检索：scripted 用它扩展查询变体，
决策形态下子问题由 planner 产出（同为查询变体）。
"""

from dataclasses import dataclass, field

from .decision.planner import ROUTE_DIRECT, ROUTE_MULTI, make_plan
from .decision.sufficiency import check_sufficiency
from .query.rewrite import build_query_variants
from .retrieval.fusion import near_dup_merge, rrf_fuse

ANSWER_PROMPT = """Answer the question based only on the evidence passages below.
Give a short, direct answer (a few words). If the evidence is insufficient, answer "unknown".

Evidence:
{context}

Question: {question}
Answer:"""

RETRY_NUDGE = ("Conclude now without further analysis. Do not re-read or re-check the passages. "
               "Reply with at most 5 words: the answer if the evidence contains it, otherwise \"unknown\".")


@dataclass
class AgentConfig:
    top_k: int = 10
    max_rounds: int = 1          # scripted：固定一轮；use_decision：检索预算上限
    max_evidence: int = 20       # 送入答案上下文的最大证据块数（多轮累积后截断）
    use_dense: bool = True       # 混合检索：BM25 + 稠密向量两路 RRF（naive 级已含）
    use_rewrite: bool = False    # S4：多跳分解 + HyDE
    use_wiki: bool = False       # S3：wiki 索引视图（已实测无净增益，保留开关作负结果复现）
    use_kg: bool = False         # S3 重构：实体关系图 + PPR 三路等权融合（+③ 级）
    use_decision: bool = False   # S5：LLM 接管控制流（路由 / 计划 / 早停）
    answer_max_tokens: int = 1024                # 含思维链；过小会让推理吃光预算、答案为空
    answer_retry_budgets: tuple = (2048, 4096)   # 空答案重试预算阶梯（实测逐级提高可救回打转）


@dataclass
class AgentResult:
    answer: str
    evidence: list[str]          # 送入上下文的 chunk_id（按融合排名）
    rounds: int                  # 实际检索轮数（direct 路由为 0）
    prompt_tokens: int           # 全部 LLM 调用聚合（含 plan / sufficiency / answer）
    completion_tokens: int
    finish_reasons: list[str] = field(default_factory=list)   # 答案调用各次
    n_calls: int = 1             # 答案调用次数（含重试）
    queries: list[str] = field(default_factory=list)          # 实际用于检索的查询
    n_llm_calls: int = 1         # 全部 LLM 调用次数（成本诊断）
    route: str = ""              # 决策形态下的路由（direct/single/multi）


class Agent:
    """tools: {name: obj.search(query, k) -> [(chunk_id, score)]}；chunk_store: {chunk_id: chunk}。"""

    def __init__(self, llm, tools: dict, chunk_store: dict, config: AgentConfig | None = None):
        self.llm = llm
        self.tools = tools
        self.store = chunk_store
        self.config = config or AgentConfig()

    def answer(self, question: str) -> AgentResult:
        if self.config.use_decision:
            return self._agentic_answer(question)
        variants = build_query_variants(self.llm, question) if self.config.use_rewrite else [question]
        evidence = []
        for v in variants:   # 多查询变体：逐条检索、按首见顺序累积去重
            for cid in self._retrieve(v):
                if cid not in evidence:
                    evidence.append(cid)
        evidence = evidence[: self.config.max_evidence]
        calls = self._answer_with_evidence(question, evidence)
        return AgentResult(answer=calls[-1].text.strip(), evidence=evidence, rounds=1,
                           prompt_tokens=sum(c.prompt_tokens for c in calls),
                           completion_tokens=sum(c.completion_tokens for c in calls),
                           finish_reasons=[c.finish_reason for c in calls],
                           n_calls=len(calls), queries=variants, n_llm_calls=len(calls))

    def _agentic_answer(self, question: str) -> AgentResult:
        """① 决策形态：路由 → 逐轮检索（预算 max_rounds）→ 充分度早停 → 生成。"""
        plan = make_plan(self.llm, question)
        llm_calls = list(plan.calls)
        queries: list[str] = []
        evidence: list[str] = []
        seen: set[str] = set()
        rounds = 0
        if plan.route != ROUTE_DIRECT:
            pending = list(plan.subquestions) if (plan.route == ROUTE_MULTI and plan.subquestions) \
                else [question]
            for _ in range(max(1, self.config.max_rounds)):
                if not pending:
                    break
                q = pending.pop(0)
                queries.append(q)
                for cid in self._retrieve(q):
                    if cid not in seen:
                        seen.add(cid)
                        evidence.append(cid)
                rounds += 1
                suf = check_sufficiency(self.llm, question, self._format(evidence))
                llm_calls.extend(suf.calls)
                if suf.ok:
                    break
                if suf.next_query and suf.next_query not in queries:
                    pending.insert(0, suf.next_query)
        evidence = evidence[: self.config.max_evidence]
        answer_calls = self._answer_with_evidence(question, evidence)
        llm_calls.extend(answer_calls)
        return AgentResult(answer=answer_calls[-1].text.strip(), evidence=evidence, rounds=rounds,
                           prompt_tokens=sum(c.prompt_tokens for c in llm_calls),
                           completion_tokens=sum(c.completion_tokens for c in llm_calls),
                           finish_reasons=[c.finish_reason for c in answer_calls],
                           n_calls=len(answer_calls), queries=queries,
                           n_llm_calls=len(llm_calls), route=plan.route)

    def _retrieve(self, query: str) -> list[str]:
        """单条查询 × 全部工具 → RRF 融合（② 开启时先取宽候选再近重合并）。"""
        routes = [[cid for cid, _ in tool.search(query, k=self.config.top_k)]
                  for tool in self.tools.values()]
        vec_fn = getattr(self.tools.get("dense"), "vector", None) if self.config.use_rewrite else None
        wide = self.config.top_k * 2 if vec_fn is not None else self.config.top_k
        fused = rrf_fuse(routes, top_n=wide)
        if vec_fn is not None:
            fused = near_dup_merge(fused, vec_fn)[:self.config.top_k]
        return [cid for cid, _ in fused][:self.config.top_k]

    def _answer_with_evidence(self, question: str, evidence: list[str]) -> list:
        """答案生成 + 空答案预算阶梯重试；返回各次 LLMResponse（末次为准）。"""
        messages = [
            {"role": "system",
             "content": "You answer multi-hop questions using only the provided evidence."},
            {"role": "user",
             "content": ANSWER_PROMPT.format(context=self._format(evidence), question=question)},
        ]
        resp = self.llm.chat(messages, tag="answer", max_tokens=self.config.answer_max_tokens)
        calls = [resp]
        # 推理链吃光预算（finish_reason=length）时 content 为空；带收尾提示逐级加预算重试，
        # 实测可破"反复复核"式打转；各级仍空则如实留空（finish_reasons 逐次记录，评测期可查）
        for budget in self.config.answer_retry_budgets:
            if resp.text.strip():
                break
            resp = self.llm.chat(messages + [{"role": "user", "content": RETRY_NUDGE}],
                                 tag="answer_retry", max_tokens=int(budget))
            calls.append(resp)
        return calls

    def _format(self, evidence_ids: list[str]) -> str:
        lines = []
        for cid in evidence_ids:
            c = self.store.get(cid)
            if c is not None:
                lines.append(f"[{len(lines) + 1}] {c['title']}: {c['text']}")
        return "\n".join(lines) if lines else "(no evidence retrieved)"
