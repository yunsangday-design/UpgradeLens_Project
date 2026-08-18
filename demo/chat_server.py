"""UpgradeLens Demo Server — 零额外依赖（仅 stdlib + upgradelens）。

提供：
- GET /            → index.html（双页：使用 / 项目，tab 切换）
- GET /chat.html   → 对话式使用页（也可被 index 的「使用」tab 内嵌）
- POST /api/run     → 运行 DependencyUpgradeAgent，返回可视化结果
- GET  /api/project  → 项目元数据（指标 / 阶段 / 用法）
- GET  /api/comparison → S8 三架构对照（后台计算一次后缓存）

启动：
    uv run python demo/chat_server.py

然后打开 http://127.0.0.1:8503
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 `demo` 包可被导入

from demo.jobs import Job, JobManager
from upgradelens.core.task import SoftwareTask, TaskContext, TaskKind
from upgradelens.presentation.projector import project_assessment


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a new thread (needed for SSE long-polling)."""

    daemon_threads = True


ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = Path(__file__).resolve().parent
EVAL_CASES_DIR = ROOT / "tests" / "fixtures" / "eval"
PORT = 8503

# 缺 repo 时，按依赖名自动选用对应 eval fixture 仓库，让 demo “发 query 即出结果”。
_DEFAULT_REPO_BY_DEP = {
    "pydantic": "tests/fixtures/eval/pydantic_field_validator/repo",
    "sqlalchemy": "tests/fixtures/eval/sqlalchemy_engine_execute/repo",
    "fastapi": "tests/fixtures/eval/fastapi_depends/repo",
}


