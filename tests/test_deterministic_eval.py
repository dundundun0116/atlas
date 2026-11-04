"""确定性长程评测：出题、判分、注入三件事必须各自独立（设计文档 §11 Phase 5）。

这组用例守的是**评测的可信度**，不是功能：

- 同一个种子必须给出同一套题（否则"跑了两遍"没有意义）；
- oracle 不能读 agent 的自我评价（否则 agent 说 ok 就等于成功）；
- 注入的"命中率"必须是算出来的，而且要把"压根没派发到工具"分开记；
- 卡住的题要能真的续跑到终态，且**全程工具只被调用 1 次**（未知不重发）。
"""

import json
import unicodedata

import pytest

from atlas.evaluation.longhorizon import (
    MIN_N_FOR_EFFECT,
    AnswerOracle,
    ApiReader,
    Chunk,
    FaultCounter,
    GeneratedTask,
    SuiteConfig,
    SuiteReport,
    TaskOutcome,
    expectation_for,
    generate_tasks,
    plan_faults,
    run_suite,
)

SMALL = SuiteConfig(count=6, seed=7, fault_every=3)


@pytest.fixture(scope="module")
def report():
    # 同一份配置跑两遍：既拿报告，也顺手验"可重跑"
    return run_suite(SMALL), run_suite(SMALL)


# ---------------------------------------------------------------- 出题


class TestGenerator:
    def test_same_seed_gives_identical_tasks(self):
        first = [task.to_dict() for task in generate_tasks(count=6, seed=7)]
        second = [task.to_dict() for task in generate_tasks(count=6, seed=7)]
        assert first == second

    def test_different_seed_gives_different_tasks(self):
        first = [task.gold for task in generate_tasks(count=6, seed=7)]
        second = [task.gold for task in generate_tasks(count=6, seed=7 + 1)]
        assert first != second

    def test_gold_appears_in_exactly_one_chunk(self):
        for task in generate_tasks(count=8, seed=3):
            hits = [item for item in task.chunks if f"ANSWER:{task.gold}" in item.quote]
            assert len(hits) == 1
            assert task.gold_chunk.chunk_id == hits[0].chunk_id

    def test_distractors_do_not_carry_the_marker(self):
        # 干扰项要是也带 ANSWER 标记，"抄到第一个"就会变成随机事件
        for task in generate_tasks(count=8, seed=3):
            for chunk in task.chunks:
                if chunk is task.gold_chunk:
                    continue
                assert "ANSWER:" not in chunk.quote
                assert any(value in chunk.quote for value in task.distractors)

    def test_task_ids_and_golds_are_unique(self):
        tasks = generate_tasks(count=10, seed=11)
        assert len({task.task_id for task in tasks}) == 10
        assert len({task.gold for task in tasks}) == 10

    def test_question_contains_subject(self):
        for task in generate_tasks(count=4, seed=5):
            assert task.question.endswith("？")
            assert task.gold != ""


# ---------------------------------------------------------------- 判分（独立性）


def _task(answer="1914", distractors=("1802", "1354")) -> GeneratedTask:
    return GeneratedTask(
        task_id="T01", question="某人的出生年份是哪一年？", gold=answer,
        chunks=(Chunk(source="权威", chunk_id="c1",
                      quote=f"某人的出生年份是 ANSWER:{answer}。"),),
        distractors=distractors,
    )


class TestOracle:
    def test_gold_passes(self):
        assert AnswerOracle().grade("答案是 1914 年。", _task()).passed

    def test_missing_gold_fails(self):
        verdict = AnswerOracle().grade("上下文里没有可用证据，无法回答。", _task())
        assert verdict.passed is False
        assert "gold" in verdict.reason

    def test_distractor_present_fails(self):
        # 抄到了干扰值：既出现了 gold 也出现了干扰值 ⇒ 不能算对
        verdict = AnswerOracle().grade("有说是 1914 年，也有说 1802 年。", _task())
        assert verdict.passed is False
        assert "干扰值" in verdict.reason

    def test_empty_answer_fails(self):
        assert AnswerOracle().grade("", _task()).passed is False

    def test_full_width_digits_are_normalized(self):
        # 读法差异（全半角）不该影响判分
        assert unicodedata.normalize("NFKC", "１９１４") == "1914"
        assert AnswerOracle().grade("答案是 １９１４ 年。", _task()).passed

    def test_oracle_ignores_the_agent_verdict(self):
        """地基：判分只看 gold，不读 agent 的自我评价。

        这里连"答案"都不给，只说明 agent 自认为成功——oracle 必须照样判不过。
        """
        oracle = AnswerOracle()
        assert oracle.grade("verdict=ok approved=true", _task()).passed is False


# ---------------------------------------------------------------- 故障注入


