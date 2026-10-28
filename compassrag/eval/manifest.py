"""Run manifest：一次评测运行的完整身份（M3）。

没有 manifest 的实验结果是**不可复核**的：换过模型、换过语料、换过抽样种子，
数字看起来一样但没有可比性。所以每个 run 必须自带：

* 配置快照（含新增预算字段）与模型/嵌入模型名；
* 语料变体、块数、块 id 指纹（语料被重建能立刻发现）；
* 抽样文件、抽样 id 指纹、随机种子；
* git commit 与工作区是否脏（代码版本）。

manifest 与 trace 分开落盘：manifest 一次一个 run，trace 一题一行。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1
MANIFEST_DIR_DEFAULT = Path("runs") / "manifests"


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                             timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - 没有 git 也要能跑（导出/复现件）
        return ""


def sha1_ids(ids: Iterable[str], *, prefix: str = "") -> str:
    """稳定的集合指纹：顺序无关，但成员变化一定变。"""
    h = hashlib.sha1(prefix.encode("utf-8"))
    for item in sorted({str(i) for i in ids}):
        h.update(item.encode("utf-8") + b"\n")
    return h.hexdigest()[:16]


class RunManifest(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    created_at: str = Field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    benchmark: str
    config_name: str
    config: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    embedding_model: str = ""
    corpus_variant: str = ""
    corpus_chunks: int = 0
    corpus_hash: str = ""
    sample_file: str = ""
    sample_hash: str = ""
    n_questions: int = 0
    seed: int | None = None
    git_commit: str = ""
    git_dirty: bool = False
    notes: dict[str, Any] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        """把"会影响数字的字段"合起来——两次 run 指纹相同才谈得上可比。"""
        payload = {
            "benchmark": self.benchmark,
            "config": self.config,
            "model": self.model,
            "embedding_model": self.embedding_model,
            "corpus_variant": self.corpus_variant,
            "corpus_hash": self.corpus_hash,
            "sample_hash": self.sample_hash,
            "git_commit": self.git_commit,
            "n_questions": self.n_questions,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def build_manifest(*, repo: Path, benchmark: str, config_name: str,
                   config: dict[str, Any], model: str = "", embedding_model: str = "",
                   corpus_variant: str = "", chunk_ids: Sequence[str] = (),
                   sample_file: str = "", sample_ids: Sequence[str] = (),
                   seed: int | None = None, run_id: str = "",
                   notes: dict[str, Any] | None = None) -> RunManifest:
    repo = Path(repo)
    return RunManifest(
        run_id=run_id or f"{benchmark}__{config_name}__{int(time.time())}",
        benchmark=benchmark,
        config_name=config_name,
        config=dict(config),
        model=model,
        embedding_model=embedding_model,
        corpus_variant=corpus_variant,
        corpus_chunks=len(list(chunk_ids)),
        corpus_hash=sha1_ids(chunk_ids, prefix=f"{benchmark}:{corpus_variant}:"),
        sample_file=sample_file,
        sample_hash=sha1_ids(sample_ids, prefix=f"{benchmark}:"),
        n_questions=len(list(sample_ids)),
        seed=seed,
        git_commit=_git(repo, "rev-parse", "--short", "HEAD"),
        git_dirty=bool(_git(repo, "status", "--porcelain")),
        notes=dict(notes or {}),
    )


def manifest_path(run_dir: str | Path, benchmark: str, config_name: str) -> Path:
    return Path(run_dir) / f"{benchmark}__{config_name}.json"


def write_manifest(manifest: RunManifest, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return p


def read_manifest(path: str | Path) -> RunManifest | None:
    p = Path(path)
    if not p.exists():
        return None
    return RunManifest.model_validate_json(p.read_text(encoding="utf-8"))


def comparable(a: RunManifest | None, b: RunManifest | None) -> bool:
    """两个 run 的"影响数字的字段"是否一致（用于拒绝不可比的对照）。"""
    return a is not None and b is not None and a.fingerprint() == b.fingerprint()
