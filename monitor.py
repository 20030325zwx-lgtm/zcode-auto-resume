#!/usr/bin/env python3
"""Watch recently active ZCode sessions for retriable errors."""

import argparse
import datetime as dt
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time


HOME = Path.home()
TASK_DB = HOME / ".zcode/v2/tasks-index.sqlite"
SESSION_DB = HOME / ".zcode/cli/db/db.sqlite"
STATE_DIR = HOME / ".zcode/auto-resume"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "monitor.lock"
LOG_FILE = STATE_DIR / "monitor.log"
STATUS_FILE = STATE_DIR / "status.json"
PORT_FILE = STATE_DIR / "dashboard.port"
DASHBOARD = Path(__file__).with_name("dashboard.html")
DESKTOP_SENDER = Path(__file__).with_name("desktop_send.applescript")
PROMPT = "Continue the unfinished task in this conversation. Review existing progress first, avoid repeating completed work, and report the result briefly."
INTERVAL_SECONDS = 120
MIN_AGE_SECONDS = 120
ACTIVE_WINDOW_SECONDS = 20 * 60
MAX_ATTEMPTS_PER_DAY = 100
COOLDOWN_SECONDS = 2 * 60
DELIVERY_TIMEOUT_SECONDS = 30


def connect_readonly(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)


