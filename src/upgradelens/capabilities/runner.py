"""Capability dispatch (plan stage S7 access layer).

A thin entry point that maps a :class:`TaskKind` to its capability function and runs
it end-to-end in a given gateway mode. The CLI and (later) the Workbench call this; it
performs no analysis of its own and keeps every capability fully offline-capable.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from pydantic import BaseModel

from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode

__all__ = [
    "list_capabilities",
    "build_gateway",
    "build_task_from_inputs",
    "dispatch_capability",
]


def list_capabilities() -> list[dict[str, Any]]:
    """Return a catalog of every registered task capability."""
    from upgradelens.capabilities.defaults import get_default_capabilities

    out: list[dict[str, Any]] = []
    for cap in get_default_capabilities():
        kind = cap.kind.value if isinstance(cap.kind, TaskKind) else str(cap.kind)
        out.append(
            {
                "kind": kind,
                "name": cap.name,
                "description": cap.description,
                "allowed_tools": list(cap.allowed_tools),
            }
        )
    return out


def build_gateway(mode: str = "fake") -> ModelGateway:
    """Build a :class:`ModelGateway` for the requested mode.

    In ``fake`` mode the canned capability responses are loaded so the whole
    pipeline runs offline and deterministically.
    """
    if mode == "fake":
        from upgradelens.llm.fixtures_core import build_fake_core_responses

        return ModelGateway(
            ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""),
            fake_responses=build_fake_core_responses(),
        )
    from upgradelens.config import Settings

    settings = Settings()
    api_key = settings.model_api_key.get_secret_value() if settings.model_api_key else ""
    return ModelGateway(
        ModelConfig(
            mode=ModelMode(mode),
            base_url=settings.model_base_url,
            model=settings.model_name,
            api_key=api_key,
            disable_thinking=settings.model_disable_thinking,
        )
    )


def build_task_from_inputs(
    *,
    kind: str,
    repo: str = "",
    diff: str = "",
    dependency: str = "",
    source_version: str = "",
    target_version: str = "",
) -> SoftwareTask:
    """Build a :class:`SoftwareTask` from CLI-style inputs.

    ``diff`` is intentionally not stored on :class:`TaskContext` (extra fields are not
    type-checked); pass it to :func:`dispatch_capability` directly.
    """
    context = TaskContext(
        repo=str(repo),
        dependency=dependency,
        source_version=source_version,
        target_version=target_version,
    )
    return SoftwareTask(task_id="cli", kind=TaskKind(kind), goal="", context=context)


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / pydantic models into plain JSON."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}  # type: ignore[arg-type]
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="ignore")
    return obj


def dispatch_capability(
    kind: str,
    *,
    repo: str = "",
    diff: str = "",
    gateway: ModelGateway | None = None,
    dependency: str = "",
    source_version: str = "",
    target_version: str = "",
    mode: str = "fake",
    **extra: Any,
) -> dict[str, Any]:
    """Run a capability end-to-end and return a normalized JSON result.

    Supersedes the original two-kind dispatcher: every registered capability is
    now routed through :func:`run_capability` (in ``workbench``) and returned as a
    :class:`CapabilityRunResult` dump. ``extra`` carries capability-specific inputs
    that are not first-class on :class:`TaskContext` (``unified_diff``,
    ``issue_text``, ``from_version``, ``to_version``).
    """
    from upgradelens.capabilities.workbench import run_capability
    from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind

    context = TaskContext(
        repo=str(repo),
        dependency=dependency,
        source_version=source_version,
        target_version=target_version,
        **extra,
    )
    task = SoftwareTask(task_id="dispatch", kind=TaskKind(kind), goal="", context=context)
    result = run_capability(task, gateway=gateway, mode=mode)
    return result.model_dump(mode="json")
