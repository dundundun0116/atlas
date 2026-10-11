"""图状态快照导出与 CLI 测试。"""

import pytest

from atlas.export import export_graph, graph_to_html, graph_to_svg
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType


@pytest.fixture()
def engine():
    eng = GraphMemoryEngine()
    eng.add_goal("证明主定理")
    s1 = eng.add_subtask("给出上界")
    s2 = eng.add_subtask("证明引理 3")
    eng.link(s2, s1, EdgeType.DEPENDS_ON)
    eng.complete_subtask(s1.id)
    eng.add_constraint("γL ≤ 1/24")
    d1 = eng.add_decision("采用鞅版本引理", reason="变量非独立")
    eng.supersede_with(d1.id, new_content="采用独立版本引理", reason="重读后发现独立性成立")
    eng.add_fact("L 是 L-smooth 常数")
    eng.add_artifact("L(h^t,w^t) ≤ 4γL·E[f(x^0)]")
    return eng


class TestSvg:
    def test_contains_all_node_ids(self, engine):
        svg = graph_to_svg(engine)
        for node_id in engine.nodes:
            assert node_id in svg
        assert svg.startswith("<svg")
        assert svg.rstrip().endswith("</svg>")

    def test_status_colors_and_dashed_edges(self, engine):
        svg = graph_to_svg(engine)
        assert "#e3f2fd" in svg  # done 填充
        assert "#eceff1" in svg  # superseded 填充
        assert "stroke-dasharray" in svg

    def test_deterministic(self, engine):
        assert graph_to_svg(engine) == graph_to_svg(engine)

    def test_empty_graph_is_valid(self):
        svg = graph_to_svg(GraphMemoryEngine())
        assert svg.startswith("<svg") and "</svg>" in svg


class TestHtml:
    def test_contains_legend_and_tables(self, engine):
        html = graph_to_html(engine)
        assert "图状态快照" in html and "<table" in html
        assert "γL ≤ 1/24" in html
        assert "已作废" in html

    def test_escapes_html_content(self):
        eng = GraphMemoryEngine()
        eng.add_fact("<script>alert(1)</script>")
        html = graph_to_html(eng)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestExportAndCli:
    def test_export_svg_and_html(self, engine, tmp_path):
        graph_path = tmp_path / "graph.jsonl"
        engine.save_jsonl(graph_path)
        svg_path = export_graph(str(graph_path), str(tmp_path / "g.svg"))
        html_path = export_graph(str(graph_path), str(tmp_path / "g.html"))
        assert open(svg_path, encoding="utf-8").read().startswith("<svg")
        assert "图状态快照" in open(html_path, encoding="utf-8").read()

    def test_unsupported_suffix_raises(self, engine, tmp_path):
        graph_path = tmp_path / "graph.jsonl"
        engine.save_jsonl(graph_path)
        with pytest.raises(ValueError):
            export_graph(str(graph_path), str(tmp_path / "g.pdf"))

    def test_cli_export(self, engine, tmp_path, capsys):
        from atlas.cli import main

        graph_path = tmp_path / "graph.jsonl"
        engine.save_jsonl(graph_path)
        out = tmp_path / "snap.html"
        assert main(["export", str(graph_path), "--out", str(out)]) == 0
        assert "图状态快照" in out.read_text(encoding="utf-8")
        assert "已导出" in capsys.readouterr().out
