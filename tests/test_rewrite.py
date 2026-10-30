from compassrag.llm.client import LLMResponse
from compassrag.query.rewrite import (
    MAX_SUBQUESTIONS,
    build_query_variants,
    decompose,
    hyde,
    parse_subquestions,
)


class ScriptLLM:
    """按序返回文本的假 LLM；记录 tag 与预算。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls.append({"tag": tag, "max_tokens": max_tokens, "messages": messages})
        text = self.texts.pop(0) if self.texts else ""
        return LLMResponse(text=text, model="fake", prompt_tokens=1, completion_tokens=1,
                           total_tokens=2, latency_ms=1.0,
                           finish_reason="stop" if text else "length")


def test_parse_subquestions_handles_noise():
    text = "Sub-questions:\n- Who wrote it?\n  - When was it written?\nnot a bullet\n"
    assert parse_subquestions(text) == ["Who wrote it?", "When was it written?"]
    assert parse_subquestions("no bullets here") == []
    assert parse_subquestions("") == []


def test_decompose_returns_subquestions_and_drops_original():
    llm = ScriptLLM(["- Which country hosted the 1992 Summer Olympics?\n"
                     "- Who was its president in 1992?\n"
                     "- When is the birthday of the creator of Le ruisseau noir?"])
    subs = decompose(llm, "Who was the president of the country that hosted the 1992 Summer Olympics?")
    assert subs == ["Which country hosted the 1992 Summer Olympics?", "Who was its president in 1992?",
                    "When is the birthday of the creator of Le ruisseau noir?"]
    assert llm.calls[0]["tag"] == "decompose"


def test_decompose_caps_and_dedups():
    lines = "\n".join(f"- sub question {i}?" for i in range(6))
    llm = ScriptLLM([lines])
    subs = decompose(llm, "q")
    assert len(subs) == MAX_SUBQUESTIONS


def test_decompose_falls_back_to_original_on_failure():
    llm = ScriptLLM(["", ""])  # 阶梯三级（1024/2048/4096）全空
    assert decompose(llm, "Some question?") == ["Some question?"]
    assert [c["tag"] for c in llm.calls] == ["decompose", "decompose_retry", "decompose_retry"]
    assert [c["max_tokens"] for c in llm.calls] == [1024, 2048, 4096]


def test_hyde_returns_passage_and_empty_on_failure():
    llm = ScriptLLM(["The 1992 Summer Olympics were hosted by Spain, whose president was Felipe Gonzalez."])
    assert hyde(llm, "q").startswith("The 1992")
    assert hyde(ScriptLLM(["", ""]), "q") == ""


def test_build_query_variants_includes_original_subs_and_hyde():
    llm = ScriptLLM(["- Who created Le ruisseau noir?\n- When was that person born?",
                     "Le ruisseau noir was created by Gustave Courbet, born in 1819."])
    variants = build_query_variants(llm, "When is the birthday of the creator of Le ruisseau noir?")
    assert variants[0] == "When is the birthday of the creator of Le ruisseau noir?"
    assert "Who created Le ruisseau noir?" in variants
    assert variants[-1].startswith("Le ruisseau noir was created")
    assert [c["tag"] for c in llm.calls] == ["decompose", "hyde"]
