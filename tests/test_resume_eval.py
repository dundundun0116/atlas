import json

from scripts.run_resume_eval import _result_complete


def _manifest(path, **overrides):
    data = {
        "benchmark": "musique",
        "config_name": "naive",
        "corpus_variant": "samples",
        "n_questions": 2,
        "sample_hash": "7d0f47ca7d1f5a99",
        "config": {"strict_answer_words": 5},
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_resume_requires_exact_rows_and_manifest_identity(tmp_path):
    target = tmp_path / "musique__naive__samples.jsonl"
    manifest = tmp_path / "musique__naive.json"
    target.write_text('{"id":"q1"}\n{"id":"q2"}\n', encoding="utf-8")
    _manifest(manifest)
    kwargs = dict(benchmark="musique", config_name="naive", variant="samples",
                  expected_config={"strict_answer_words": 5})
    from compassrag.eval.manifest import sha1_ids
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_data["sample_hash"] = sha1_ids({"q1", "q2"}, prefix="musique:")
    manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
    assert _result_complete(target, manifest, {"q1", "q2"}, **kwargs)

    target.write_text('{"id":"q1"}\n{"id":"q1"}\n', encoding="utf-8")
    assert not _result_complete(target, manifest, {"q1", "q2"}, **kwargs)

    target.write_text('{"id":"q1"}\n{"id":"q2"}\nnot-json\n', encoding="utf-8")
    assert not _result_complete(target, manifest, {"q1", "q2"}, **kwargs)


def test_resume_rejects_stale_manifest(tmp_path):
    target = tmp_path / "result.jsonl"
    manifest = tmp_path / "manifest.json"
    target.write_text('{"id":"q1"}\n', encoding="utf-8")
    _manifest(manifest, n_questions=1, config_name="plus_full")
    assert not _result_complete(
        target, manifest, {"q1"}, benchmark="musique", config_name="naive",
        variant="samples", expected_config={"strict_answer_words": 5})
