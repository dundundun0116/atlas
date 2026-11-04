"""可执行判据的求值（设计文档 §2.5 / §4.5）。

原则：**可验证优先于自主性**。Step 是否完成由结构化判据决定，不由模型自报。
求值结果三态：

- `True`   —— 判据满足；
- `False`  —— 判据不满足（Step 失败，可重试/重规划）；
- `None`   —— **判不了**（缺字段、需要人、模型自评）：转 `needs_human`，绝不当成通过。

这里只实现可离线、确定性的几种；其余（`environment_predicate` 等）留给后续阶段，
返回 `None` 是诚实行为，不是遗漏。
"""

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from atlas.planning.schemas import Criterion, CriterionKind, Evaluator

#: 一次判据求值的解释（报告与排障要能说清"为什么没过"）。
CriterionResult = Tuple[Optional[bool], str]


def evaluate_criterion(criterion: Criterion, output: Dict[str, Any]) -> CriterionResult:
    """对一条判据求值；返回 (三态结果, 可读理由)。"""
    if criterion.evaluator is Evaluator.MODEL_JUDGE:
        # 模型自评不能单独批准完成（§4.5）；有其它可执行判据时由它们决定。
        return None, "model_judge 不能单独判定完成"
    if criterion.evaluator is Evaluator.HUMAN:
        return None, "需要人工确认"

    payload = output or {}
    config = criterion.config or {}

    if criterion.kind is CriterionKind.JSON_SCHEMA:
        required = list(config.get("required") or [])
        missing = [key for key in required if key not in payload]
        if missing:
            return False, f"输出缺少必需字段 {missing}"
        return True, "必需字段齐全"

    if criterion.kind is CriterionKind.EVIDENCE_SUPPORT:
        refs = payload.get(config.get("field", "refs")) or []
        try:
            min_refs = int(config.get("min_refs", 1))
        except (TypeError, ValueError):
            return None, f"min_refs 配置不可用：{config.get('min_refs')!r}"
        if not isinstance(refs, (list, tuple)):
            return None, "输出里的证据不是列表"
        return (len(refs) >= min_refs,
                f"证据 {len(refs)} 条（要求 ≥{min_refs}）")

    if criterion.kind is CriterionKind.FILE_EXISTS:
        path = str(config.get("path") or "")
        if not path:
            return None, "file_exists 判据缺少 path"
        exists = os.path.exists(path)
        return exists, f"{'存在' if exists else '不存在'}：{path}"

    if criterion.kind is CriterionKind.EXIT_CODE:
        field = str(config.get("field") or "exit_code")
        expected = int(config.get("expected", 0))
        value = payload.get(field)
        if value is None:
            return None, f"输出缺少 {field}"
        return int(value) == expected, f"{field}={value}（期望 {expected}）"

    if criterion.kind is CriterionKind.TEST_RESULT:
        field = str(config.get("field") or "tests_passed")
        value = payload.get(field)
        if value is None:
            return None, f"输出缺少 {field}"
        return bool(value), f"{field}={value}"

    if criterion.kind is CriterionKind.HUMAN_APPROVAL:
        return None, "human_approval 判据必须由人工满足"

    # environment_predicate / constraint_check / file_hash：当前阶段没有求值器
    return None, f"{criterion.kind.value} 判据当前没有确定性求值器"


def evaluate_step(
    criteria: Sequence[Criterion], output: Dict[str, Any]
) -> Tuple[Optional[bool], List[Dict[str, Any]]]:
    """一步的全部 required 判据：全过才 `True`；任一不满足即 `False`；否则 `None`。"""
    details: List[Dict[str, Any]] = []
    verdict: Optional[bool] = True
    for criterion in criteria:
        if not criterion.required:
            continue
        passed, reason = evaluate_criterion(criterion, output)
        details.append({
            "criterion_id": criterion.criterion_id,
            "kind": criterion.kind.value,
            "evaluator": criterion.evaluator.value,
            "passed": passed,
            "reason": reason,
        })
        if passed is False:
            verdict = False
        elif passed is None and verdict is True:
            verdict = None
    return verdict, details


def has_executable_criterion(criteria: Sequence[Criterion]) -> bool:
    return any(item.required and item.is_actionable for item in criteria)
