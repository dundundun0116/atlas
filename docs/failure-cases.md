# 三个可复述的故障案例

每个案例都按「现象 → 根因 → 修复 → 回归测试 → 结果」组织，测试路径可直接点开核对。面试时按这个顺序讲，比讲功能更有说服力。

---

## 案例一：进程中断后任务继续执行

**现象**：评测跑到一半服务进程被杀，重启后任务从 PLANNING 重新开始，已经写入的节点和判分记录都还在，但状态机回到起点；重复推进还会把已完成的状态再写一遍。

**根因**：状态只活在内存里，进程退出即丢失；`wrap_user_prompt` 边结转边推进，没有把「当前处于哪个阶段、下一步该谁上场」落盘。恢复时只能靠重跑，而重跑与已有节点冲突。

**修复**：
1. checkpoint 保存 `task_state / graph_version / pending_role / retry_count / context_digest_version`（`atlas/storage/repository.py::save_checkpoint`）；
2. 恢复只允许从 `PAUSED` 继续，且继续一次 —— 第二次调用返回 `state_conflict`（`atlas/runtime.py::resume_task`）；
3. 幂等结果写进事件表 `payload["result"]`，跨进程重放返回原结果（`decisions/D8`）。

**回归测试**：
- `tests/test_api_contract.py::TestRestart::test_task_continues_after_service_restart`
- `tests/test_runtime_api.py::TestPauseResume::test_task_can_only_be_resumed_once`
- `tests/test_repository.py::TestCheckpoint::test_resume_from_checkpoint_after_restart`

**结果**：换连接（等价于新进程）后状态从 `PAUSED` 恢复到 `EXECUTING`，幂等键在重启后仍命中；重复恢复被明确拒绝，而不是静默重跑。

---

## 案例二：重复提交不产生重复状态

**现象**：客户端超时后重试同一请求，图里出现两个内容相同的 Artifact，事件表里出现两条 `add_node`，下游 Auditor 拿到两份草稿互相矛盾。

**根因**：写接口没有幂等键；`role_note` 每次都无条件 `add_node`，把"重试"当成"新写入"。

**修复**：
1. 所有写请求带 `request_id` + `idempotency_key`；结果落事件表并受 `(session_id, idempotency_key)` 唯一约束（`decisions/D8`）；
2. 研究工具调用再加一层缓存与写入守卫（`_research_cache` / `_research_written`），因为工具结果也可能被重放；
3. 修这条时测试立刻抓到过"缓存命中但仍写了一次 Fact"——说明只做缓存不够，写图也要有守卫（`decisions/D6`）。

**回归测试**：
- `tests/test_runtime_api.py::TestFacadeLifecycle::test_create_task_is_idempotent`
- `tests/test_state_machine.py::TestFaultInjection::test_orchestrator_reaches_completed_through_machine`
- `tests/test_evaluation.py::TestFaultInjection::test_duplicate_submission_does_not_duplicate_records`

**结果**：同一幂等键重放返回 `replayed=True`，节点数不变、事件数不变；离线评测里重复提交注入后记录条数完全一致。

---

## 案例三：旧决策污染上下文

**现象**：用户把预算从 5000 改成 8000，计划区里同时出现两条约束，模型按旧的 5000 做规划；更隐蔽的情况是旧摘要里只有"预算 5000"而没有"已作废"标记，新会话直接沿用了它。

**根因**：两件事都没做——① 互斥约束没有冲突检测，程序默认挑一条；② 过期/作废信息没有强制留痕，旧摘要照样能进上下文。

**修复**：
1. 生命周期与接替关系留在节点上（`superseded_by` + 作废原因），旧节点不删除（`decisions/D7`）；
2. 组装前做治理审计：过期节点不进有效计划区，但**必须**进"风险与待确认"段说明原因；互斥约束显式告警、两条都保持 active，不许自动择一；
3. 图状态记忆策略里，登记"旧值 X 已作废"后，含该旧值的摘要不再进入上下文（`atlas/evaluation/policies.py::GraphStatePolicy`）。

**回归测试**：
- `tests/test_context_policy.py::TestAssemblyGovernance::test_stale_constraint_does_not_enter_plan_and_audit_still_passes`
- `tests/test_context_policy.py::TestAudit::test_conflicting_constraints_are_flagged_not_resolved`
- `tests/test_evaluation.py::TestFaultInjection::test_stale_decision_fault_distinguishes_policies`

**结果**：过期约束不再出现在"活跃约束"区但出现在风险段；冲突两条都在、并给出 `约束冲突（key=budget）`；注入旧决策时全历史策略会误用、图状态策略不会——差异可测（离线口径，见 `docs/evaluation.md`）。

---

## 讲这三个案例的原则

1. **先给现象，再给根因**，不要一上来讲架构；
2. **指着测试说话**：每个结论都能在测试文件里找到对应断言；
3. **说清楚边界**：哪些数字来自离线脚手架、哪些来自真实模型（见 `decisions/D9`）。
