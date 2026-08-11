"""Smoke test for the conversational chat server (demo/chat_server.py).

Starts the zero-dependency stdlib HTTP server on an ephemeral port and asserts
the ``/api/run`` endpoint drives ``DependencyUpgradeAgent`` in fake mode and
returns a serializable result (intent / plan / verified / trace / cost).
"""
from __future__ import annotations

import http.client
import http.server
import json
import threading
import time
from unittest import TestCase

from demo.chat_server import ChatHandler


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

    def test_run_returns_serialized_result(self):
        status, data = self._post(
            {"goal": "upgrade pydantic from 1.x to 2.7", "mode": "fake"}
        )
        self.assertEqual(status, 200)
        for key in ("intent", "plan", "verified", "degradations", "trace", "cost"):
            self.assertIn(key, data)
        self.assertIn(data["intent"]["kind"], {"upgrade_task", "need_clarification"})
        self.assertIsInstance(data["trace"], list)
        self.assertIsInstance(data["cost"], dict)
        self.assertIn("total_tokens", data["cost"])
        self.assertIn("call_count", data["cost"])

    def test_missing_goal_returns_400(self):
        status, data = self._post({"mode": "fake"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)
