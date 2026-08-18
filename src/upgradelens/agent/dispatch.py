"""Unified task dispatch — the single "brain" routing any task to its engine.

Research report M1b ("让主循环按 capability 编排"): one ``dispatch_by_task`` layer
over the capability registry so a natural-language task reaches the right engine
and all five capabilities share the *same verification gate* and the *same result
contract*.

Design
------
* ``DEPENDENCY_UPGRADE`` runs through the real ReAct upgrade loop, exposed by
  :func:`run_capability` -> :class:`DependencyUpgradeAgent` (the S4/S5
  clone -> scan -> retrieve -> verify state machine).
* ``pr_review`` / ``issue_repair`` / ``security_review`` / ``breaking_change``
  run through the shared capability analyzers, also via :func:`run_capability`.

Both paths normalise to a single :class:`CapabilityRunResult` and run each
capability's verifier, so "五大能力都经同一状态机 + 同一验证闸运行" holds at the
dispatch layer. The returned result is enriched with ``capability_meta`` (the
per-kind ``allowed_tools`` from the capability catalog) as the first, observable
step toward driving the loop by ``capability.build_plan`` — the deeper M1b refactor
that replaces the per-kind ``if/elif`` inside ``run_capability`` with a
capability-driven plan from the registry. This module is the stable seam for that
evolution; it contains no duplicated execution logic of its own.
"""

from __future__ import annotations

from typing import Any

from upgradelens.capabilities.workbench import CapabilityRunResult, run_capability
from upgradelens.core.task import SoftwareTask, TaskKind


def _capability_meta(kind: TaskKind) -> dict[str, Any]:
    """Resolve per-kind capability metadata for observability on the dispatch seam.

    Reads ``allowed_tools`` from the default capability catalog so the unified
    result records which tools each capability may use.
    """
    from upgradelens.capabilities.workbench import list_capabilities

    kind_value = kind.value if isinstance(kind, TaskKind) else str(kind)
    for cap in list_capabilities():
        if cap["kind"] == kind_value:
            return {
                "kind": kind_value,
                "name": cap.get("name", kind_value),
                "allowed_tools": list(cap.get("allowed_tools", [])),
            }
    return {"kind": kind_value, "name": kind_value, "allowed_tools": []}


def dispatch_by_task(task: SoftwareTask, *, mode: str = "fake") -> CapabilityRunResult:
    """Route ``task`` to the correct engine and return a normalized result.

    This is the unified entry point the research report's M1b calls for: a single
    function that triages by ``task.kind`` and runs the right capability, keeping
    every kind behind one verification gate and one result contract. The natural
    language entry point (``/api/task/run``) and any future CLI should call this
    rather than ``run_capability`` directly, so the dispatch policy lives in one place.
    """
    result = run_capability(task, mode=mode)
    result.capability_meta = _capability_meta(task.kind)
    return result