def scan_sessions(now):
    if not TASK_DB.exists() or not SESSION_DB.exists():
        raise RuntimeError("ZCode task or session database was not found")
    now_ms = int(now * 1000)
    active_since = now_ms - ACTIVE_WINDOW_SECONDS * 1000
    with connect_readonly(TASK_DB) as db:
        rows = db.execute(
            """SELECT task_id, title, workspace_path, updated_at,
                      task_status, cli_last_outcome FROM tasks
               WHERE updated_at >= ? AND updated_at <= ?
                 AND deleted = 0 AND archived = 0
               ORDER BY updated_at DESC""",
            (active_since, now_ms),
        ).fetchall()
    scanned = []
    with connect_readonly(SESSION_DB) as db:
        for session_id, title, cwd, updated_at, task_status, outcome in rows:
            item = {
                "id": session_id, "title": title or session_id,
                "workspace": cwd, "updated_at": updated_at / 1000,
                "task_status": task_status or "unknown", "index_status": task_status or "unknown",
                "decision": "normal", "reason": "未检测到可重试错误", "message_at": None,
            }
            scanned.append(item)
            message = db.execute(
                """SELECT id, data, time_created FROM message WHERE session_id = ?
                   ORDER BY sequence DESC, time_created DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            if not message:
                item.update(decision="skipped", reason="会话没有消息")
                continue
            item["message_at"] = message[2] / 1000
            if not active_since <= message[2] <= now_ms:
                item.update(decision="skipped", reason="最后一条消息不在最近 20 分钟内")
                continue
            try:
                data = json.loads(message[1])
            except (ValueError, AttributeError):
                item.update(decision="skipped", reason="无法读取最后一条消息")
                continue
            message_time = data.get("time") or {}
            live_reply = (data.get("role") == "assistant" and not data.get("error")
                          and message_time.get("created")
                          and not message_time.get("completed"))
            running_tool = db.execute(
                """SELECT 1 FROM part WHERE session_id = ? AND time_updated >= ?
                   AND json_extract(data, '$.type') = 'tool'
                   AND json_extract(data, '$.state.status') = 'running'
                   LIMIT 1""",
                (session_id, active_since),
            ).fetchone()
            if live_reply or running_tool:
                detail = "最新回复尚未结束" if live_reply else "工具步骤仍标记为运行中"
                item.update(task_status="running", decision="running",
                            reason=f"{detail}；索引 {task_status} 不是实时状态")
                continue
            if task_status == "completed":
                if now_ms - message[2] < MIN_AGE_SECONDS * 1000:
                    item.update(task_status="recent", decision="recent",
                                reason="最近仍有对话更新；索引 completed 不能证明任务结束")
                else:
                    item.update(task_status="index_completed", decision="index_completed",
                                reason="索引为 completed；未检测到运行中的步骤")
                continue
            if task_status != "error" or outcome != "error":
                continue
            if now_ms - updated_at < MIN_AGE_SECONDS * 1000:
                item.update(decision="waiting", reason="错误发生不足 2 分钟")
                continue
            session = db.execute(
                "SELECT last_outcome FROM session WHERE id = ?", (session_id,)
            ).fetchone()
            if not session or session[0] != "error":
                item.update(decision="skipped", reason="会话状态与任务索引不一致")
                continue
            error = data.get("error") or {}
            details = error.get("data") or {}
            attribution = details.get("attribution") or {}
            code = details.get("code")
            reason = attribution.get("reason")
            if not (code == "model_rate_limited" or reason == "rate_limited"
                    or (code == "model_request_failed" and reason == "unknown")):
                item.update(decision="skipped", reason="最后一条消息没有可重试错误")
                continue
            if not Path(cwd).is_dir():
                item.update(decision="skipped", reason="工作目录不存在")
                continue
            item.update(decision="ready", reason="检测到可重试错误",
                        message_id=message[0], error_code=code or reason)
    return scanned


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)


def prompt_delivered(session_id, started_ms, prompt=PROMPT):
    with connect_readonly(SESSION_DB) as db:
        return db.execute(
            """SELECT 1 FROM message m JOIN part p ON p.message_id = m.id
               WHERE m.session_id = ? AND m.time_created >= ?
                 AND json_extract(m.data, '$.role') = 'user'
                 AND json_extract(p.data, '$.type') = 'text'
                 AND json_extract(p.data, '$.text') = ? LIMIT 1""",
            (session_id, started_ms, prompt),
        ).fetchone() is not None


def send_resume(session_id, title, started_at, prompt=PROMPT):
    with connect_readonly(TASK_DB) as db:
        same_titles = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE title = ? AND deleted = 0", (title,)
        ).fetchone()[0]
    if same_titles != 1:
        return False, "会话标题不唯一，无法安全定位桌面会话"
    try:
        result = subprocess.run(
            ["osascript", str(DESKTOP_SENDER), title, prompt],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if prompt_delivered(session_id, int(started_at * 1000), prompt):
            return True, "继续指令已进入原会话"
        return False, f"桌面发送器未完成：{exc}"
    if result.returncode != 0 or result.stdout.strip() != "submitted":
        if prompt_delivered(session_id, int(started_at * 1000), prompt):
            return True, "继续指令已进入原会话"
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        detail = detail[0][:240] if detail else f"退出码 {result.returncode}"
        with LOG_FILE.open("a") as log:
            print(f"desktop send {session_id}: {detail}", file=log)
        return False, f"桌面发送失败：{detail}"
    deadline = time.monotonic() + DELIVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if prompt_delivered(session_id, int(started_at * 1000), prompt):
            return True, "继续指令已进入原会话"
        time.sleep(0.5)
    return False, "桌面已尝试提交，但原会话 30 秒内未出现指令"


def run_once(dry_run=False):
    now = time.time()
    day = dt.datetime.fromtimestamp(now).astimezone().date().isoformat()
    state = load_state()
    if state.get("day") != day:
        state = {"day": day, "sessions": {}}
    previous = read_status()
    sessions = scan_sessions(now)
    events = previous.get("events", [])
    resumed = 0
    blocking_error = None
    for item in sessions:
        session_id = item["id"]
        record = state["sessions"].get(session_id, {})
        item["attempts_today"] = record.get("attempts", 0)
        if item["decision"] != "ready":
            continue
        message_id = item.pop("message_id")
        if record.get("attempts", 0) >= MAX_ATTEMPTS_PER_DAY:
            item.update(decision="limit", reason="已达今日 100 次上限")
            continue
        if record.get("message_id") == message_id:
            item.update(decision="handled", reason="这条错误已触发过续跑")
            continue
        if now - record.get("last_at", 0) < COOLDOWN_SECONDS:
            item.update(decision="cooldown", reason="等待 2 分钟冷却")
            continue
        print(f"resume {session_id} in {item['workspace']}", flush=True)
        if dry_run:
            continue
        state["sessions"][session_id] = {
            "attempts": record.get("attempts", 0) + 1,
            "last_at": now,
            "message_id": record.get("message_id"),
        }
        save_json(STATE_FILE, state)
        delivered, detail = send_resume(session_id, item["title"], now)
        if delivered:
            state["sessions"][session_id]["message_id"] = message_id
            save_json(STATE_FILE, state)
            resumed += 1
        item.update(decision="sent" if delivered else "failed", reason=detail,
                    attempts_today=record.get("attempts", 0) + 1)
        events.insert(0, {"at": now, "id": session_id,
                          "title": item["title"],
                          "type": "sent" if delivered else "failed",
                          "detail": detail})
        if not delivered:
            blocking_error = detail
            break
    if not dry_run:
        checks = previous.get("checks", [])
        checks.insert(0, {"at": now, "scanned": len(sessions), "resumed": resumed})
        save_json(STATUS_FILE, {
            "checked_at": now, "next_check_at": now + INTERVAL_SECONDS,
            "pid": os.getpid(), "interval_seconds": INTERVAL_SECONDS,
            "active_window_seconds": ACTIVE_WINDOW_SECONDS,
            "attempts_today_total": sum(
                record.get("attempts", 0) for record in state["sessions"].values()
            ),
            "sessions": sessions, "events": events[:50], "checks": checks[:30],
            "error": blocking_error,
        })
    return blocking_error


def read_status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            with self.server.monitor_control:
                status = read_status()
                status["monitoring"] = self.server.monitoring
                status["pid"] = os.getpid()
                if not self.server.monitoring:
                    status["next_check_at"] = None
            body = json.dumps(status, ensure_ascii=False).encode()
            content_type = "application/json; charset=utf-8"
        elif self.path == "/":
            body = DASHBOARD.read_bytes()
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in ("/api/start", "/api/stop"):
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if origin != expected or self.headers.get("X-ZCode-Control") != "1":
            self.send_error(403)
            return
        with self.server.monitor_control:
            self.server.monitoring = self.path == "/api/start"
            self.server.monitor_control.notify_all()
        body = json.dumps({"monitoring": self.server.monitoring}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        pass


def start_dashboard():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765), DashboardHandler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    server.monitor_control = threading.Condition()
    server.monitoring = False
    PORT_FILE.write_text(str(server.server_port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def daemon():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        server = start_dashboard()
        while True:
            with server.monitor_control:
                while not server.monitoring:
                    server.monitor_control.wait()
                try:
                    blocking_error = run_once()
                    if blocking_error:
                        server.monitoring = False
                except Exception as exc:
                    with LOG_FILE.open("a") as log:
                        print(f"{dt.datetime.now().isoformat()} monitor error: {exc}", file=log)
                    status = read_status()
                    status["error"] = str(exc)
                    save_json(STATUS_FILE, status)
                if server.monitoring:
                    server.monitor_control.wait(timeout=INTERVAL_SECONDS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "daemon", "once"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.action == "start":
        with LOG_FILE.open("a") as log:
            subprocess.Popen([sys.executable, __file__, "daemon"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    elif args.action == "daemon":
        daemon()
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
