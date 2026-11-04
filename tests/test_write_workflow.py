"""写操作工作流：写入 → 读回确认，部分生效时自动补偿（设计文档 §5.5）。

这条链的重点不是"能写"，而是**写完外部世界是什么样**：
- 正常路径：写入 + 读回确认都过，任务完成；
- 部分生效：写入步失败，但**补偿自动执行**，世界被收拾干净；
- 补偿不了：交人工，不猜。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.planning.validator import validate_plan
from atlas.planning.write_workflow import build_write_plan
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools.writes import ControllableWriteTool, WriteToolError

VALUE = {"amount": 100, "currency": "CNY"}


def _plan_payload(target="order-1", value=None, approved=None, expect_absent=False):
    payload = build_write_plan(target=target, value=value or VALUE,
                               expect_absent=expect_absent).to_dict()
    for step in payload["steps"]:
        if step["step_id"] == "write":
            # 保留计划里已经声明的参数（例如前置条件），只补执行需要的
            template = dict(step.get("tool_args_template") or {})
            template.update({"target": target, "value": dict(value or VALUE)})
            step["tool_args_template"] = template
            if approved is not None:
                # 不可补偿的副作用要先批准才能派发；替身可补偿，平时不用设
                step["tool_args_template"]["approved"] = approved
        if step["step_id"] == "confirm":
            # 后置条件：读回必须包含写进去的字段
            step["tool_args_template"] = {"target": target,
                                          "expect": dict(value or VALUE)}
    return payload


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "write_flow.db")
    yield database
    database.close()


def _client(db, tool=None):
    return TestClient(create_app(
        GraphRepository(db), write_tool=tool,
        orchestrate_llm=lambda prompt: "{}",
        context_token_counter=lambda text: (len(text) + 3) // 4,
    ))


def _run(client, target="order-1", approved=None, expect_absent=False):
    client.post("/tasks", json={"session_id": "s1", "goal": "写入"})
    submitted = client.post("/tasks/s1/plans",
                            json={"plan": _plan_payload(target, approved=approved,
                                                        expect_absent=expect_absent),
                                  "activate": True}).json()
    plan_id = submitted["plan"]["plan_id"]
    body = client.post(f"/tasks/s1/plans/{plan_id}/advance", json={}).json()
    return plan_id, body


class TestPlanShape:
    def test_plan_validates_with_its_own_tool(self):
        plan = build_write_plan(target="order-1", value=VALUE)
        report = validate_plan(plan, allowed_tools=["demo_writer"])
        assert report.ok, report.error_codes()
        assert report.topological_order == ["write", "confirm"]

    def test_empty_value_is_refused(self):
        with pytest.raises(ValueError):
            build_write_plan(target="order-1", value={})

    def test_missing_target_is_refused(self):
        with pytest.raises(ValueError):
            build_write_plan(target="  ", value=VALUE)

    def test_confirm_reads_target_from_write_output(self):
        plan = build_write_plan(target="order-1", value=VALUE)
        write = plan.step("write")
        confirm = plan.step("confirm")
        assert write.output_named("target") is not None
        assert confirm.input_named("target") is not None


class TestHappyPath:
    def test_write_then_confirm_completes(self, db):
        tool = ControllableWriteTool(mode="ok")
        client = _client(db, tool)
        _, body = _run(client)
        assert body["state"] == "completed"
        assert {step["step_id"]: step["status"] for step in body["steps"]} == {
            "write": "done", "confirm": "done",
        }
        assert tool.world == {"order-1": VALUE}

    def test_tool_call_is_recorded_as_settled(self, db):
        tool = ControllableWriteTool(mode="ok")
        client = _client(db, tool)
        _run(client)
        calls = GraphRepository(db).tool_calls("s1")
        assert [call["status"] for call in calls] == ["settled"]
        assert calls[0]["outcome"] == "applied"


class TestPartialWriteIsCompensated:
    def test_partial_write_leaves_no_residue(self, db):
        tool = ControllableWriteTool(mode="partial")
        client = _client(db, tool)
        _, body = _run(client)

        statuses = {step["step_id"]: step["status"] for step in body["steps"]}
        assert statuses["write"] == "failed", "部分生效不能算完成"
        assert statuses["confirm"] == "blocked"
        assert body["state"] == "failed"
        assert tool.world == {}, "补偿必须把半成品清掉"
        assert "undo:order-1" in tool.operations

    def test_compensation_is_a_linked_new_call(self, db):
        tool = ControllableWriteTool(mode="partial")
        client = _client(db, tool)
        _run(client)
        calls = GraphRepository(db).tool_calls("s1")
        assert len(calls) == 2
        original = next(item for item in calls if not item["compensation_for"])
        compensation = next(item for item in calls if item["compensation_for"])
        assert compensation["compensation_for"] == original["tool_call_id"]
        assert original["status"] == "failed_partial", "原调用历史不能被改写"
        assert compensation["status"] == "settled"

    def test_partial_without_compensation_goes_human(self, db):
        class NoUndo(ControllableWriteTool):
            def undo(self, key):
                raise WriteToolError("invalid_request", "不支持补偿")

        tool = NoUndo(mode="partial")
        tool.SPEC = type(tool.SPEC)(
            name=tool.SPEC.name, version=tool.SPEC.version,
            effect_class=tool.SPEC.effect_class,
            supports_idempotency=tool.SPEC.supports_idempotency,
            supports_lookup=tool.SPEC.supports_lookup,
            supports_compensation=False,
        )
        client = _client(db, tool)
        # 这个工具声明"不可补偿"，所以派发前必须先批准——要测的是**批准之后**
        # 部分生效仍只能交给人，而不是被闸门挡在门外
        _, body = _run(client, approved=True)
        statuses = {step["step_id"]: step["status"] for step in body["steps"]}
        assert statuses["write"] == "needs_human"
        assert any(op.startswith("write:") for op in tool.operations), "确实写过"


class TestGuards:
    def test_write_without_tool_fails_the_step(self, db):
        client = _client(db, tool=None)
        _, body = _run(client)
        statuses = {step["step_id"]: step["status"] for step in body["steps"]}
        assert statuses["write"] == "failed"

    def test_confirm_fails_when_world_is_empty(self, db):
        tool = ControllableWriteTool(mode="ok")
        client = _client(db, tool)
        # 让外部世界在执行过程中被清空：读回就确认不了
        original_write = tool.write

        def write_and_clear(key, value):
            original_write(key, value)
            tool.world.clear()
            return {"applied": dict(value)}

        tool.write = write_and_clear
        _, body = _run(client)
        statuses = {step["step_id"]: step["status"] for step in body["steps"]}
        assert statuses["write"] == "done"
        assert statuses["confirm"] == "failed", "读回对不上就不能算写入成功"

    def test_invalid_binding_is_rejected_before_activation(self, db):
        client = _client(db, ControllableWriteTool())
        client.post("/tasks", json={"session_id": "s1", "goal": "写入"})
        payload = _plan_payload()
        # 把 binding 指向一个 write 没声明的输出
        payload["dependencies"][0]["binding"] = {
            "output_name": "not_declared", "input_name": "target",
        }
        body = client.post("/tasks/s1/plans", json={"plan": payload}).json()
        assert body["plan"]["status"] == "rejected"
        assert "UNKNOWN_OUTPUT_BINDING" in [
            item["code"] for item in body["plan"]["validation"]["issues"]
        ]


class TestPreAndPostconditions:
    """§11 Phase 4：派发前复查外部世界，执行后读回比对。"""

    def test_precondition_violated_blocks_dispatch(self, db):
        tool = ControllableWriteTool(mode="ok")
        tool.world["order-1"] = {"amount": 999}      # 计划时它还不存在
        client = _client(db, tool)
        _, body = _run(client, expect_absent=True)
        statuses = {step["step_id"]: step["status"] for step in body["steps"]}
        assert statuses["write"] == "failed"
        # 关键：**一次写入都没发出去**——不是写完再发现冲突
        # （`observe` 是复查本身，允许；不许出现的是 write）
        assert not any(op.startswith("write:") for op in tool.operations)
        assert "observe:order-1" in tool.operations
        calls = GraphRepository(db).tool_calls("s1")
        assert calls == [], "前置条件不满足时不该产生工具调用"

    def test_precondition_unknown_goes_human(self, db):
        tool = ControllableWriteTool(mode="ok")

        def broken_observe(key):
            raise WriteToolError("timeout", "读不回来")

        tool.observe = broken_observe
        client = _client(db, tool)
        _, body = _run(client, expect_absent=True)
        statuses = {step["step_id"]: step["status"] for step in body["steps"]}
        # 判不了 ≠ 满足：转人工，不许硬写
        assert statuses["write"] == "needs_human"
        assert tool.operations == [], "读不回时连写都不该尝试"

    def test_confirm_compares_written_fields(self, db):
        tool = ControllableWriteTool(mode="ok")
        original_write = tool.write

        def write_something_else(key, value):
            # 服务端写进去了别的值（例如被另一方覆盖）：读回对不上就不能算成功
            tool.world[key] = {"amount": 1}
            return {"applied": dict(value)}

        tool.write = write_something_else
        client = _client(db, tool)
        _, body = _run(client)
        statuses = {step["step_id"]: step["status"] for step in body["steps"]}
        assert statuses["write"] == "done"
        assert statuses["confirm"] == "failed", "读回字段对不上就不能确认"
        tool.write = original_write


class TestReplanIsWired:
    """环境变化要**在主路径上**触发局部重规划，而不是只有库里的函数。"""

    def test_precondition_violation_records_a_revision(self, db):
        tool = ControllableWriteTool(mode="ok")
        tool.world["order-1"] = {"amount": 999}
        client = _client(db, tool)
        _, body = _run(client, expect_absent=True)

        assert body["steps"][0]["status"] == "failed"
        revisions = body["revisions"]
        assert len(revisions) == 1, "触发一次环境变化就该留下一条可解释记录"
        record = revisions[0]
        assert record["trigger"] == "environment_changed"
        assert record["changed_steps"] == ["write"]
        assert record["affected_steps"] == ["confirm"], "下游确认步建立在旧世界上"
        assert record["number"] == 1
        assert record["reason"], "必须能解释为什么重规划"
        # 变更里带着指纹，说明"世界变成了什么样"
        assert record["changes"][0]["target"] == "order-1"

    def test_revision_is_persisted_and_numbered(self, db):
        tool = ControllableWriteTool(mode="ok")
        tool.world["order-1"] = {"amount": 999}
        client = _client(db, tool)
        _run(client, expect_absent=True)
        stored = GraphRepository(db).plan_revisions("s1", "P1")
        assert [item["number"] for item in stored] == [1]
        assert stored[0]["stale_steps"] == [], "下游还没完成，不需要转 stale"
