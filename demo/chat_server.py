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
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from upgradelens.presentation.projector import project_assessment

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
# HTTP handler
# --------------------------------------------------------------------------- #
class ChatHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""):
            self.path = "/index.html"
        elif path == "/api/project":
            self._handle_project()
            return
        elif path == "/api/comparison":
            self._handle_comparison()
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/run":
            self._handle_run()
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
                    result.outcome.verified.model_dump(mode="json")
                    if result.outcome
                    else None
                ),
                "degradations": list(result.degradations),
                "trace": (
                    [e.to_dict() for e in result.trace.events] if result.trace else []
                ),
                "cost": {
                    "total_tokens": (
                        sum(
                            r.prompt_tokens + r.completion_tokens
                            for r in result.gateway.ledger
                        )
                        if result.gateway
                        else 0
                    ),
                    "call_count": (
                        len(result.gateway.ledger) if result.gateway else 0
                    ),
                },
                "error": result.error,
                "assessment": (
                    project_assessment(result.outcome).model_dump(mode="json")
                    if result.outcome is not None
                    else None
                ),
            }
            self._json_response(response)

        except Exception as exc:
            traceback.print_exc()
            self._json_response({"error": str(exc)}, 500)

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
    server = HTTPServer(("127.0.0.1", PORT), ChatHandler)
    _ensure_comparison_started()  # 后台预热 S8 对照
    print(f"UpgradeLens Demo: http://127.0.0.1:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
