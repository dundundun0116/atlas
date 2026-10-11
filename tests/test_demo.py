"""离线演示测试：脚本化提取器跑通全链路并导出快照。"""

from atlas.demo import run_demo


def test_demo_runs_offline_and_exports_snapshots(tmp_path, capsys):
    out = tmp_path / "graph.html"
    run_demo(str(out), budget=8000)
    printed = capsys.readouterr().out
    assert "## 当前计划与约束" in printed              # 四区组装被打印
    assert "已作废，原因：展会期间房价翻倍" in printed   # 决策变迁进踩坑区
    assert "[D2] 住滨江区" in printed                 # 接替决定在计划区
    assert out.exists() and "Atlas 演示" in out.read_text(encoding="utf-8")
    assert (tmp_path / "graph.svg").exists()
