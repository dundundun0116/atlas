"""阶段 F 的三类长程研究 fixture。

三个家族各测一种失败模式：
- `multi_hop`：结论要跨子任务累积（后面依赖前面学到的事实）→ 测记忆是否丢链；
- `conflicting_sources`：同一事实有新旧两个值 → 测过期信息是否被误用；
- `needs_revision`：首轮产出违反约束 → 测 Auditor 打回与修订是否生效。

fixture 是**纯数据**，不依赖模型；harness 用脚本化研究者跑它，所以离线可复现。
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass(frozen=True)
class Subtask:
    """一个研究子任务：要什么、必须包含什么、不许碰什么。"""

    subtask_id: str
    question: str
    required_fact: str
    #: 需要前序子任务已学到的事实（空=本子任务自带证据）
    needs_earlier_fact: str = ""
    #: 受约束禁止的做法；上下文里看不到约束时研究者会违规
    forbidden_method: str = ""
    #: 过期值：上下文里只有它时研究者会误用
    stale_value: str = ""
    #: 判分用的核心断言（真实模型的措辞/LaTeX 与 required_fact 不会逐字一致）
    probe: str = ""


@dataclass(frozen=True)
class Fixture:
    task_id: str
    family: str
    goal: str
    constraints: Tuple[str, ...] = ()
    subtasks: Tuple[Subtask, ...] = ()
    stale_values: Tuple[str, ...] = ()
    #: 需要修订的任务：第一次产出应被判违规，修订后合格
    requires_revision: bool = False
    #: 运行前已存在的记录/摘要（模拟"上一轮留下的旧记忆"）
    seed_records: Tuple[str, ...] = ()
    seed_summaries: Tuple[str, ...] = ()
    #: 每个子任务开始前才可获得的当前证据，避免把未来事实预置给记忆策略。
    step_records: Tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.subtasks:
            raise ValueError(f"{self.task_id}: 没有子任务")
        ids = [s.subtask_id for s in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.task_id}: 子任务 id 重复")
        for subtask in self.subtasks:
            if not str(subtask.required_fact or "").strip():
                raise ValueError(f"{self.task_id}/{subtask.subtask_id}: required_fact 为空")
        if self.step_records and len(self.step_records) != len(self.subtasks):
            raise ValueError(f"{self.task_id}: step_records 必须与子任务数量一致")
        if self.requires_revision and not self.constraints:
            raise ValueError(f"{self.task_id}: requires_revision 但没有约束")


def multi_hop_fixture() -> Fixture:
    """结论跨子任务累积：S2/S3 需要 S1 学到的引理。"""
    return Fixture(
        task_id="multi-hop-bound",
        family="multi_hop",
        goal="证明 L(h^t,w^t) 的上界",
        constraints=("不得使用未证明的引理",),
        # 首问必须能答：真实任务的"S1"同样有检索到的证据，不是凭空知道答案
        seed_records=(
            "fact: 引理 2 给出 L ≤ 2τσ²（来源 Theorem 3, 2312.05134）",
            "fact: 递推式形式为 L_{t+1} ≤ L_t + 4γ（来源 Lemma 4）",
            "fact: 合并后最终常数项为 4γL（来源 Theorem 5）",
        ),
        seed_summaries=(
            "[证据] 引理 2 给出 L ≤ 2τσ²；递推式含 +4γ；最终常数项 4γL",
        ),
        subtasks=(
            Subtask(
                subtask_id="S1",
                question="引理 2 给出的鞅版本界是什么？",
                required_fact="引理 2 给出 L ≤ 2τσ²",
                probe="2τσ²",
            ),
            Subtask(
                subtask_id="S2",
                question="把引理 2 的界代入递推式后得到什么？",
                required_fact="L ≤ 2τσ² + 4γ",
                needs_earlier_fact="引理 2 给出 L ≤ 2τσ²",
                probe="4γ",
            ),
            Subtask(
                subtask_id="S3",
                question="最终常数项是多少？",
                required_fact="常数项为 4γL",
                needs_earlier_fact="L ≤ 2τσ² + 4γ",
                forbidden_method="直接引用未证明的引理 7",
                probe="4γL",
            ),
            Subtask(
                subtask_id="S4",
                question="整条证明依赖哪个主引理？",
                required_fact="主引理是引理 2",
                needs_earlier_fact="引理 2 给出 L ≤ 2τσ²",  # 3 步之前的事实：摘要窗口会丢链
                probe="引理 2",
            ),
        ),
    )


def conflicting_sources_fixture() -> Fixture:
    """同一事实有新旧两版：旧值在上下文里就会被误用。"""
    return Fixture(
        task_id="conflict-constant",
        family="conflicting_sources",
        goal="确定论文中常数项的取值",
        constraints=("以最新修订版为准",),
        stale_values=("4τσ²",),
        seed_records=(
            "fact: 常数项为 2τσ²\n备注: 旧值 4τσ² 已作废（修订版）",
            "fact: 修订版常数项 2τσ² 对应引理 2（来源 修订说明第 2 节）",
        ),
        seed_summaries=("旧摘要: 常数项 4τσ²",),
        subtasks=(
            Subtask(
                subtask_id="S1",
                question="修订版给出的常数项是多少？",
                required_fact="常数项为 2τσ²",
                stale_value="4τσ²",
                probe="2τσ²",
            ),
            Subtask(
                subtask_id="S2",
                question="该常数项与引理编号如何对应？",
                required_fact="对应引理 2",
                needs_earlier_fact="常数项为 2τσ²",
                probe="引理 2",
            ),
        ),
    )


def needs_revision_fixture() -> Fixture:
    """首轮产出违反格式约束 → 必须被打回并修订一次。"""
    return Fixture(
        task_id="revision-format",
        family="needs_revision",
        goal="给出结论并附证据",
        constraints=("结论必须附证据", "不得只给结论"),
        requires_revision=True,
        seed_records=(
            "fact: 主结论 L ≤ 2τσ²，证据见定理 3（来源 2312.05134 第 7 节）",
        ),
        seed_summaries=(
            "[证据] 主结论 L ≤ 2τσ²，证据见定理 3",
        ),
        subtasks=(
            Subtask(
                subtask_id="S1",
                question="结论是什么？",
                required_fact="L ≤ 2τσ²",
                forbidden_method="只给结论",
                probe="2τσ²",
            ),
            Subtask(
                subtask_id="S2",
                question="证据出处在哪里？",
                required_fact="证据见定理 3",
                needs_earlier_fact="L ≤ 2τσ²",
                probe="定理 3",
            ),
        ),
    )


def long_horizon_fixture(steps: int = 12) -> Fixture:
    """长历史召回：12 个参数分散在 12 条证据里，逐条问回去。

    这是"结构化记忆是否让上下文收敛"的判决实验：
    - 全历史策略的上下文随步数线性增长；
    - 图状态策略把事实沉淀进图、原始轨迹按预算裁剪；
    - 摘要策略只留最近两条（且摘要里只有前两个参数的取值）→ 后半段必然丢值。
    """
    facts = [(f"k{i}", round(1 + i * 0.75, 2)) for i in range(1, steps + 1)]
    step_records = tuple(
        f"fact: 参数 {key} = {value:g}（来源 附录 {index + 1}）"
        for index, (key, value) in enumerate(facts)
    )
    seed_summaries = (
        f"[证据] 参数 k1 = {facts[0][1]:g}、k2 = {facts[1][1]:g}，其余参数见附录",
    )
    subtasks = tuple(
        Subtask(
            subtask_id=f"S{index + 1}",
            question=f"参数 {key} 的取值是多少？",
            required_fact=f"参数 {key} = {value:g}",
            probe=f"{value:g}",
        )
        for index, (key, value) in enumerate(facts)
    )
    return Fixture(
        task_id=f"long-horizon-{steps}",
        family="long_horizon",
        goal="汇总附录里的全部参数取值",
        constraints=("必须引用附录中的原值",),
        seed_records=(),
        seed_summaries=seed_summaries,
        step_records=step_records,
        subtasks=subtasks,
    )


FIXTURE_BUILDERS = {
    "multi_hop": multi_hop_fixture,
    "conflicting_sources": conflicting_sources_fixture,
    "needs_revision": needs_revision_fixture,
    "long_horizon": long_horizon_fixture,
}


#: 默认对照集（不含 long_horizon：长任务单独跑，避免混进短任务的均值）
DEFAULT_FAMILIES = ("multi_hop", "conflicting_sources", "needs_revision")


def load_fixtures(families: Tuple[str, ...] = DEFAULT_FAMILIES) -> List[Fixture]:
    """按家族取 fixture；每个都会跑一遍结构校验。"""
    fixtures: List[Fixture] = []
    for family in families:
        builder = FIXTURE_BUILDERS.get(family)
        if builder is None:
            raise ValueError(f"未知 fixture 家族：{family}（可选 {sorted(FIXTURE_BUILDERS)}）")
        fixture = builder()
        fixture.validate()
        fixtures.append(fixture)
    return fixtures


#: 三种记忆策略的固定名字（报告与 replay 都用它做键）
POLICY_NAMES = ("full_history", "narrative_summary", "graph_state")