class TestFaults:
    def test_plan_is_deterministic_and_covers_every_n(self):
        tasks = generate_tasks(count=9, seed=2)
        plan = plan_faults(tasks, every=3)
        assert list(plan.assignments) == ["T03", "T06", "T09"]

    def test_kinds_rotate(self):
        tasks = generate_tasks(count=6, seed=2)
        plan = plan_faults(tasks, every=3, kinds=("timeout", "invalid_request"))
        assert plan.assignments == {"T03": "timeout", "T06": "invalid_request"}

    def test_every_zero_means_no_faults(self):
        assert plan_faults(generate_tasks(count=6, seed=2), every=0).count == 0

    def test_expectation_follows_fault_semantics(self):
        assert expectation_for("") == "passed"
        assert expectation_for("timeout") == "passed"
        assert expectation_for("invalid_request") == "failed"

    def test_counter_separates_hit_from_missed(self):
        counter = FaultCounter(planned={"T03": "timeout", "T06": "invalid_request"})
        counter.record("T03", "timeout")
        assert counter.hit_rate == 0.5
        assert counter.missed == ["T06"], "计划了但没派发到工具的必须单独记"

    def test_hit_rate_is_none_without_faults(self):
        assert FaultCounter().hit_rate is None


# ---------------------------------------------------------------- 整套跑


class TestSuite:
    def test_rerun_is_byte_identical(self, report):
        first, second = report
        assert first.digest == second.digest
        assert first.question_digest == second.question_digest
        assert [item.to_dict() for item in first.tasks] == [
            item.to_dict() for item in second.tasks]

    def test_clean_tasks_all_pass(self, report):
        first, _ = report
        assert len(first.clean) == 4
        assert first.clean_rate == 1.0

    def test_injected_faults_behave_as_expected(self, report):
        first, _ = report
        assert len(first.faulted) == 2
        assert all(item.as_expected for item in first.faulted)

    def test_timeout_is_reconciled_and_resumed(self, report):
        first, _ = report
        timeout_task = next(item for item in first.tasks if item.fault == "timeout")
        assert timeout_task.needed_resume is True
        assert timeout_task.resumed is True
        assert timeout_task.passed is True
        assert timeout_task.reconciles == 1

    def test_determinate_failure_is_not_retried(self, report):
        first, _ = report
        invalid = next(item for item in first.tasks
                       if item.fault == "invalid_request")
        assert invalid.state == "failed"
        assert invalid.tool_invocations == 1, "确定性失败不该重发"
        assert invalid.passed is False, "拿不到证据就不能算答对"

    def test_unknown_result_is_never_resent(self, report):
        first, _ = report
        # 未知结果那条题恢复后照样只调用了 1 次——对账复用而不是重发
        assert all(item.tool_invocations == 1 for item in first.tasks)

    def test_recovery_rate_has_real_samples(self, report):
        # 手动场景时代这个指标只能是 null；现在有真样本
        first, _ = report
        assert first.resume_required == ["T03"]
        assert first.recovery_rate == 1.0

    def test_fault_hit_rate_is_measured_not_claimed(self, report):
        first, _ = report
        assert len(first.faults.planned) == 2
        assert len(first.faults.hit) == 2
        assert first.faults.missed == []
        assert first.faults.hit_rate == 1.0

    def test_state_is_consistent_everywhere(self, report):
        first, _ = report
        assert first.inconsistent == []
        assert "状态一致" in first.to_markdown()

    def test_context_packages_are_recorded(self, report):
        first, _ = report
        assert any(item.context_digests for item in first.tasks)

    def test_small_sample_reports_mechanism_only(self, report):
        first, _ = report
        assert first.mechanism_only is True
        assert "只报机制验证" in first.to_markdown()
        assert "Wilson" not in first.to_markdown()

    def test_report_serializes_to_json(self, report):
        first, _ = report
        payload = json.loads(json.dumps(first.to_dict(), ensure_ascii=False))
        assert payload["config_fingerprint"] == SMALL.fingerprint()
        assert payload["cohorts"]["clean"]["n"] == 4
        assert payload["faults"]["hit_rate"] == 1.0
        assert payload["consistency"]["violations"] == {}


class TestConfigFingerprint:
    def test_same_config_same_fingerprint(self):
        assert SuiteConfig(count=6, seed=7).fingerprint() == \
            SuiteConfig(count=6, seed=7).fingerprint()

    def test_any_field_change_changes_fingerprint(self):
        base = SuiteConfig(count=6, seed=7).fingerprint()
        assert SuiteConfig(count=6, seed=8).fingerprint() != base
        assert SuiteConfig(count=7, seed=7).fingerprint() != base
        assert SuiteConfig(count=6, seed=7, fault_every=0).fingerprint() != base
        # 换模型当然算换了一套配置
        assert SuiteConfig(count=6, seed=7, model="m1").fingerprint() != base

    def test_model_field_decides_reader_kind(self):
        assert SuiteConfig().reader_kind == "scripted"
        assert SuiteConfig(model="deepseek-v4.1-flash").reader_kind == "api"


