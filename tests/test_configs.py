"""配置文件守卫：`configs/ablation/*.yaml` 必须都能装进 AgentConfig。

改字段名（比如把 use_decision 改成别的）时，这个测试会立刻炸，而不是等到跑数那天
才发现配置被静默忽略。
"""

from pathlib import Path

import pytest
import yaml

from compassrag.agent import AgentConfig

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
FILES = sorted((CONFIG_DIR / "ablation").glob("*.yaml"))


def test_there_are_configs_to_check():
    assert FILES, "configs/ablation 下没有 yaml，测试失去意义"


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_ablation_config_loads_into_agent_config(path):
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "agent" in cfg, f"{path.name} 缺少 agent 段"
    agent_cfg = AgentConfig(**cfg["agent"])       # 未知字段会抛 TypeError
    assert agent_cfg.max_rounds >= 1
    assert agent_cfg.max_evidence >= 1


def test_answer_format_experiment_config_only_differs_by_word_limit():
    base = yaml.safe_load((CONFIG_DIR / "ablation" / "plus_full.yaml").read_text(encoding="utf-8"))["agent"]
    words = yaml.safe_load((CONFIG_DIR / "ablation" / "plus_full_words.yaml").read_text(encoding="utf-8"))["agent"]
    diff = {k for k in set(base) | set(words) if base.get(k) != words.get(k)}
    assert diff == {"strict_answer_words"}, f"B 臂不该有其它差异：{diff}"
    assert words["strict_answer_words"] == 5
