"""Smoke + contract test for the conversational chat server (demo/chat_server.py).

Starts the zero-dependency stdlib HTTP server on an ephemeral port and asserts
the ``/api/run`` endpoint drives ``DependencyUpgradeAgent`` in fake mode and
returns the S15 presentation DTO (intent / plan / assessment / upgrade_plan /
badges / trace / cost). Also asserts the static ``chat.html`` exposes the
result-visualization sections the frontend renders into.
"""
from __future__ import annotations

import http.client
import http.server
import json
import threading
import time
from pathlib import Path
from unittest import TestCase

from demo.chat_server import ChatHandler

_DEMO_HTML = Path(__file__).resolve().parents[2] / "demo" / "chat.html"


class _ChatServer:
    def __init__(self) -> None:
        self.server = http.server.HTTPServer(("127.0.0.1", 0), ChatHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()
        time.sleep(0.05)  # let the loop begin accepting

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestChatServerSmoke(TestCase):
    def setUp(self) -> None:
        self.srv = _ChatServer()
        self.srv.start()

    def tearDown(self) -> None:
        self.srv.stop()

    def _post(self, payload: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=60)
        body = json.dumps(payload).encode("utf-8")
        conn.request(
            "POST",
            "/api/run",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def _get(self, path: str) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=30)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        return resp.status, body

    def test_index_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("upgradelens", body.lower())

    def test_project_endpoint(self):
        status, body = self._get("/api/project")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["name"], "UpgradeLens")
        self.assertIn("agent", data["systems"])
        # S15 should be the in-progress head of the roadmap.
        self.assertEqual(data["stages"][-1]["id"], "S15")
        self.assertEqual(data["stages"][-1]["status"], "in_progress")
        self.assertEqual(data["stages"][-2]["id"], "S14")
        self.assertEqual(data["stages"][-2]["status"], "done")

    def test_run_returns_presentation_dto(self):
        status, data = self._post(
            {"goal": "upgrade pydantic from 1.x to 2.7", "mode": "fake"}
        )
        self.assertEqual(status, 200)
        for key in ("intent", "plan", "verified", "assessment", "upgrade_plan",
                    "badges", "degradations", "trace", "cost"):
            self.assertIn(key, data)
        # S15 info hierarchy is backed by separated verified / unconfirmed risks.
        self.assertEqual(data["intent"]["kind"], "upgrade_task")
        self.assertGreater(len(data["plan"]["steps"]), 0)
        self.assertIsInstance(data["assessment"]["verified_risks"], list)
        self.assertIsInstance(data["assessment"]["degraded_risks"], list)
        self.assertIsInstance(data["upgrade_plan"]["steps"], list)
        # trace is serialized as a list of event dicts (consumed by the UI).
        self.assertIsInstance(data["trace"], list)
        self.assertGreater(len(data["trace"]), 0)
        self.assertIn("tool", data["trace"][0])
        self.assertIsInstance(data["cost"], dict)
        self.assertIn("total_tokens", data["cost"])
        # badges must be present and carry at least the mode + KB provenance.
        self.assertIsInstance(data["badges"], list)
        self.assertGreater(len(data["badges"]), 0)
        badge_kinds = {b["kind"] for b in data["badges"]}
        self.assertTrue(badge_kinds & {"mode-static", "mode-model"})
        # fake mode resolves docs → local KB hit badge expected.
        self.assertIn("kb-hit", badge_kinds)

    def test_missing_goal_returns_400(self):
        status, data = self._post({"mode": "fake"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_html_exposes_result_sections(self):
        """The static page must render the S15 sections the API contract feeds."""
        self.assertTrue(_DEMO_HTML.exists(), "demo/chat.html missing")
        html = _DEMO_HTML.read_text(encoding="utf-8")
        for marker in (
            "已验证问题",   # verified-findings zone
            "待确认问题",   # unconfirmed (yellow) zone
            "升级修改计划",  # upgrade plan
            "分析过程",     # folded analysis process
            "renderBadges", # RAG / source badges
            "renderFinding",  # finding cards
            "verified_risks",  # reads the separated risk list
            "degraded_risks",
        ):
            self.assertIn(marker, html, f"chat.html missing section marker: {marker}")
        # The old CSS typo must be gone.
        self.assertNotIn("0.8rem1rem", html)