def _infer_dep(goal: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit.lower()
    low = goal.lower()
    for d in ("pydantic", "sqlalchemy", "fastapi"):
        if d in low:
            return d
    return None


# --------------------------------------------------------------------------- #
# 项目元数据（静态事实，供「项目」页渲染）
# --------------------------------------------------------------------------- #
PROJECT_INFO = {
    "name": "UpgradeLens",
    "version": "0.2.0",
    "tagline": "证据驱动、能根据缺口与反馈自我调整的依赖升级 Agent",
    "summary": (
        "UpgradeLens 把一次依赖升级评估拆成「意图识别 → 证据收集 → 计划驱动执行 "
        "→ 验证 → 降级说明」的闭环。它不依赖每个依赖的专用知识包：所有依赖默认走 "
        "代码扫描 → 共享语料 RAG → LLM → 验证，并由 AgentPlan 真正驱动每一步、"
        "由 Verifier 反馈触发重新规划。"
    ),
    "principles": [
        "共享 RAG 主路径：所有依赖走同一套检索与验证，不为单依赖写死事实。",
        "Plan 驱动：AgentPlan 的每一步都是真实工具调用，而非文本装饰。",
        "证据驱动：结论必须能追溯到代码符号 / 文档 / 检索结果。",
        "Verifier 反馈闭环：编造的引用会被检出，缺口会触发自主补检索。",
        "可降级：证据不足时诚实说明，而不是编造。",
    ],
    "metrics": {
        "tests_passed": 527,
        "eval_cases": 18,
        "cli_commands": 12,
        "modes": ["fake", "replay", "live"],
        "latest_commit": "4998bd4",
    },
    "stages": [
        {"id": "S0", "title": "固化共享 RAG 主路径与 Agent 范式基线", "status": "done"},
        {"id": "S1", "title": "统一升级任务与版本契约", "status": "done"},
        {"id": "S2", "title": "收敛 Pipeline / ReAct 两套证据收集路径", "status": "done"},
        {"id": "S3", "title": "让 AgentPlan 真正驱动执行", "status": "done"},
        {"id": "S4", "title": "建立证据覆盖判断与自主补检索", "status": "done"},
        {"id": "S5", "title": "让 Verifier 反馈触发重新规划", "status": "done"},
        {"id": "S6", "title": "产品化共享语料与执行计划", "status": "done"},
        {"id": "S7", "title": "UpgradePlan 导出与受控执行", "status": "done"},
        {"id": "S8", "title": "建立 Agent 对照评测与 CI 门禁", "status": "done"},
        {"id": "S9", "title": "演示与项目包装（统一 API）", "status": "done"},
        {"id": "S10", "title": "对话式可视化前端", "status": "done"},
        {"id": "S11", "title": "统一执行与产物落盘（run_store）", "status": "done"},
        {"id": "S12", "title": "统一升级评估展示契约（presentation）", "status": "done"},
        {"id": "S13", "title": "让修改建议与 UpgradePlan 成为默认产出物", "status": "done"},
        {"id": "S14", "title": "统一升级评估结果中文国际化", "status": "done"},
        {"id": "S15", "title": "重构对话页结果可视化", "status": "in_progress"},
    ],
    "systems": {
        "direct_llm": "裸 LLM / coding agent：直接信任模型输出，无检索、无验证。",
        "fixed_pipeline": "确定性 collect → analyse → verify 流水线：同一份证据经共享检索 + 验证。",
        "agent": "UpgradeLens Agent 循环：plan 驱动、按缺口补检索、按验证反馈重规划。",
    },
    "usage": {
        "cli": [
            "uv run upgradelens scan-dependency --repo <path> --dependency pydantic",
            "uv run upgradelens assess --repo <path> --dependency pydantic --target 2.7",
            "uv run upgradelens agent --goal 'upgrade pydantic to 2.7' --mode fake",
            "uv run upgradelens eval-compare --mode fake",
            "uv run upgradelens eval-ablate --mode fake",
        ],
        "python": (
            "from upgradelens import DependencyUpgradeAgent\n\n"
            "result = DependencyUpgradeAgent(mode='fake').run(\n"
            "    'upgrade pydantic from 1.x to 2.7',\n"
            "    dependency='pydantic',\n"
            "    target_version='2.7',\n"
            ")\n"
            "print(result.outcome.verified.conclusion)"
        ),
    },
    "limits": [
        "评测结论目前基于 FAKE 确定性数据；live 真实模型对照待补。",
        "尚未实现多语言、传递依赖链交叉升级、与生产 CI 的自动 PR 集成。",
        "WebView 内不支持 Streamlit（WebSocket），对话页用零依赖 HTTP server 实现。",
    ],
}


# --------------------------------------------------------------------------- #
# Job Manager (E1: async task execution)
# --------------------------------------------------------------------------- #
_JOB_MANAGER = JobManager(max_workers=2)


# --------------------------------------------------------------------------- #
# S8 对照（后台计算一次，缓存结果）
# --------------------------------------------------------------------------- #
_COMPARISON = {"data": None, "ready": False, "lock": threading.Lock()}


def _compute_comparison() -> None:
    with _COMPARISON["lock"]:
        if _COMPARISON["data"] is None:
            from upgradelens.eval.comparison import run_comparison_from_dir

            report = run_comparison_from_dir(EVAL_CASES_DIR)
            _COMPARISON["data"] = report.to_json()
        _COMPARISON["ready"] = True


def _ensure_comparison_started() -> None:
    if _COMPARISON["ready"] or _COMPARISON["data"] is not None:
        return
    threading.Thread(target=_compute_comparison, daemon=True).start()


# --------------------------------------------------------------------------- #
# Result badges (S15)
# --------------------------------------------------------------------------- #
def _build_badges(result: Any) -> list[dict]:
    """Derive the RAG / data-source badges shown in the result header (S15).

    The badges make the provenance of the answer explicit without leaking
    engine internals:

    - ``kb-hit`` — at least one risk is backed by a resolved document / RAG chunk
      from the local knowledge base.
    - ``online-supplement`` — some evidence was pulled live as a temporary
      supplement (populated once S16/S17 online fallback lands).
    - ``kb-writing`` — local KB was used but part of the answer is still being
      written back (partial).
    - ``kb-failed`` — doc/coverage retrieval failed, so the result is incomplete.
    - ``mode-static`` / ``mode-model`` — offline deterministic vs model-assisted.

    Only badges the data actually supports are emitted; the UI renders whatever
    is present, so S16/S17 can extend the data without touching the frontend.
    """
    from upgradelens.presentation.models import UpgradeAssessmentView

    badges: list[dict] = []
    assessment = result.assessment
    if assessment is None:
        return badges
    asmt = assessment
    if not isinstance(asmt, UpgradeAssessmentView):
        try:
            asmt = UpgradeAssessmentView.model_validate(assessment)
        except Exception:
            return badges

    if asmt.static:
        badges.append({"kind": "mode-static", "text": "离线模拟输出"})
    else:
        badges.append({"kind": "mode-model", "text": "模型辅助"})

    degradations = list(result.degradations)
    doc_failure = any(
        d in degradations for d in ("NO_DOC_INDEX", "COVERAGE_INSUFFICIENT", "NO_CODE_EVIDENCE")
    )

    # Online supplement (S16) is signalled by a trace event rather than a
    # degradation, because it is a success path, not a degradation.
    trace = getattr(result, "trace", None)
    event_tools: list[str] = []
    if trace is not None:
        raw = getattr(trace, "events", None)
        if raw is None and isinstance(trace, list):
            raw = trace
        for e in raw or []:
            tool = getattr(e, "tool", None)
            if tool is None and isinstance(e, dict):
                tool = e.get("tool")
            if tool:
                event_tools.append(tool)
    online_ok = "online_supplement" in event_tools

    # The local knowledge base is the grounding source in both offline (fake)
    # and live mode unless retrieval explicitly failed. Online supplement wins
    # over a local miss (it recovered the answer); a bare local miss is a failure.
    if online_ok:
        badges.append({"kind": "online-supplement", "text": "在线资料临时补充"})
    elif doc_failure:
        badges.append({"kind": "kb-failed", "text": "资料补充失败，结果不完整"})
    else:
        badges.append({"kind": "kb-hit", "text": "本地知识库命中"})
        if asmt.is_partial:
            badges.append({"kind": "kb-writing", "text": "资料正在写入知识库"})

    return badges


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class ChatHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            self.path = "/index.html"
        elif path == "/workbench":
            self.path = "/workbench.html"
        elif path == "/api/project":
            self._handle_project()
            return
        elif path == "/api/comparison":
            self._handle_comparison()
            return
        elif path.startswith("/api/jobs/") and path.endswith("/events"):
            self._handle_job_events(path)
            return
        elif path.startswith("/api/jobs/"):
            self._handle_job_status(path)
            return
        elif path.startswith("/static/"):
            # Serve from demo/static/
            pass
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/run":
            self._handle_run()
        elif path == "/api/capability/run":
            self._handle_capability_run()
        elif path == "/api/task/run":
            self._handle_task_run()
        elif path == "/api/run-async":
            self._handle_run_async()
        elif path == "/api/scan":
            self._handle_scan()
        elif path == "/api/scan-async":
            self._handle_scan_async()
        else:
            self.send_error(404)

    # -- endpoints --------------------------------------------------------- #
    def _handle_project(self):
        self._json_response(PROJECT_INFO)

    def _handle_comparison(self):
        _ensure_comparison_started()
        if _COMPARISON["ready"] and _COMPARISON["data"] is not None:
            self._json_response(_COMPARISON["data"])
        else:
            self._json_response({"status": "computing"})

    def _handle_run(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as exc:
            self._json_response({"error": f"bad request: {exc}"}, 400)
            return

        goal = body.get("goal", "")
        if not goal:
            self._json_response({"error": "goal is required"}, 400)
            return

        repo = body.get("repo") or None
        dependency = body.get("dependency") or None
        target_version = body.get("target_version") or None
        source_version = body.get("source_version") or None
        mode = body.get("mode", "fake")

        if not repo:
            inferred = _infer_dep(goal, dependency)
            repo = _DEFAULT_REPO_BY_DEP.get(inferred) if inferred else None

        try:
            from upgradelens import DependencyUpgradeAgent
            from upgradelens.db.database import DEFAULT_DB_PATH

            agent = DependencyUpgradeAgent(mode=mode)
            result = agent.run(
                goal,
                repo=repo,
                dependency=dependency,
                target_version=target_version,
                source_version=source_version,
                db=str(DEFAULT_DB_PATH),
            )

            response = {
                "intent": result.intent.model_dump(mode="json"),
                "plan": result.plan.to_dict() if result.plan else None,
                "verified": (
                    result.outcome.verified.model_dump(mode="json") if result.outcome else None
                ),
                "degradations": list(result.degradations),
                "trace": ([e.to_dict() for e in result.trace.events] if result.trace else []),
                "cost": {
                    "total_tokens": (
                        sum(r.prompt_tokens + r.completion_tokens for r in result.gateway.ledger)
                        if result.gateway
                        else 0
                    ),
                    "call_count": (len(result.gateway.ledger) if result.gateway else 0),
                },
                "error": result.error,
                "badges": _build_badges(result),
                "assessment": (
                    result.assessment.model_dump(mode="json")
                    if result.assessment is not None
                    else (
                        project_assessment(result.outcome).model_dump(mode="json")
                        if result.outcome is not None
                        else None
                    )
                ),
                "upgrade_plan": (
                    result.upgrade_plan.model_dump(mode="json")
                    if result.upgrade_plan is not None
                    else None
                ),
            }
            self._json_response(response)

        except Exception as exc:
            traceback.print_exc()
            self._json_response({"error": str(exc)}, 500)

    def _handle_capability_run(self):
        """POST /api/capability/run — run any capability, return a normalized result.

        The body selects a capability ``kind`` and supplies the inputs it needs
        (``diff`` for pr/security/breaking, ``issue_text`` for issue repair,
        ``dependency``/``source_version``/``target_version`` for upgrades). With no
        ``repo`` the server falls back to a built-in eval fixture so a preset can be
        replayed offline.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as exc:
            self._json_response({"error": f"bad request: {exc}"}, 400)
            return
        try:
            from upgradelens.capabilities.workbench import run_capability

            kind = str(body.get("kind") or "dependency_upgrade")
            repo = str(body.get("repo") or "").strip()
            if not repo:
                if kind == "dependency_upgrade":
                    dep = body.get("dependency") or _infer_dep(str(body.get("goal", "")), None)
                    default = _DEFAULT_REPO_BY_DEP.get(
                        dep, "tests/fixtures/eval/pydantic_field_validator/repo"
                    )
                else:
                    default = "tests/fixtures/eval/pydantic_field_validator/repo"
                repo = str((ROOT / default).resolve().as_posix())
            mode = str(body.get("mode") or "fake")
            context = TaskContext(
                repo=repo,
                dependency=str(body.get("dependency") or ""),
                source_version=str(body.get("source_version") or ""),
                target_version=str(body.get("target_version") or ""),
                unified_diff=str(body.get("diff") or ""),
                issue_text=str(body.get("issue_text") or ""),
                from_version=str(body.get("from_version") or ""),
                to_version=str(body.get("to_version") or ""),
            )
            task = SoftwareTask(
                task_id="workbench",
                kind=TaskKind(kind),
                goal=str(body.get("goal") or ""),
                context=context,
            )
            result = run_capability(task, mode=mode)
            self._json_response(result.model_dump(mode="json"))
        except Exception as exc:
            traceback.print_exc()
            self._json_response({"error": str(exc)}, 500)

    def _handle_task_run(self):
        """POST /api/task/run — natural-language entry: triage text, then dispatch.

        This is the M1a end-to-end bridge: a single free-text message is routed to the
        correct capability via :func:`route_task` and executed through the unified
        :func:`run_capability` dispatcher. Optional ``diff`` / ``issue_text`` /
        ``from_version`` / ``to_version`` inputs can be supplied in the body for PR /
        security / issue / breaking-change capabilities (which need more than NL can carry).
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as exc:
            self._json_response({"error": f"bad request: {exc}"}, 400)
            return
        try:
            from upgradelens.agent.router import route_task
            from upgradelens.agent.supervisor import AgentContext, run_supervisor

            text = str(body.get("text") or "").strip()
            if not text:
                self._json_response({"error": "text is required"}, 400)
                return

            mode = str(body.get("mode") or "fake")
            task = route_task(text)
            ctx = task.context

            # Resolve a usable repo (mirror /api/capability/run fallback).
            repo = ctx.repo
            if not repo:
                default = "tests/fixtures/eval/pydantic_field_validator/repo"
                repo = str((ROOT / default).resolve().as_posix())

            # Merge capability-specific inputs that natural language cannot provide.
            # ``TaskContext`` is frozen, so rebuild it (do not mutate in place).
            from upgradelens.core.task import SoftwareTask, TaskContext

            merged = TaskContext(
                repo=repo,
                dependency=ctx.dependency,
                source_version=ctx.source_version,
                target_version=ctx.target_version,
                unified_diff=str(body.get("diff") or ""),
                issue_text=str(body.get("issue_text") or ""),
                from_version=str(body.get("from_version") or ""),
                to_version=str(body.get("to_version") or ""),
            )
            task = SoftwareTask(
                task_id=task.task_id,
                kind=task.kind,
                goal=text,
                context=merged,
            )

            # Route through the controlled Supervisor + Handoff layer (M3). A
            # single-capability request short-circuits to dispatch_by_task; a
            # multi-capability request fans out to isolated sub-agents.
            agent_ctx = AgentContext(mode=mode)
            sup = run_supervisor(task, agent_ctx, mode=mode)
            self._json_response(
                {
                    "kind": task.kind.value,
                    "goal": text,
                    "orchestration": sup.orchestration,
                    "capability_kinds": sup.capability_kinds,
                    "result": sup.result.model_dump(mode="json") if sup.result else None,
                    "sub_results": [r.model_dump(mode="json") for r in sup.sub_results],
                    "summary": sup.summary,
                    "verification_passed": sup.verification_passed,
                    "degradations": sup.degradations,
                    "budget_tokens_used": sup.budget_tokens_used,
                    "budget_tokens_limit": sup.budget_tokens_limit,
                    # unified-runtime observability (MA-2-2 / MA-2-3)
                    "root_run_id": sup.root_run_id,
                    "execution_plan": sup.execution_plan,
                    "agent_runs": sup.agent_runs,
                    "aggregate_result": sup.aggregate_result,
                    "conflicts": sup.conflicts,
                }
            )
        except Exception as exc:
            traceback.print_exc()
            self._json_response({"error": str(exc)}, 500)
        """POST /api/scan — scan MVP manifests for upgradable dependencies."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as exc:
            self._json_response({"error": f"bad request: {exc}"}, 400)
            return

        repo = body.get("repo", "")
        if not repo:
            self._json_response({"error": "repo is required"}, 400)
            return

        repo_path = Path(repo).resolve()
        if not repo_path.is_dir():
            self._json_response({"error": f"repo is not a directory: {repo}"}, 400)
            return

        # Security: only allow paths under the project root or common workspace
        try:
            repo_path.relative_to(ROOT.parent)
        except ValueError:
            self._json_response({"error": "repo path must be within the workspace"}, 403)
            return

        try:
            from upgradelens.analyzers.upgradable_scan import (
                scan_upgradable_dependencies,
            )
            from upgradelens.tools.fetcher import RestrictedFetcher
            from upgradelens.tools.pypi import PyPIClient

            fetcher = RestrictedFetcher()
            pypi = PyPIClient(fetcher)
            result = scan_upgradable_dependencies(repo_path, pypi)
            self._json_response(result.model_dump(mode="json"))
        except Exception as exc:
            traceback.print_exc()
            self._json_response({"error": str(exc)}, 500)

    def _handle_scan_async(self):
        """POST /api/scan-async → 202 {job_id} for async scan."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as exc:
            self._json_response({"error": f"bad request: {exc}"}, 400)
            return

        repo = body.get("repo", "")
        if not repo:
            self._json_response({"error": "repo is required"}, 400)
            return

        repo_path = Path(repo).resolve()
        if not repo_path.is_dir():
            self._json_response({"error": f"repo is not a directory: {repo}"}, 400)
            return

        try:
            repo_path.relative_to(ROOT.parent)
        except ValueError:
            self._json_response({"error": "repo path must be within the workspace"}, 403)
            return

        job = self._submit_scan_job(repo_path)
        self._json_response({"job_id": job.job_id}, 202)

    def _handle_run_async(self):
        """POST /api/run-async → 202 {job_id} for async assessment."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as exc:
            self._json_response({"error": f"bad request: {exc}"}, 400)
            return

        goal = body.get("goal", "")
        if not goal:
            self._json_response({"error": "goal is required"}, 400)
            return

        repo = body.get("repo") or None
        dependency = body.get("dependency") or None
        target_version = body.get("target_version") or None
        source_version = body.get("source_version") or None
        mode = body.get("mode", "fake")

        if not repo:
            inferred = _infer_dep(goal, dependency)
            repo = _DEFAULT_REPO_BY_DEP.get(inferred) if inferred else None

        job = self._submit_run_job(goal, mode, repo, dependency, target_version, source_version)
        self._json_response({"job_id": job.job_id}, 202)

    def _handle_job_status(self, path: str):
        """GET /api/jobs/{job_id} → job snapshot."""
        job_id = path.removeprefix("/api/jobs/").strip("/")
        job = _JOB_MANAGER.get(job_id)
        if job is None:
            self._json_response({"error": "job not found"}, 404)
            return
        self._json_response(job.snapshot())

    def _handle_job_events(self, path: str):
        """GET /api/jobs/{job_id}/events → SSE stream."""
        job_id = path.removeprefix("/api/jobs/").removesuffix("/events").strip("/")
        job = _JOB_MANAGER.get(job_id)
        if job is None:
            self._json_response({"error": "job not found"}, 404)
            return

        # Parse Last-Event-ID for reconnection
        last_id = 0
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id:
            try:
                last_id = int(last_event_id)
            except ValueError:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            # Send any buffered events first
            for event in job.events_since(last_id):
                self.wfile.write(event.to_sse().encode("utf-8"))
                self.wfile.flush()
                last_id = event.id

            # Stream new events until job is terminal
            while not job.is_terminal:
                with job._condition:
                    job._condition.wait(timeout=15.0)
                new_events = job.events_since(last_id)
                if new_events:
                    for event in new_events:
                        self.wfile.write(event.to_sse().encode("utf-8"))
                        self.wfile.flush()
                        last_id = event.id
                else:
                    # Heartbeat to keep connection alive
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()

            # Send any remaining events after terminal
            for event in job.events_since(last_id):
                self.wfile.write(event.to_sse().encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected

    # -- async job submission helpers --------------------------------------- #

    def _submit_scan_job(self, repo_path: Path) -> Job:
        """Submit a scan as an async job, return the Job."""

        def _do_scan(job: Job) -> dict:
            from upgradelens.analyzers.upgradable_scan import (
                scan_upgradable_dependencies,
            )
            from upgradelens.tools.fetcher import RestrictedFetcher
            from upgradelens.tools.pypi import PyPIClient

            job.emit("step_started", {"step": "scan", "repo": str(repo_path)})
            fetcher = RestrictedFetcher()
            pypi = PyPIClient(fetcher)
            result = scan_upgradable_dependencies(repo_path, pypi)
            job.emit("step_finished", {"step": "scan", "items": len(result.items)})
            return result.model_dump(mode="json")

        return _JOB_MANAGER.submit("scan", {"repo": str(repo_path)}, _do_scan)

    def _submit_run_job(
        self,
        goal: str,
        mode: str,
        repo: str | None,
        dependency: str | None,
        target_version: str | None,
        source_version: str | None,
    ) -> Job:
        """Submit an assessment as an async job, return the Job."""
        params = {
            "goal": goal,
            "mode": mode,
            "repo": repo,
            "dependency": dependency,
            "target_version": target_version,
        }

        def _do_run(job: Job) -> dict:
            from upgradelens import DependencyUpgradeAgent
            from upgradelens.agent.plan import AgentPlan
            from upgradelens.db.database import DEFAULT_DB_PATH

            job.emit("step_started", {"step": "agent_init", "mode": mode})

            # Build a plan_writer that emits SSE events on every plan change
            def _plan_event_writer(plan: AgentPlan) -> None:
                """Emit a plan_updated event with step summaries."""
                steps_summary = []
                for s in plan.steps:
                    steps_summary.append(
                        {
                            "id": s.id,
                            "tool": s.tool,
                            "seq": s.seq,
                            "status": s.status,
                            "reason": s.reason or "",
                            "observation": (s.observation or "")[:120],
                        }
                    )
                job.emit("plan.updated", {"steps": steps_summary, "status": plan.status})
                # Also emit step-level events for running/completed transitions
                for s in plan.steps:
                    if s.status == "running":
                        job.emit(
                            "step_started",
                            {
                                "step": s.tool,
                                "plan_step_id": s.id,
                                "reason": s.reason or "",
                            },
                        )

            job.emit("step_started", {"step": "agent_run", "dependency": dependency or ""})
            agent = DependencyUpgradeAgent(mode=mode)
            result = agent.run(
                goal,
                repo=repo,
                dependency=dependency,
                target_version=target_version,
                source_version=source_version,
                db=str(DEFAULT_DB_PATH),
                plan_writer=_plan_event_writer,
            )

            job.emit("step_started", {"step": "projecting"})
            response = {
                "intent": result.intent.model_dump(mode="json"),
                "plan": result.plan.to_dict() if result.plan else None,
                "verified": (
                    result.outcome.verified.model_dump(mode="json") if result.outcome else None
                ),
                "degradations": list(result.degradations),
                "trace": ([e.to_dict() for e in result.trace.events] if result.trace else []),
                "cost": {
                    "total_tokens": (
                        sum(r.prompt_tokens + r.completion_tokens for r in result.gateway.ledger)
                        if result.gateway
                        else 0
                    ),
                    "call_count": (len(result.gateway.ledger) if result.gateway else 0),
                },
                "error": result.error,
                "badges": _build_badges(result),
                "assessment": (
                    result.assessment.model_dump(mode="json")
                    if result.assessment is not None
                    else (
                        project_assessment(result.outcome).model_dump(mode="json")
                        if result.outcome is not None
                        else None
                    )
                ),
                "upgrade_plan": (
                    result.upgrade_plan.model_dump(mode="json")
                    if result.upgrade_plan is not None
                    else None
                ),
            }
            job.emit("step_finished", {"step": "complete"})
            return response

        return _JOB_MANAGER.submit("run", params, _do_run)

    def _json_response(self, data: dict, status: int = 200):
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write(f"[chat] {args[0]}\n")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ChatHandler)
    _ensure_comparison_started()  # 后台预热 S8 对照
    print(f"UpgradeLens Demo: http://127.0.0.1:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
        _JOB_MANAGER.stop(timeout=10.0)
        server.shutdown()
        print("Stopped.")


if __name__ == "__main__":
    main()
