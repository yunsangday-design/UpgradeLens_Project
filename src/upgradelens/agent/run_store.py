"""Run artifacts for the ``agent`` command (ROADMAP Step 2).

Every run of ``upgradelens agent`` writes a self-contained directory under
``runs/<run_id>/`` so the result is replayable and auditable:

- ``plan.json``   -- the (currently linear) plan the run followed;
- ``trace.jsonl`` -- one JSON object per tool call (tool/params/status/latency);
- ``report.json`` -- the verified assessment, machine-readable;
- ``assessment.json`` -- the S12 presentation view (flattened code/doc evidence);
- ``report.md``   -- the verified assessment, human-readable;
- ``RUN.md``      -- a human-readable run summary tying the above together.

No secret (API key, token) or raw model prompt is ever written. The store
redacts secret-shaped strings on the way out and the unit tests assert this
cannot silently regress (see ``tests/unit/test_run_store.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from upgradelens.agent.plan import AgentPlan
from upgradelens.core.action import ActionProposal
from upgradelens.core.finding import Finding
from upgradelens.core.task import SoftwareTask
from upgradelens.core.verification import VerificationResult
from upgradelens.report.render import render_markdown, render_plan_markdown
from upgradelens.tools.trace import ToolTrace
from upgradelens.verify.models import VerifiedReport

#: Substrings/shape that look like credentials. Anything matching is replaced
#: with ``***`` before it touches disk.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)\b(bearer|api[_-]?key|apikey|token|secret)\b\s*[:=]\s*\S+"),
)
REDACTED = "***"

#: The fixed, deterministic collection plan used when the planner cannot run
#: (fake mode or a planning failure). It mirrors the tools the ReAct loop drives
#: in live mode; ``clone_repo`` is dropped for local paths at plan-build time.
#: The model analysis itself (the harness's closing step) is intentionally not a
#: plan step -- it is the loop's terminal action, not a collected evidence tool.
DEFAULT_PLAN_STEPS: tuple[dict[str, Any], ...] = (
    {
        "order": 1,
        "tool": "clone_repo",
        "phase": "collect",
        "purpose": "Resolve repo to a local dir (clone if URL; skipped for a local path).",
    },
    {
        "order": 2,
        "tool": "scan_dependency",
        "phase": "collect",
        "purpose": "Scan the dependency manifest(s) for the current version.",
    },
    {
        "order": 3,
        "tool": "scan_code",
        "phase": "collect",
        "purpose": "Collect AST code evidence for the dependency.",
    },
    {
        "order": 4,
        "tool": "retrieve_for_package",
        "phase": "collect",
        "purpose": "Retrieve doc evidence from the ingested store (skipped if no db).",
    },
    {
        "order": 5,
        "tool": "supplement_retrieval",
        "phase": "collect",
        "purpose": "ROADMAP Step 4: assess doc-evidence coverage of code symbols and "
        "run focused supplementary retrieval for any gaps (skipped if no db / no code).",
    },
)


def redact_text(text: str) -> str:
    """Return ``text`` with any secret-shaped substring replaced by ``***``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _redact_value(value: Any) -> Any:
    """Recursively redact strings inside a JSON-serialisable structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(val) for val in value]
    return value


@dataclass
class RunStore:
    """Owns one run directory and writes its artifacts defensively."""

    run_dir: Path
    run_id: str

    @classmethod
    def create(cls, out_dir: Path, run_id: str) -> RunStore:
        run_dir = out_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return cls(run_dir=run_dir, run_id=run_id)

    # -- low-level writers ------------------------------------------------- #

    def _write_json(self, name: str, data: Any) -> None:
        text = json.dumps(_redact_value(data), indent=2, ensure_ascii=False)
        (self.run_dir / name).write_text(text + "\n", encoding="utf-8")

    def _write_text(self, name: str, text: str) -> None:
        (self.run_dir / name).write_text(redact_text(text), encoding="utf-8")

    # -- artifacts --------------------------------------------------------- #

    def write_intent(self, intent: dict[str, Any]) -> None:
        self._write_json("intent.json", intent)

    def write_plan(
        self,
        *,
        intent: dict[str, Any],
        plan: AgentPlan | None = None,
        mode: str = "",
    ) -> None:
        """Persist the (live) plan atomically.

        ``plan`` may be ``None`` (e.g. a non-upgrade intent); we then write an
        empty plan so the artifact shape stays stable and diffable.
        """
        if plan is None:
            plan = AgentPlan(request_id=self.run_id, mode=mode, steps=[])
        payload = plan.to_dict()
        payload["kind"] = intent.get("kind")
        payload["request"] = {
            "repo": intent.get("repo"),
            "dependency": intent.get("dependency"),
            "target_version": intent.get("target_version"),
            "source_version": intent.get("source_version"),
        }
        self._write_json("plan.json", payload)

    def write_trace(self, trace: ToolTrace) -> None:
        events = _redact_value(trace.to_dict())
        lines = [json.dumps(event, ensure_ascii=False) for event in events]
        body = "\n".join(lines)
        self._write_text("trace.jsonl", body + "\n" if body else "")

    def write_report(self, verified: VerifiedReport) -> None:
        self._write_json("report.json", verified.model_dump(mode="json"))
        self._write_text("report.md", render_markdown(verified))

    def write_assessment(
        self, outcome: Any, *, upgrade_plan: Any = None, locale: str = "zh-CN"
    ) -> None:
        """S12: persist the flattened presentation view + resolved evidence map.

        ``report.json`` keeps the raw :class:`VerifiedReport` for backwards
        compatibility; ``assessment.json`` is the self-contained contract the UI
        and external agents consume, with no on-the-fly evidence-ID joins.
        """
        from upgradelens.presentation.projector import project_assessment

        view = project_assessment(outcome, upgrade_plan=upgrade_plan, locale=locale)
        self._write_json("assessment.json", view.model_dump(mode="json"))

    def write_upgrade_plan(self, plan: Any) -> None:
        """S13: persist the modification plan as machine- and human-readable artifacts."""
        if plan is None:
            return
        self._write_json("upgrade-plan.json", plan.model_dump(mode="json"))
        md = render_plan_markdown(plan)
        if md:
            self._write_text("upgrade-plan.md", md)

    def write_run_md(
        self,
        *,
        intent: dict[str, Any],
        mode: str,
        verified: VerifiedReport | None,
        degradations: tuple[str, ...],
    ) -> None:
        self._write_text(
            "RUN.md", _render_run_md(self.run_id, intent, mode, verified, degradations)
        )

    # -- S1 integration: generic SoftwareTask artifacts --------------------- #

    def write_software_task(self, task: SoftwareTask) -> None:
        """Persist a :class:`SoftwareTask` (the generic task contract)."""
        self._write_json("task.json", task.model_dump(mode="json"))

    def read_software_task(self) -> SoftwareTask | None:
        path = self.run_dir / "task.json"
        if not path.exists():
            return None
        return SoftwareTask(**json.loads(path.read_text(encoding="utf-8")))

    def write_findings(self, findings: list[Finding]) -> None:
        self._write_json("findings.json", [f.model_dump(mode="json") for f in findings])

    def read_findings(self) -> list[Finding]:
        path = self.run_dir / "findings.json"
        if not path.exists():
            return []
        return [Finding(**d) for d in json.loads(path.read_text(encoding="utf-8"))]

    def write_actions(self, actions: list[ActionProposal]) -> None:
        self._write_json("actions.json", [a.model_dump(mode="json") for a in actions])

    def read_actions(self) -> list[ActionProposal]:
        path = self.run_dir / "actions.json"
        if not path.exists():
            return []
        return [ActionProposal(**d) for d in json.loads(path.read_text(encoding="utf-8"))]

    def write_verification(self, verification: VerificationResult) -> None:
        self._write_json("verification.json", verification.model_dump(mode="json"))

    def read_verification(self) -> VerificationResult | None:
        path = self.run_dir / "verification.json"
        if not path.exists():
            return None
        return VerificationResult(**json.loads(path.read_text(encoding="utf-8")))


def _render_run_md(
    run_id: str,
    intent: dict[str, Any],
    mode: str,
    verified: VerifiedReport | None,
    degradations: tuple[str, ...],
) -> str:
    """Build the human-readable run summary (``RUN.md``)."""
    kind = intent.get("kind", "unknown")
    lines: list[str] = [
        f"# UpgradeLens run {run_id}",
        "",
        f"- **mode**: `{mode}`",
        f"- **kind**: `{kind}`",
    ]

    if kind == "upgrade_task" and verified is not None:
        req = {
            "repo": intent.get("repo"),
            "dependency": intent.get("dependency"),
            "target_version": intent.get("target_version"),
            "source_version": intent.get("source_version"),
        }
        lines.append(f"- **repo**: `{req['repo']}`")
        lines.append(f"- **dependency**: `{req['dependency']}`")
        lines.append(f"- **target_version**: `{req['target_version']}`")
        lines.append(f"- **source_version**: `{req['source_version']}`")
        lines.append("")
        lines.append("## Outcome")
        lines.append("")
        lines.append(f"- conclusion: **{verified.conclusion.value}**")
        lines.append(f"- verified risks: **{len(verified.verified_risks)}**")
        lines.append(f"- degraded risks: **{len(verified.degraded_risks)}**")
        lines.append(f"- citation existence rate: **{verified.citation_existence_rate:.2f}**")
        lines.append(f"- partial: `{verified.partial}`  static: `{verified.static}`")
        if degradations:
            lines.append("")
            lines.append("### Degradations")
            for item in degradations:
                lines.append(f"- {item}")
        if verified.notes:
            lines.append("")
            lines.append(f"> {verified.notes}")
    elif kind == "need_clarification":
        lines.append("")
        lines.append("## Clarification needed")
        lines.append("")
        lines.append(intent.get("clarification", "") or "(no detail provided)")
        if intent.get("missing"):
            lines.append("")
            lines.append("Missing: " + ", ".join(intent["missing"]))
    elif kind == "not_upgrade":
        lines.append("")
        lines.append("## Not an upgrade task")
        lines.append("")
        lines.append(
            intent.get("clarification", "")
            or "The request was not recognised as a dependency upgrade task."
        )
    elif kind == "invalid_url":
        lines.append("")
        lines.append("## Invalid URL")
        lines.append("")
        lines.append(intent.get("clarification", "") or "The repository URL could not be parsed.")

    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    lines.append("- `plan.json` — the plan the run followed")
    lines.append("- `trace.jsonl` — one line per tool call (tool / params / status / latency)")
    lines.append("- `report.json` — the verified assessment (machine-readable)")
    lines.append("- `assessment.json` — the S12 presentation view (flattened evidence)")
    lines.append("- `report.md` — the verified assessment (human-readable)")
    lines.append("- `upgrade-plan.json` — the S13 modification plan (machine-readable)")
    lines.append("- `upgrade-plan.md` — the S13 中文修改说明 (before/after 对比)")
    lines.append("- `intent.json` — the routed Intent")
    lines.append("")
    return "\n".join(lines)


__all__ = ["RunStore", "redact_text", "DEFAULT_PLAN_STEPS"]
