"""README 结果表必须与 `runs/qa` 复算结果一致。

写报告最容易犯的错不是算错，而是**改完代码忘记更新表格**。这条测试把 README 的
六行数字和实际 runs 对起来：一旦对不上就红，逼你决定"是数据变了还是文档过期了"。

没有 runs 的环境（比如刚 clone）会跳过，不影响纯代码测试。
"""

import re
from pathlib import Path

import pytest

from compassrag.eval.report import load_rows, summarize

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
QA_DIR = ROOT / "runs" / "qa"

# README 行首标签 → 配置名
ROWS = {
    "naive": "naive",
    "+ 查询改写融合": "plus_rewrite",
    "+ 实体图索引": "plus_kg",
    "+ 检索决策层": "plus_decision",
    "+ 全家桶": "plus_full",
    "+ 证据核验与有限修复": "plus_verify",
}

pytestmark = pytest.mark.skipif(not QA_DIR.exists(), reason="没有 runs/qa，跳过文档一致性检查")


def _numbers(line: str) -> list[float]:
    """从 `| 名字 | 0.300 | 0.440 | ... | 2,162 |` 里抽出数字。"""
    cells = [c.strip().replace("**", "").replace(",", "") for c in line.strip().strip("|").split("|")]
    out = []
    for c in cells[1:]:
        m = re.match(r"^-?[\d.]+$", c)
        out.append(float(c) if m else None)
    return out


def _readme_rows() -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        for label, cfg in ROWS.items():
            if line.replace("**", "").startswith(f"| {label}"):
                found[cfg] = _numbers(line)
    return found


@pytest.mark.parametrize("cfg", list(ROWS.values()))
def test_readme_row_matches_runs(cfg):
    rows = load_rows(QA_DIR / f"musique__{cfg}__samples.jsonl")
    if not rows:
        pytest.skip(f"缺少 {cfg} 的 runs 结果")
    s = summarize(rows)
    expected = [s["quality"]["em"], s["quality"]["relaxed_em"], s["quality"]["f1"],
                s["evidence"]["ev_recall"], s["evidence"]["ev_full_hit"],
                s["cost"]["tokens_per_q"]]
    actual = _readme_rows().get(cfg)
    assert actual, f"README 里找不到 {cfg} 的行"
    for i, (a, e) in enumerate(zip(actual, expected)):
        if a is None or e is None:
            continue
        tol = 1.0 if i == 5 else 0.001      # 最后一列是 token/题，允许整数取整
        assert abs(a - e) <= tol, f"{cfg} 第 {i + 1} 列不一致：README={a} 实际={e:.4f}"