class TestReportRendering:
    def test_large_sample_reports_wilson_and_effect(self):
        tasks = [TaskOutcome(task_id=f"T{i:02d}", question="q", gold="1",
                             state="completed", passed=True, expected="passed",
                             tool_invocations=1)
                 for i in range(2)]
        config = SuiteConfig(count=2, seed=1, min_n_for_effect=1,
                             model="fake-model")
        report = SuiteReport(config=config, tasks=tasks, faults=FaultCounter(),
                             question_digest="x", reader_kind="api")
        text = report.to_markdown()
        assert report.mechanism_only is False
        assert "Wilson" in text
        assert "只报机制验证" not in text

    def test_default_threshold_matches_the_design(self):
        assert MIN_N_FOR_EFFECT == 30
        assert SuiteConfig().min_n_for_effect == 30

    def test_scripted_reader_is_always_mechanism_only(self):
        """样本再多也不能把脚本化结果当能力数字——它证明的是机制，不是模型。"""
        tasks = [TaskOutcome(task_id=f"T{i:02d}", question="q", gold="1",
                             state="completed", passed=True, expected="passed")
                 for i in range(40)]
        report = SuiteReport(config=SuiteConfig(count=40, seed=1), tasks=tasks,
                             faults=FaultCounter(), question_digest="x")
        assert report.n >= 30
        assert report.mechanism_only is True
        assert "只报机制验证" in report.to_markdown()

    def test_violations_show_up_in_the_report(self):
        bad = TaskOutcome(task_id="T01", question="q", gold="1", state="paused",
                          expected="passed", issues=["任务没有到终态（paused）"])
        report = SuiteReport(config=SuiteConfig(min_n_for_effect=1), tasks=[bad],
                             faults=FaultCounter(), question_digest="x")
        assert [item.task_id for item in report.inconsistent] == ["T01"]
        assert "✗" in report.to_markdown()


# ---------------------------------------------------------------- 真模型研究者


def _transport(*responses):
    """假的传输层：按顺序返回响应/抛异常，用来测记账与重试（不联网）。"""
    calls = []

    def _send(payload):
        calls.append(payload)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    _send.calls = calls
    return _send


def _reply(content="ok", prompt=10, completion=3, cost=0.0001):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "opencode": {"list_cost": cost}},
    }


class TestApiReader:
    def test_requires_a_model(self):
        with pytest.raises(ValueError):
            ApiReader("")

    def test_records_measured_usage(self):
        send = _transport(_reply("a", 100, 20, 0.001), _reply("b", 200, 40, 0.002))
        reader = ApiReader("m", transport=send)
        assert reader("p1") == "a"
        assert reader("p2") == "b"
        totals = reader.totals
        assert totals["calls"] == 2
        assert totals["prompt_tokens"] == 300
        assert totals["completion_tokens"] == 60
        assert abs(totals["cost_usd"] - 0.003) < 1e-9
        assert totals["source"] == "measured"

    def test_preamble_is_prepended(self):
        send = _transport(_reply())
        ApiReader("m", transport=send)("原始 prompt")
        content = send.calls[0]["messages"][0]["content"]
        assert content.endswith("原始 prompt")
        assert "虚构" in content

    def test_retries_on_server_error_then_succeeds(self):
        import urllib.error

        error = urllib.error.HTTPError("u", 503, "busy", {}, None)
        send = _transport(error, _reply("第二条成功"))
        reader = ApiReader("m", transport=send, max_attempts=3, backoff_s=0)
        assert reader("p") == "第二条成功"
        assert len(send.calls) == 2
        assert reader.totals["calls"] == 1, "失败的尝试不进用量账"

    def test_gives_up_after_max_attempts(self):
        import urllib.error

        error = urllib.error.HTTPError("u", 500, "boom", {}, None)
        reader = ApiReader("m", transport=_transport(error), max_attempts=2, backoff_s=0)
        with pytest.raises(RuntimeError):
            reader("p")

    def test_client_error_is_not_retried(self):
        import urllib.error

        error = urllib.error.HTTPError("u", 400, "bad", {}, None)
        send = _transport(error)
        reader = ApiReader("m", transport=send, max_attempts=3, backoff_s=0)
        with pytest.raises(urllib.error.HTTPError):
            reader("p")
        assert len(send.calls) == 1, "4xx 是调用方的错，重试没意义"

    def test_scripted_run_reports_zero_usage(self, report):
        first, _ = report
        assert first.reader_kind == "scripted"
        assert first.usage == {}
        assert all(item.prompt_tokens == 0 for item in first.tasks)


class TestCounterfactualTasks:
    """题集必须用世界知识里不存在的实体，否则真模型会按常识答、把机制评测带偏。"""

    def test_subjects_are_fictional_devices(self):
        from atlas.evaluation.longhorizon import SUBJECTS

        assert all(any(ch.isdigit() for ch in item) for item in SUBJECTS), \
            "型号里应当带编号，现实中没有同名器件"
        assert not any(item in ("爱因斯坦", "图灵") for item in SUBJECTS)

    def test_gold_chunk_reads_like_an_archive(self):
        for task in generate_tasks(count=4, seed=7):
            quote = task.gold_chunk.quote
            assert "档案" in quote
            assert f"ANSWER:{task.gold}" in quote
