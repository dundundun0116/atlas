"""查询侧改写（②）：多跳分解 + HyDE 假答案改写。

两个组件都是"生成类"调用，沿用项目统一的预算阶梯协议（思维链会吃光小预算）：
首轮 budgets[0]，空输出时带收尾提示逐级加预算重试；任一组件失败只降级为该
变体缺失，绝不让整题失败。
"""

MAX_SUBQUESTIONS = 4

REWRITE_NUDGE = "Output the result now. Do not analyze further."

DECOMPOSE_PROMPT = """Break a multi-hop question into the minimal set of sub-questions needed to answer it.
Each sub-question must be self-contained (name the entities explicitly) and answerable by a single fact.

Example 1:
Question: Who was the president of the country that hosted the 1992 Summer Olympics?
Sub-questions:
- Which country hosted the 1992 Summer Olympics?
- Who was the president of that country in 1992?

Example 2:
Question: When is the birthday of the creator of Le ruisseau noir?
Sub-questions:
- Who created Le ruisseau noir?
- When was that person born?

Rules:
- 1-4 sub-questions; if the question needs no decomposition, output the original question unchanged.
- Output one sub-question per line, each starting with "- ", nothing else.

Question: {question}
Sub-questions:"""

HYDE_PROMPT = """Write a short encyclopedia-style passage (2-3 sentences) that directly answers the question.
It will be used as a search query, so include likely entities, names and dates. Do not hedge.

Question: {question}
Passage:"""


def parse_subquestions(text: str) -> list[str]:
    """从 "- " 行解析子问题；无有效行返回空列表。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("- "):
            q = line[2:].strip()
            if q:
                out.append(q)
    return out


def _chat_ladder(llm, messages, *, tag, budgets=(1024, 2048, 4096), accept):
    """预算阶梯调用：accept(text) 通过即返回文本，否则带收尾提示升级预算重试。"""
    for n, budget in enumerate(budgets):
        msgs = messages if n == 0 else messages + [{"role": "user", "content": REWRITE_NUDGE}]
        resp = llm.chat(msgs, tag=tag if n == 0 else f"{tag}_retry", max_tokens=int(budget))
        if accept(resp.text):
            return resp.text
    return ""


def decompose(llm, question: str, *, max_sub: int = MAX_SUBQUESTIONS) -> list[str]:
    """多跳分解：返回去重截断后的子问题列表（失败/无需分解时为 [question]）。"""
    text = _chat_ladder(llm, [{"role": "system", "content": "You decompose multi-hop questions."},
                              {"role": "user", "content": DECOMPOSE_PROMPT.format(question=question)}],
                        tag="decompose", accept=lambda t: bool(parse_subquestions(t)))
    subs: list[str] = []
    for q in parse_subquestions(text):
        if q.lower() != question.lower() and q not in subs:
            subs.append(q)
    return subs[:max_sub] if subs else [question]


def hyde(llm, question: str) -> str:
    """HyDE 假答案改写：返回一段假想答案段落（失败时为空串）。"""
    return _chat_ladder(llm, [{"role": "system", "content": "You write hypothetical encyclopedia passages."},
                              {"role": "user", "content": HYDE_PROMPT.format(question=question)}],
                        tag="hyde", accept=lambda t: bool(t.strip())).strip()


SOURCE_ORIGINAL = "original"
SOURCE_SUBQUESTION = "subquestion"
SOURCE_HYDE = "hyde"


def build_query_variants_sourced(llm, question: str,
                                 *, max_sub: int = MAX_SUBQUESTIONS) -> list[tuple[str, str]]:
    """`build_query_variants` 的来源标注版：返回 [(查询, 来源)]。

    来源是轨迹的一部分——评测要能回答"这条查询为什么会存在"，否则 HyDE 与子问题
    的贡献无法分开归因。
    """
    variants: list[tuple[str, str]] = [(question, SOURCE_ORIGINAL)]
    seen = {question}
    for q in decompose(llm, question, max_sub=max_sub):
        if q not in seen:
            seen.add(q)
            variants.append((q, SOURCE_SUBQUESTION))
    h = hyde(llm, question)
    if h and h not in seen:
        seen.add(h)
        variants.append((h, SOURCE_HYDE))
    return variants


def build_query_variants(llm, question: str, *, max_sub: int = MAX_SUBQUESTIONS) -> list[str]:
    """检索用查询变体 = 原问题 + 子问题（≤max_sub）+ HyDE 假答案段落。"""
    return [q for q, _ in build_query_variants_sourced(llm, question, max_sub=max_sub)]
