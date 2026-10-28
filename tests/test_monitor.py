import importlib.util
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SCRIPT = Path(__file__).resolve().parents[1] / "monitor.py"
SPEC = importlib.util.spec_from_file_location("auto_resume_monitor", SCRIPT)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.now = 1_800_000_000
        self.session_id = "session_recent"
        self.task_db = root / "tasks.sqlite"
        self.session_db = root / "sessions.sqlite"
        with sqlite3.connect(self.task_db) as db:
            db.execute("""CREATE TABLE tasks (
                task_id TEXT, title TEXT, workspace_path TEXT, created_at INTEGER,
                updated_at INTEGER, task_status TEXT, cli_last_outcome TEXT,
                deleted INTEGER, archived INTEGER)""")
            db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)", (
                self.session_id, "昨天开始的任务", str(root),
                (self.now - 86400) * 1000, (self.now - 180) * 1000,
                "error", "error",
            ))
        with sqlite3.connect(self.session_db) as db:
            db.execute("CREATE TABLE session (id TEXT, last_outcome TEXT)")
            db.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_created INTEGER, sequence INTEGER)")
            db.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, data TEXT, time_updated INTEGER)")
            db.execute("INSERT INTO session VALUES (?, 'error')", (self.session_id,))
            db.execute("INSERT INTO message VALUES (?, ?, ?, ?, 1)", (
                "error-1", self.session_id,
                json.dumps({"error": {"data": {"code": "model_rate_limited"}}}),
                (self.now - 180) * 1000,
            ))
        self.patches = [
            mock.patch.object(monitor, "TASK_DB", self.task_db),
            mock.patch.object(monitor, "SESSION_DB", self.session_db),
            mock.patch.object(monitor, "STATE_FILE", root / "state.json"),
            mock.patch.object(monitor, "STATUS_FILE", root / "status.json"),
            mock.patch.object(monitor, "LOG_FILE", root / "monitor.log"),
            mock.patch.object(monitor.time, "time", return_value=self.now),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_old_task_with_recent_message_is_checked(self):
        sessions = monitor.scan_sessions(self.now)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["decision"], "ready")

    def test_daily_limit_blocks_send_and_appears_on_dashboard(self):
        monitor.STATE_FILE.write_text(json.dumps({
            "day": monitor.dt.datetime.fromtimestamp(self.now).astimezone().date().isoformat(),
            "sessions": {self.session_id: {"attempts": 100}},
        }))
        with mock.patch.object(monitor, "send_resume") as sender:
            monitor.run_once()
        sender.assert_not_called()
        status = json.loads(monitor.STATUS_FILE.read_text())
        self.assertEqual(status["attempts_today_total"], 100)
        self.assertEqual(status["sessions"][0]["decision"], "limit")

    def test_stale_message_is_not_resumed(self):
        with sqlite3.connect(self.session_db) as db:
            db.execute("UPDATE message SET time_created = ?", ((self.now - 3600) * 1000,))
        sessions = monitor.scan_sessions(self.now)
        self.assertEqual(sessions[0]["decision"], "skipped")

    def test_unfinished_assistant_message_overrides_stale_error_index(self):
        with sqlite3.connect(self.session_db) as db:
            db.execute("INSERT INTO message VALUES (?, ?, ?, ?, 2)", (
                "running-2", self.session_id,
                json.dumps({"role": "assistant", "time": {"created": (self.now - 5) * 1000}}),
                (self.now - 5) * 1000,
            ))
        sessions = monitor.scan_sessions(self.now)
        self.assertEqual(sessions[0]["task_status"], "running")
        self.assertEqual(sessions[0]["index_status"], "error")
        self.assertEqual(sessions[0]["decision"], "running")
        with mock.patch.object(monitor, "send_resume") as sender:
            monitor.run_once()
        sender.assert_not_called()

    def test_running_tool_overrides_completed_index(self):
        with sqlite3.connect(self.task_db) as db:
            db.execute("UPDATE tasks SET task_status = 'completed', cli_last_outcome = 'completed'")
        with sqlite3.connect(self.session_db) as db:
            db.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?)", (
                "tool-1", "error-1", self.session_id,
                json.dumps({"type": "tool", "state": {"status": "running"}}),
                (self.now - 10) * 1000,
            ))
        sessions = monitor.scan_sessions(self.now)
        self.assertEqual(sessions[0]["index_status"], "completed")
        self.assertEqual(sessions[0]["task_status"], "running")
        self.assertEqual(sessions[0]["decision"], "running")

    def test_recent_completed_index_is_not_called_finished_task(self):
        with sqlite3.connect(self.task_db) as db:
            db.execute("UPDATE tasks SET task_status = 'completed', cli_last_outcome = 'completed'")
        with sqlite3.connect(self.session_db) as db:
            db.execute("UPDATE message SET time_created = ?", ((self.now - 10) * 1000,))
        sessions = monitor.scan_sessions(self.now)
        self.assertEqual(sessions[0]["task_status"], "recent")
        self.assertEqual(sessions[0]["decision"], "recent")

    def test_completed_index_without_live_evidence_is_labeled_as_index(self):
        with sqlite3.connect(self.task_db) as db:
            db.execute("UPDATE tasks SET task_status = 'completed', cli_last_outcome = 'completed'")
        sessions = monitor.scan_sessions(self.now)
        self.assertEqual(sessions[0]["task_status"], "index_completed")
        self.assertEqual(sessions[0]["decision"], "index_completed")

    def test_delivery_requires_prompt_in_original_session(self):
        self.assertFalse(monitor.prompt_delivered(self.session_id, (self.now - 10) * 1000))
        with sqlite3.connect(self.session_db) as db:
            db.execute("INSERT INTO message VALUES (?, ?, ?, ?, 2)", (
                "user-2", self.session_id, json.dumps({"role": "user"}),
                self.now * 1000,
            ))
            db.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?)", (
                "text-2", "user-2", self.session_id,
                json.dumps({"type": "text", "text": monitor.PROMPT}),
                self.now * 1000,
            ))
        self.assertTrue(monitor.prompt_delivered(self.session_id, (self.now - 10) * 1000))

    def test_failed_desktop_send_does_not_mark_message_handled(self):
        with mock.patch.object(monitor, "send_resume", return_value=(False, "桌面发送失败")):
            monitor.run_once()
        state = json.loads(monitor.STATE_FILE.read_text())
        self.assertEqual(state["sessions"][self.session_id]["attempts"], 1)
        self.assertIsNone(state["sessions"][self.session_id]["message_id"])
        status = json.loads(monitor.STATUS_FILE.read_text())
        self.assertEqual(status["sessions"][0]["decision"], "failed")
        self.assertEqual(status["checks"][0]["resumed"], 0)

    def test_desktop_send_requires_unique_title(self):
        with sqlite3.connect(self.task_db) as db:
            db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)", (
                "another-session", "昨天开始的任务", self.tmp.name,
                self.now * 1000, self.now * 1000, "error", "error",
            ))
        with mock.patch.object(monitor.subprocess, "run") as runner:
            delivered, detail = monitor.send_resume(self.session_id, "昨天开始的任务", self.now)
        runner.assert_not_called()
        self.assertFalse(delivered)
        self.assertIn("标题不唯一", detail)

    def test_desktop_send_requires_database_confirmation(self):
        submitted = mock.Mock(returncode=0, stdout="submitted\n", stderr="")
        with mock.patch.object(monitor.subprocess, "run", return_value=submitted), \
             mock.patch.object(monitor, "prompt_delivered", return_value=True) as confirmed:
            delivered, detail = monitor.send_resume(self.session_id, "昨天开始的任务", self.now)
        self.assertTrue(delivered)
        self.assertIn("原会话", detail)
        confirmed.assert_called_once_with(self.session_id, self.now * 1000, monitor.PROMPT)

    def test_desktop_send_reports_ui_error(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="composer contains an unsent draft\n")
        with mock.patch.object(monitor.subprocess, "run", return_value=failed):
            delivered, detail = monitor.send_resume(self.session_id, "昨天开始的任务", self.now)
        self.assertFalse(delivered)
        self.assertIn("unsent draft", detail)

    def test_send_failure_stops_monitoring(self):
        with mock.patch.object(monitor, "send_resume", return_value=(
            False, "桌面已尝试提交，但原会话 30 秒内未出现指令"
        )):
            blocking_error = monitor.run_once()
        self.assertIn("未出现指令", blocking_error)
        status = json.loads(monitor.STATUS_FILE.read_text())
        self.assertEqual(status["error"], blocking_error)
        self.assertEqual(status["sessions"][0]["decision"], "failed")

    def test_dashboard_control_starts_and_stops_explicitly(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), monitor.DashboardHandler)
        server.monitor_control = threading.Condition()
        server.monitoring = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, method="GET", origin=None):
            headers = {"X-ZCode-Control": "1"} if method == "POST" else {}
            if origin:
                headers["Origin"] = origin
            with urlopen(Request(base + path, headers=headers, method=method)) as response:
                return json.load(response)

        self.assertFalse(request("/api/status")["monitoring"])
        with self.assertRaises(HTTPError) as rejected:
            request("/api/start", "POST", "http://example.com")
        self.assertEqual(rejected.exception.code, 403)
        self.assertFalse(request("/api/status")["monitoring"])
        self.assertTrue(request("/api/start", "POST", base)["monitoring"])
        self.assertTrue(request("/api/status")["monitoring"])
        self.assertFalse(request("/api/stop", "POST", base)["monitoring"])
        self.assertFalse(request("/api/status")["monitoring"])


if __name__ == "__main__":
    unittest.main()
