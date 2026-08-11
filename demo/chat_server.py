"""UpgradeLens Chat Server — 零额外依赖（仅 stdlib + upgradelens）。

提供：
- GET /           → chat.html
- POST /api/run   → JSON 结果

启动：
    uv run python demo/chat_server.py

然后打开 http://127.0.0.1:8503
"""

from __future__ import annotations

import json
import sys
import traceback
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DEMO_DIR = Path(__file__).resolve().parent
PORT = 8503


class ChatHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "":
            self.path = "/chat.html"
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/run":
            self._handle_run()
        else:
            self.send_error(404)

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

        try:
            from upgradelens import DependencyUpgradeAgent

            agent = DependencyUpgradeAgent(mode=mode)
            result = agent.run(
                goal,
                repo=repo,
                dependency=dependency,
                target_version=target_version,
                source_version=source_version,
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
        # quieter logs
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write(f"[chat] {args[0]}\n")


def main():
    server = HTTPServer(("127.0.0.1", PORT), ChatHandler)
    print(f"UpgradeLens Chat UI: http://127.0.0.1:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
