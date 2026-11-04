"""公开数据集适配与 DAG 消融（设计文档 §11 Phase 5 剩余）。

两组用例守两件事：

- **适配层不许偷偷放宽判分**：gold 不是数值、语料没有 `ANSWER:` 标记、干扰项没给——
  这些都要**拒绝加载**，而不是"尽力而为"。评测里最贵的错误是悄悄变松的判分。
- **消融是同一个研究者跑两条工作流**：换了研究者就不是工作流的差别了；
  而且差距要能被解释（干净题上它们是同价的，差别在机制而不在效果）。
"""

import json

import pytest

from atlas.evaluation.ablation import (
    DAG_WORKFLOW,
    LEGACY_WORKFLOW,
    DualReader,
    run_ablation,
)
from atlas.evaluation.datasets import (
    ANSWER_MARK,
    DatasetFormatError,
    from_record,
    load_jsonl,
    summarize,
    to_records,
)
from atlas.evaluation.longhorizon import generate_tasks


def _record(**overrides):
    record = {
        "id": "pub-1",
        "question": "某器件的标定年份是哪一年？",
        "answer": "1975",
        "contexts": [
            {"source": "权威档案", "text": "档案原文：标定年份为 ANSWER:1975。"},
            {"source": "二手资料", "text": "另有说法提到 2052 年，未标注出处。"},
        ],
        "distractors": ["2052"],
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------- 适配层


class TestDatasetAdapter:
    def test_maps_a_record_into_an_internal_task(self):
        task = from_record(_record())
        assert task.task_id == "pub-1"
        assert task.gold == "1975"
        assert task.distractors == ("2052",)
        assert len(task.chunks) == 2
        assert ANSWER_MARK in task.gold_chunk.quote

    def test_numeric_gold_is_required(self):
        # 文本答案要先定口径再改 oracle，绝不在适配层放宽
        with pytest.raises(DatasetFormatError) as excinfo:
            from_record(_record(answer="Einstein"))
        assert "确定性" in str(excinfo.value)

    def test_missing_answer_marker_is_refused(self):
        with pytest.raises(DatasetFormatError) as excinfo:
            from_record(_record(contexts=["没有标记的一段话"]))
        assert "标记" in str(excinfo.value)

    def test_multiple_markers_are_refused(self):
        with pytest.raises(DatasetFormatError) as excinfo:
            from_record(_record(contexts=[
                "档案 A：ANSWER:1975", "档案 B：ANSWER:2029"]))
        assert "只允许一个" in str(excinfo.value)

    def test_missing_field_names_the_source(self):
        with pytest.raises(DatasetFormatError) as excinfo:
            from_record({"id": "x"}, source="bench.jsonl:3")
        assert "bench.jsonl:3" in str(excinfo.value)

    def test_field_map_allows_renaming(self):
        record = {"qid": "q9", "q": "是哪一年？", "a": "1999",
                  "docs": ["原文 ANSWER:1999"], "wrong": ["2001"]}
        task = from_record(record, fields={"id": "qid", "question": "q",
                                           "answer": "a", "contexts": "docs",
                                           "distractors": "wrong"})
        assert (task.task_id, task.gold, task.distractors) == ("q9", "1999", ("2001",))

    def test_round_trip_keeps_gold_and_chunks(self):
        tasks = generate_tasks(count=2, seed=5)
        record = to_records(tasks)[0]
        restored = from_record(record)
        assert restored.gold == tasks[0].gold
        assert len(restored.chunks) == len(tasks[0].chunks)

    def test_summary_fingerprint_is_stable(self):
        tasks = generate_tasks(count=3, seed=5)
        first, second = summarize(tasks, name="same"), summarize(tasks, name="same")
        assert first.fingerprint == second.fingerprint
        assert first.count == 3


class TestJsonlLoading:
    def _write(self, tmp_path, lines):
        path = tmp_path / "bench.jsonl"
        path.write_text("\n".join(json.dumps(item) if not isinstance(item, str)
                                  else item for item in lines), encoding="utf-8")
        return str(path)

    def test_loads_records_and_skips_blank_lines(self, tmp_path):
        path = self._write(tmp_path, [_record(), "", _record(id="pub-2")])
        tasks = load_jsonl(path)
        assert [task.task_id for task in tasks] == ["pub-1", "pub-2"]

    def test_duplicate_id_is_refused(self, tmp_path):
        path = self._write(tmp_path, [_record(), _record()])
        with pytest.raises(DatasetFormatError) as excinfo:
            load_jsonl(path)
        assert "重复" in str(excinfo.value)

    def test_bad_json_points_at_the_line(self, tmp_path):
        path = self._write(tmp_path, [_record(), "{not json"])
        with pytest.raises(DatasetFormatError) as excinfo:
            load_jsonl(path)
        assert ":2" in str(excinfo.value)

    def test_empty_file_is_refused(self, tmp_path):
        path = self._write(tmp_path, [])
        with pytest.raises(DatasetFormatError):
            load_jsonl(path)


# ---------------------------------------------------------------- 消融


class TestAblation:
    @pytest.fixture(scope="class")
    @classmethod
    def report(cls):
        return run_ablation(generate_tasks(count=2, seed=7))

    def test_both_workflows_run_every_task(self, report):
        assert len(report.by_workflow(DAG_WORKFLOW)) == 2
        assert len(report.by_workflow(LEGACY_WORKFLOW)) == 2

    def test_same_tasks_same_grader(self, report):
        # 两条路径用的是同一批题、同一个 oracle —— 差距不是来自题目
        dag = report.by_workflow(DAG_WORKFLOW)
        legacy = report.by_workflow(LEGACY_WORKFLOW)
        assert [run.task_id for run in dag] == [run.task_id for run in legacy]

    def test_clean_tasks_pass_on_both_paths(self, report):
        for run in report.runs:
            assert run.passed is True, (run.workflow, run.task_id, run.reason)

    def test_clean_tasks_cost_the_same_model_calls(self, report):
        # 这是消融的结论之一：干净题上 DAG 并没有多花模型调用
        dag = sum(run.model_calls for run in report.by_workflow(DAG_WORKFLOW))
        legacy = sum(run.model_calls for run in report.by_workflow(LEGACY_WORKFLOW))
        assert dag == legacy == 6

    def test_legacy_has_no_recovery_entry(self, report):
        text = report.to_markdown()
        assert LEGACY_WORKFLOW in text and DAG_WORKFLOW in text
        assert "没有" in text

    def test_report_serializes(self, report):
        payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
        assert payload["summaries"]["dag"]["passed"] == 2
        assert payload["summaries"]["legacy"]["n"] == 2


class TestDualReader:
    def test_planner_prompt_gets_a_plan(self):
        assert json.loads(DualReader()("You are the PLANNER ..."))["route"]

    def test_worker_prompt_is_not_mistaken_for_planner(self):
        # WORKER 的提示里会出现 "PLANNER" 字样（引用计划），粗匹配会把答案写成计划
        answer = DualReader()("You are the WORKER. Follow the PLANNER's route. ANSWER:1975")
        assert "1975" in answer and "route" not in answer

    def test_auditor_still_gets_json(self):
        assert json.loads(DualReader()("You are the AUDITOR ..."))["verdict"] == "ok"
