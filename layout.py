#!/usr/bin/env python3
# Abhängigkeiten: gi (GTK4 + Adw), websockets (pip install websockets)

from __future__ import annotations

import asyncio
import ipaddress
import io
import json
import os
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

try:
    import websockets
except Exception:  # optional dependency
    websockets = None

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

    GI_AVAILABLE = True
except Exception:
    GI_AVAILABLE = False


def get_local_ip() -> str:
    """Get local LAN IP without relying on external connectivity."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


CONFIG_DIR = Path.home() / ".config" / "layout"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DESKTOP_FILE = Path.home() / ".local" / "share" / "applications" / "layout.desktop"
# Schüler erscheinen im Ranking erst ab mindestens 10 gelösten Einmaleins-Aufgaben.
MIN_RANKING_TASKS = 10
DEFAULT_RANDOM_MIN = 1
DEFAULT_RANDOM_MAX = 100
MAX_NAME_LENGTH = 40
WS_STARTUP_TIMEOUT = 3
WS_POLL_INTERVAL = 0.05
QR_INSTALL_HINT = "Für QR-Code installieren: pip install qrcode[pil]"


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_desktop_entry() -> None:
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    exec_path = Path(__file__).resolve()
    content = """[Desktop Entry]
Type=Application
Name=Layout
Exec=python3 {exec_path}
Icon=layout
Terminal=false
Categories=Education;
""".format(exec_path=exec_path)
    if not DESKTOP_FILE.exists() or "Icon=layout" not in DESKTOP_FILE.read_text(encoding="utf-8", errors="ignore"):
        DESKTOP_FILE.write_text(content, encoding="utf-8")


@dataclass
class Student:
    name: str
    connected: bool = True
    score_correct: int = 0
    score_total: int = 0
    waiting_question: str = ""
    waiting_answer: str = ""
    hand_raised_at: float | None = None
    hand_seen: bool = False
    needs_more_time: bool = False
    answered_current_task: bool = False
    feedback: str = ""


@dataclass
class TaskSession:
    mode: str  # custom | random
    timer: int
    start_ts: float
    show_feedback: bool = True
    input_type: str = "numeric"
    question: str = ""
    answer: str = ""
    random_tasks: dict[str, tuple[str, str]] = field(default_factory=dict)
    answers: dict[str, str] = field(default_factory=dict)
    correct: dict[str, bool] = field(default_factory=dict)
    revealed: bool = False
    random_op: str = "gemischt"
    random_min: int = DEFAULT_RANDOM_MIN
    random_max: int = DEFAULT_RANDOM_MAX


class LayoutState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.students: dict[str, Student] = {}
        self.current_task: TaskSession | None = None
        self.listeners: list[Callable[[], None]] = []
        self.server_ip = ""
        self.server_port = 0

    def add_listener(self, listener: Callable[[], None]) -> None:
        self.listeners.append(listener)

    def notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                pass

    def join(self, name: str) -> None:
        with self.lock:
            if not name:
                return
            student = self.students.get(name)
            if student is None:
                self.students[name] = Student(name=name)
            else:
                student.connected = True
        self.notify()

    def connected_total(self) -> int:
        with self.lock:
            return len(self.students)

    def answered_count(self) -> int:
        with self.lock:
            if not self.current_task:
                return 0
            return len(self.current_task.answers)

    def correct_count(self) -> int:
        with self.lock:
            if not self.current_task:
                return 0
            return sum(1 for ok in self.current_task.correct.values() if ok)

    def wrong_count(self) -> int:
        with self.lock:
            if not self.current_task:
                return 0
            return sum(1 for ok in self.current_task.correct.values() if not ok)

    def pending_count(self) -> int:
        with self.lock:
            return max(0, self.connected_total() - self.answered_count())

    def seconds_left(self) -> int:
        with self.lock:
            if not self.current_task:
                return 0
            elapsed = int(time.time() - self.current_task.start_ts)
            return max(0, self.current_task.timer - elapsed)

    def _normalize_math(self, value: str) -> str:
        return value.replace(" ", "").replace("×", "x").lower().strip()

    def _mk_random_question(self, op: str, min_v: int, max_v: int) -> tuple[str, str]:
        a = random.randint(min_v, max_v)
        b = random.randint(min_v, max_v)
        selected = random.choice(["+", "-", "×", "÷"]) if op == "gemischt" else op
        if selected == "+":
            return f"{a} + {b}", str(a + b)
        if selected == "-":
            if b > a:
                a, b = b, a
            return f"{a} - {b}", str(a - b)
        if selected == "×":
            return f"{a} × {b}", str(a * b)
        b = random.randint(max(1, min_v), max_v)
        ans = random.randint(min_v, max_v)
        a = b * ans
        return f"{a} ÷ {b}", str(ans)

    def start_custom_task(self, question: str, answer: str, timer: int, show_feedback: bool = True) -> None:
        with self.lock:
            self.current_task = TaskSession(
                mode="custom",
                timer=timer,
                start_ts=time.time(),
                show_feedback=show_feedback,
                input_type="numeric",
                question=question,
                answer=answer,
            )
            for student in self.students.values():
                student.answered_current_task = False
                student.needs_more_time = False
                student.hand_raised_at = None
                student.hand_seen = False
                student.feedback = ""
        self.notify()

    def start_random_task(self, op: str, min_v: int, max_v: int, timer: int) -> None:
        with self.lock:
            task = TaskSession(
                mode="random",
                timer=timer,
                start_ts=time.time(),
                show_feedback=True,
                input_type="numeric",
                random_op=op,
                random_min=min_v,
                random_max=max_v,
            )
            for name in self.students:
                task.random_tasks[name] = self._mk_random_question(op, min_v, max_v)
            self.current_task = task
            for student in self.students.values():
                student.answered_current_task = False
                student.needs_more_time = False
                student.hand_raised_at = None
                student.hand_seen = False
                student.feedback = ""
        self.notify()

    def question_for(self, name: str) -> tuple[str, str]:
        with self.lock:
            if not self.current_task:
                return "", ""
            if self.current_task.mode == "custom":
                return self.current_task.question, self.current_task.answer
            if name not in self.current_task.random_tasks:
                self.current_task.random_tasks[name] = self._mk_random_question(
                    self.current_task.random_op,
                    self.current_task.random_min,
                    self.current_task.random_max,
                )
            return self.current_task.random_tasks[name]

    def submit_answer(self, name: str, value: str) -> dict[str, Any]:
        with self.lock:
            task = self.current_task
            if not task:
                return {"accepted": False}
            normalized = self._normalize_math(value)
            _, answer = self.question_for(name)
            ok = normalized == self._normalize_math(answer)
            task.answers[name] = value
            task.correct[name] = ok
            student = self.students.setdefault(name, Student(name=name))
            student.answered_current_task = True
            if task.show_feedback:
                student.feedback = "✅ Richtig!" if ok else f"❌ Leider falsch, Lösung war: {answer}"
            else:
                student.feedback = ""
            payload = {"accepted": True, "correct": ok, "feedback": student.feedback}
        self.notify()
        return payload

    def reveal(self) -> str:
        with self.lock:
            if not self.current_task:
                return ""
            self.current_task.revealed = True
            if self.current_task.mode == "custom":
                answer = self.current_task.answer
            else:
                answer = "Jede/r hatte eine eigene Aufgabe"
        self.notify()
        return answer

    def raise_hand(self, name: str) -> None:
        with self.lock:
            student = self.students.setdefault(name, Student(name=name))
            student.hand_raised_at = time.time()
            student.hand_seen = False
        self.notify()

    def ack_hand(self, name: str) -> None:
        with self.lock:
            if name in self.students:
                self.students[name].hand_seen = True
        self.notify()

    def reset_hands(self) -> None:
        with self.lock:
            for student in self.students.values():
                student.hand_raised_at = None
                student.hand_seen = False
        self.notify()

    def need_more_time(self, name: str) -> bool:
        with self.lock:
            student = self.students.setdefault(name, Student(name=name))
            if student.needs_more_time:
                return False
            student.needs_more_time = True
        self.notify()
        return True

    def extend_for_more_time(self, seconds: int) -> None:
        with self.lock:
            if self.current_task:
                self.current_task.timer += max(0, seconds)
            for student in self.students.values():
                student.needs_more_time = False
        self.notify()

    def active_hands(self) -> list[tuple[str, str, bool]]:
        with self.lock:
            out = []
            for student in self.students.values():
                if student.hand_raised_at is not None:
                    ts = datetime.fromtimestamp(student.hand_raised_at).strftime("%H:%M:%S")
                    out.append((student.name, ts, student.hand_seen))
            return sorted(out, key=lambda x: x[1])

    def more_time_requests(self) -> list[str]:
        with self.lock:
            return sorted([s.name for s in self.students.values() if s.needs_more_time])

    def waiting_task_for(self, name: str) -> tuple[str, str]:
        with self.lock:
            student = self.students.setdefault(name, Student(name=name))
            if not student.waiting_question:
                a = random.randint(1, 10)
                b = random.randint(1, 10)
                student.waiting_question = f"{a} × {b}"
                student.waiting_answer = str(a * b)
            return student.waiting_question, student.waiting_answer

    def submit_waiting_answer(self, name: str, answer: str) -> dict[str, Any]:
        with self.lock:
            student = self.students.setdefault(name, Student(name=name))
            _, expected = self.waiting_task_for(name)
            ok = self._normalize_math(answer) == self._normalize_math(expected)
            student.score_total += 1
            if ok:
                student.score_correct += 1
            a = random.randint(1, 10)
            b = random.randint(1, 10)
            student.waiting_question = f"{a} × {b}"
            student.waiting_answer = str(a * b)
            payload = {
                "correct": ok,
                "score": student.score_correct,
                "total": student.score_total,
                "percent": int((student.score_correct / student.score_total) * 100) if student.score_total else 0,
                "next_question": student.waiting_question,
            }
        self.notify()
        return payload

    def ranking(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = []
            for student in self.students.values():
                if student.score_total < MIN_RANKING_TASKS:
                    continue
                percent = int((student.score_correct / student.score_total) * 100)
                rows.append({"name": student.name, "percent": percent, "count": student.score_total})
            rows.sort(key=lambda x: (-x["percent"], -x["count"], x["name"].lower()))
            return rows

    def reset_ranking(self) -> None:
        with self.lock:
            for student in self.students.values():
                student.score_correct = 0
                student.score_total = 0
                student.waiting_question = ""
                student.waiting_answer = ""
        self.notify()

    def snapshot_for(self, name: str) -> dict[str, Any]:
        with self.lock:
            student = self.students.setdefault(name, Student(name=name))
            if self.current_task:
                q, a = self.question_for(name)
                return {
                    "mode": "task",
                    "task_type": "task" if self.current_task.mode == "custom" else "random_task",
                    "question": q,
                    "answer": a,
                    "timer": self.current_task.timer,
                    "seconds_left": self.seconds_left(),
                    "answered": self.answered_count(),
                    "total": self.connected_total(),
                    "feedback": student.feedback,
                    "revealed": self.current_task.revealed,
                    "reveal_answer": self.current_task.answer if self.current_task.mode == "custom" else "",
                    "hand_seen": student.hand_seen,
                }
            waiting_q, _ = self.waiting_task_for(name)
            return {
                "mode": "waiting",
                "ranking": self.ranking(),
                "waiting_question": waiting_q,
                "score": student.score_correct,
                "total": student.score_total,
                "percent": int((student.score_correct / student.score_total) * 100) if student.score_total else 0,
                "hand_seen": student.hand_seen,
            }


class LayoutWebSocketServer(threading.Thread):
    def __init__(self, state: LayoutState, host: str = "0.0.0.0") -> None:
        super().__init__(daemon=True)
        self.state = state
        self.host = host
        self.port = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = None
        self.clients: dict[Any, str] = {}

    def run(self) -> None:
        if websockets is None:
            return
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def handler(websocket: Any):
            self.clients[websocket] = ""
            try:
                async for raw in websocket:
                    data = json.loads(raw)
                    name = self.clients.get(websocket, "")
                    msg_type = data.get("type")
                    if msg_type == "join":
                        name = str(data.get("name", "")).strip()[:MAX_NAME_LENGTH]
                        self.clients[websocket] = name
                        self.state.join(name)
                    elif msg_type == "answer" and name:
                        payload = self.state.submit_answer(name, str(data.get("value", "")))
                        await websocket.send(json.dumps({"type": "answer_feedback", **payload}, ensure_ascii=False))
                    elif msg_type == "raise_hand" and name:
                        self.state.raise_hand(name)
                    elif msg_type == "need_more_time" and name:
                        self.state.need_more_time(name)
                    elif msg_type == "times_table_answer" and name:
                        result = self.state.submit_waiting_answer(name, str(data.get("answer", "")))
                        await websocket.send(json.dumps({"type": "times_table_result", **result}, ensure_ascii=False))
                    await self.send_state_for(websocket)
                    await self.broadcast_progress()
            finally:
                self.clients.pop(websocket, None)

        async def start_server() -> None:
            self.server = await websockets.serve(handler, self.host, 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self.state.add_listener(self._schedule_broadcast)

        self.loop.run_until_complete(start_server())
        self.loop.run_forever()

    async def send_state_for(self, websocket: Any) -> None:
        name = self.clients.get(websocket, "")
        if not name:
            return
        snap = self.state.snapshot_for(name)
        if snap["mode"] == "task":
            await websocket.send(
                json.dumps(
                    {
                        "type": snap["task_type"],
                        "question": snap["question"],
                        "answer": snap["answer"],
                        "timer": snap["timer"],
                        "show_feedback": True,
                        "input_type": "numeric",
                    },
                    ensure_ascii=False,
                )
            )
            await websocket.send(json.dumps({"type": "timer_update", "seconds_left": snap["seconds_left"]}, ensure_ascii=False))
            await websocket.send(
                json.dumps({"type": "progress", "answered": snap["answered"], "total": snap["total"]}, ensure_ascii=False)
            )
            if snap.get("feedback"):
                await websocket.send(json.dumps({"type": "answer_feedback", "feedback": snap["feedback"]}, ensure_ascii=False))
            if snap.get("revealed"):
                await websocket.send(json.dumps({"type": "reveal", "correct_answer": snap.get("reveal_answer", "")}, ensure_ascii=False))
            if snap.get("hand_seen"):
                await websocket.send(json.dumps({"type": "raise_hand_ack", "name": name}, ensure_ascii=False))
            if snap["seconds_left"] <= 0:
                await websocket.send(json.dumps({"type": "times_up"}, ensure_ascii=False))
            return

        await websocket.send(json.dumps({"type": "waiting"}, ensure_ascii=False))
        await websocket.send(json.dumps({"type": "ranking", "entries": snap["ranking"]}, ensure_ascii=False))
        await websocket.send(
            json.dumps(
                {
                    "type": "times_table_state",
                    "question": snap["waiting_question"],
                    "score": snap["score"],
                    "total": snap["total"],
                    "percent": snap["percent"],
                },
                ensure_ascii=False,
            )
        )
        if snap.get("hand_seen"):
            await websocket.send(json.dumps({"type": "raise_hand_ack", "name": name}, ensure_ascii=False))

    def _schedule_broadcast(self) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast_all()))

    async def broadcast_all(self) -> None:
        stale = []
        for websocket in list(self.clients.keys()):
            try:
                await self.send_state_for(websocket)
            except Exception:
                stale.append(websocket)
        for ws in stale:
            self.clients.pop(ws, None)

    async def broadcast_progress(self) -> None:
        answered = self.state.answered_count()
        total = self.state.connected_total()
        msg = json.dumps({"type": "progress", "answered": answered, "total": total}, ensure_ascii=False)
        for websocket in list(self.clients.keys()):
            try:
                await websocket.send(msg)
            except Exception:
                self.clients.pop(websocket, None)


class LayoutHandler(BaseHTTPRequestHandler):
    state: LayoutState | None = None
    ws_port: int | None = None

    def _json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._html(self.student_page())
            return
        if parsed.path == "/state":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0].strip()[:MAX_NAME_LENGTH]
            if not name:
                self._json({"error": "name required"}, HTTPStatus.BAD_REQUEST)
                return
            assert self.state, "Handler state not initialized"
            self.state.join(name)
            self._json(self.state.snapshot_for(name))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        assert self.state, "Handler state not initialized"

        if parsed.path == "/api/join":
            self.state.join(str(data.get("name", "")).strip()[:MAX_NAME_LENGTH])
            self._json({"ok": True})
            return
        if parsed.path == "/api/answer":
            result = self.state.submit_answer(
                str(data.get("name", "")).strip()[:MAX_NAME_LENGTH],
                str(data.get("value", "")),
            )
            self._json(result)
            return
        if parsed.path == "/api/raise_hand":
            self.state.raise_hand(str(data.get("name", "")).strip()[:MAX_NAME_LENGTH])
            self._json({"ok": True})
            return
        if parsed.path == "/api/need_more_time":
            accepted = self.state.need_more_time(str(data.get("name", "")).strip()[:MAX_NAME_LENGTH])
            self._json({"ok": accepted})
            return
        if parsed.path == "/api/times_table_answer":
            result = self.state.submit_waiting_answer(
                str(data.get("name", "")).strip()[:MAX_NAME_LENGTH],
                str(data.get("answer", "")),
            )
            self._json(result)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def student_page(self) -> str:
        ws_port = self.ws_port
        return f"""<!doctype html>
<html lang=\"de\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>Layout Quiz</title>
<style>
body {{ margin: 0; font-family: system-ui, sans-serif; background: #111; color: #f2f2f2; display:flex; justify-content:center; }}
.card {{ width: min(640px, 94vw); margin: 20px; border: 1px solid #333; border-radius: 12px; overflow:hidden; background:#1b1b1b; }}
.row {{ padding: 14px; border-bottom: 1px solid #2b2b2b; }}
.top {{ display:flex; justify-content:space-between; font-weight:700; }}
#question {{ min-height: 110px; display:flex; align-items:center; justify-content:center; font-size: clamp(1.7rem, 5vw, 2.5rem); text-align:center; }}
.inputrow {{ display:flex; gap:8px; }}
input, button {{ font-size: 1rem; border-radius:10px; border:1px solid #3c3c3c; padding: 10px 12px; }}
input {{ flex:1; background:#0f0f0f; color:white; }}
button {{ background:#2d6cdf; color:#fff; cursor:pointer; }}
button.secondary {{ background:#444; }}
button:disabled {{ opacity: .55; cursor:not-allowed; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
#status {{ min-height: 24px; font-weight:600; }}
.ok {{ color:#58d26a; }}
.err {{ color:#ff6a6a; }}
.subtle {{ color:#b9b9b9; }}
.hidden {{ display:none; }}
.rank li {{ display:flex; justify-content:space-between; margin:6px 0; }}
.waitanim {{ opacity:.8; animation:pulse 1.2s infinite; }}
@keyframes pulse {{ 0%{{opacity:.3}}50%{{opacity:1}}100%{{opacity:.3}} }}
</style>
</head>
<body>
<div class=\"card\">
  <div id=\"join\" class=\"row\">
    <div style=\"font-size:1.2rem;margin-bottom:10px\">Wie heißt du?</div>
    <div class=\"inputrow\"><input id=\"nameInput\" placeholder=\"Dein Name\" /><button id=\"saveName\">Start</button></div>
  </div>
  <div id=\"main\" class=\"hidden\">
    <div class=\"row top\"><span id=\"nameLabel\">👤</span><span>⏱️ <span id=\"timer\">-</span>s</span></div>
    <div class=\"row\" id=\"question\"></div>
    <div class=\"row\"><div class=\"inputrow\"><input id=\"answerInput\" inputmode=\"numeric\" /><button id=\"submitBtn\">Abgeben</button></div></div>
    <div class=\"row grid2\"><button id=\"raiseBtn\" class=\"secondary\">✋ Aufzeigen</button><button id=\"moreBtn\" class=\"secondary\">⏳ Mehr Zeit</button></div>
    <div class=\"row\"><div id=\"progress\" class=\"subtle\">0 von 0 haben abgegeben</div><div id=\"status\"></div></div>
  </div>

  <div id=\"waiting\" class=\"hidden\">
    <div class=\"row\"><strong>🏆 Bestenliste</strong><ul id=\"ranking\" class=\"rank\"></ul><div class=\"subtle\">(nur ab 10 Aufgaben)</div></div>
    <div class=\"row\">
      <strong>Einmaleins-Training</strong>
      <div id=\"waitQuestion\" style=\"font-size:2rem;text-align:center;margin:14px 0\"></div>
      <div class=\"inputrow\"><input id=\"waitInput\" inputmode=\"numeric\"/><button id=\"waitSubmit\">Prüfen</button></div>
      <div id=\"waitStatus\" class=\"subtle waitanim\" style=\"margin-top:8px\">Gleich geht's los...</div>
      <div style=\"margin-top:8px\">Dein Score: <span id=\"score\">0</span> richtig</div>
      <div>Genauigkeit: <span id=\"percent\">0</span>%</div>
    </div>
  </div>
</div>

<script>
const WS_PORT = {json.dumps(ws_port)};
const WS_URL = WS_PORT ? `ws://${{location.hostname}}:${{WS_PORT}}` : '';
let ws = null;
let hasWs = false;
let statePoll = null;
let name = localStorage.getItem('layout_name') || '';
let moreTimeUsed = false;
let currentMode = 'waiting';
let remainingSeconds = null;
const MAX_NAME_LENGTH = 40;

const $ = (id) => document.getElementById(id);

function setStatus(text, ok=false) {{
  const el = $('status');
  el.className = ok ? 'ok' : (text.includes('❌') ? 'err' : 'subtle');
  el.textContent = text;
}}

function showMain() {{ $('main').classList.remove('hidden'); $('waiting').classList.add('hidden'); currentMode='task'; }}
function showWaiting() {{ $('waiting').classList.remove('hidden'); $('main').classList.add('hidden'); currentMode='waiting'; $('timer').textContent='-'; remainingSeconds = null; }}

function ensureNameUI() {{
  if (name) {{
    $('join').classList.add('hidden');
    $('nameLabel').textContent = `👤 ${{name}}`;
    if (hasWs && ws) ws.send(JSON.stringify({{type:'join', name}}));
    else post('/api/join', {{name}});
  }} else {{
    $('join').classList.remove('hidden');
  }}
}}

async function post(url, payload) {{
  const res = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(payload)}});
  return await res.json();
}}

async function pollState() {{
  if (!name) return;
  const res = await fetch(`/state?name=${{encodeURIComponent(name)}}`);
  const snap = await res.json();
  applySnapshot(snap);
}}

function applySnapshot(snap) {{
  if (snap.mode === 'task') {{
    showMain();
    $('question').textContent = snap.question || 'Aufgabe wird geladen...';
    remainingSeconds = snap.seconds_left;
    $('timer').textContent = remainingSeconds;
    $('progress').textContent = `${{snap.answered}} von ${{snap.total}} haben abgegeben`;
    if (snap.feedback) setStatus(snap.feedback, snap.feedback.includes('✅'));
    if (snap.revealed && snap.reveal_answer) setStatus(`Lösung: ${{snap.reveal_answer}}`);
    if (snap.hand_seen) setStatus('✅ Lehrer hat gesehen', true);
  }} else {{
    showWaiting();
    $('waitQuestion').textContent = snap.waiting_question;
    $('score').textContent = snap.score;
    $('percent').textContent = snap.percent;
    const list = $('ranking');
    list.innerHTML = '';
    (snap.ranking || []).forEach((e, i) => {{
      const li = document.createElement('li');
      li.innerHTML = `<span>${{i+1}}. ${{e.name}}</span><span>${{e.percent}}% (${{e.count}})</span>`;
      list.appendChild(li);
    }});
    if (snap.hand_seen) setStatus('✅ Lehrer hat gesehen', true);
  }}
}}

function onMessage(data) {{
  if (data.type === 'task' || data.type === 'random_task') {{
    showMain();
    $('question').textContent = data.question;
    remainingSeconds = data.timer;
    $('timer').textContent = remainingSeconds;
    moreTimeUsed = false;
    $('moreBtn').disabled = false;
    $('answerInput').inputMode = data.input_type || 'numeric';
    setStatus('');
  }}
  if (data.type === 'waiting') showWaiting();
  if (data.type === 'timer_update') {{ remainingSeconds = data.seconds_left; $('timer').textContent = remainingSeconds; }}
  if (data.type === 'progress') $('progress').textContent = `${{data.answered}} von ${{data.total}} haben abgegeben`;
  if (data.type === 'raise_hand_ack') setStatus('✅ Lehrer hat gesehen', true);
  if (data.type === 'reveal') setStatus(`Lösung: ${{data.correct_answer}}`);
  if (data.type === 'answer_feedback') setStatus(data.feedback || '', (data.feedback||'').includes('✅'));
  if (data.type === 'times_up') setStatus('⏳ Zeit abgelaufen');
  if (data.type === 'ranking') {{
    const list = $('ranking');
    list.innerHTML = '';
    (data.entries || []).forEach((e, i) => {{
      const li = document.createElement('li');
      li.innerHTML = `<span>${{i+1}}. ${{e.name}}</span><span>${{e.percent}}% (${{e.count}})</span>`;
      list.appendChild(li);
    }});
  }}
  if (data.type === 'times_table_state') {{
    $('waitQuestion').textContent = data.question;
    $('score').textContent = data.score;
    $('percent').textContent = data.percent;
  }}
  if (data.type === 'times_table_result') {{
    $('waitStatus').textContent = data.correct ? '✅ Richtig!' : '❌ Falsch';
    $('waitQuestion').textContent = data.next_question;
    $('score').textContent = data.score;
    $('percent').textContent = data.percent;
    $('waitInput').value = '';
    $('waitInput').focus();
  }}
}}

function connect() {{
  if (!WS_URL) {{
    hasWs = false;
    statePoll = setInterval(pollState, 2000);
    pollState();
    ensureNameUI();
    return;
  }}
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {{ hasWs = true; ensureNameUI(); }};
  ws.onclose = () => {{ hasWs = false; if (!statePoll) statePoll = setInterval(pollState, 2000); }};
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
}}

$('saveName').onclick = () => {{
  const v = $('nameInput').value.trim().slice(0, MAX_NAME_LENGTH);
  if (!v) return;
  name = v;
  localStorage.setItem('layout_name', name);
  ensureNameUI();
  pollState();
}};

$('submitBtn').onclick = async () => {{
  if (!name) return;
  const value = $('answerInput').value.trim();
  if (!value) return;
  if (hasWs && ws) ws.send(JSON.stringify({{type:'answer', value}}));
  else {{
    const r = await post('/api/answer', {{name, value}});
    if (r.feedback) setStatus(r.feedback, !!r.correct);
  }}
}};
$('answerInput').addEventListener('keydown', (e) => {{ if (e.key === 'Enter') $('submitBtn').click(); }});

$('raiseBtn').onclick = async () => {{
  if (!name) return;
  $('raiseBtn').disabled = true;
  $('raiseBtn').textContent = 'Warte auf Lehrer...';
  if (hasWs && ws) ws.send(JSON.stringify({{type:'raise_hand'}}));
  else await post('/api/raise_hand', {{name}});
}};

$('moreBtn').onclick = async () => {{
  if (!name || moreTimeUsed) return;
  moreTimeUsed = true;
  $('moreBtn').disabled = true;
  if (hasWs && ws) ws.send(JSON.stringify({{type:'need_more_time'}}));
  else await post('/api/need_more_time', {{name}});
}};

$('waitSubmit').onclick = async () => {{
  if (!name) return;
  const answer = $('waitInput').value.trim();
  if (!answer) return;
  if (hasWs && ws) ws.send(JSON.stringify({{type:'times_table_answer', question:$('waitQuestion').textContent, answer}}));
  else {{
    const r = await post('/api/times_table_answer', {{name, answer}});
    onMessage({{type:'times_table_result', ...r}});
  }}
}};
$('waitInput').addEventListener('keydown', (e) => {{ if (e.key === 'Enter') $('waitSubmit').click(); }});

connect();
ensureNameUI();
setInterval(() => {{
  if (currentMode === 'task' && remainingSeconds !== null && remainingSeconds > 0) {{
    remainingSeconds -= 1;
    $('timer').textContent = remainingSeconds;
    if (remainingSeconds === 0) setStatus('⏳ Zeit abgelaufen');
  }}
}}, 1000);
</script>
</body>
</html>"""


def start_http_server(state: LayoutState, ws_port: int | None) -> ThreadingHTTPServer:
    LayoutHandler.state = state
    LayoutHandler.ws_port = ws_port
    server = ThreadingHTTPServer(("0.0.0.0", 0), LayoutHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if GI_AVAILABLE:
    class LayoutWindow(Adw.ApplicationWindow):
        def __init__(self, app: "LayoutApp", state: LayoutState, local_ip: str, http_port: int, ws_port: int | None):
            super().__init__(application=app)
            self.state = state
            self.set_title("Layout LAN Quiz")
            self.set_default_size(860, 640)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            root.set_margin_top(12)
            root.set_margin_bottom(12)
            root.set_margin_start(12)
            root.set_margin_end(12)

            info = Gtk.Label(
                xalign=0,
                label=f"Browser-URL: http://{local_ip}:{http_port}    WebSocket: {'aktiv' if ws_port else 'Fallback Polling'}",
            )
            root.append(info)

            connect_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            connect_box.append(Gtk.Label(label="Verbinden", xalign=0))
            self.qr_picture = Gtk.Picture()
            self.qr_picture.set_size_request(220, 220)
            connect_box.append(self.qr_picture)
            self.url_label = Gtk.Label(xalign=0)
            self.url_label.set_selectable(True)
            connect_box.append(self.url_label)
            self.qr_hint_label = Gtk.Label(xalign=0)
            connect_box.append(self.qr_hint_label)
            root.append(connect_box)

            notebook = Gtk.Notebook()
            root.append(notebook)

            self.task_tab = self._build_task_tab()
            self.rand_tab = self._build_random_tab()
            self.hand_tab = self._build_hands_tab()
            self.more_tab = self._build_more_time_tab()
            self.rank_tab = self._build_rank_tab()

            notebook.append_page(self.task_tab, Gtk.Label(label="Aufgaben"))
            notebook.append_page(self.rand_tab, Gtk.Label(label="Zufallsgenerator"))
            notebook.append_page(self.hand_tab, Gtk.Label(label="Aufzeigen"))
            notebook.append_page(self.more_tab, Gtk.Label(label="Mehr-Zeit"))
            notebook.append_page(self.rank_tab, Gtk.Label(label="Ranking"))

            self.set_content(root)
            self._refresh_connect_ui()
            GLib.timeout_add_seconds(1, self.refresh)

        def _set_qr(self, picture: Gtk.Picture, url: str) -> None:
            if not url:
                picture.set_paintable(None)
                return
            if qrcode is None:
                print(f"QR-Code deaktiviert: {QR_INSTALL_HINT}", file=sys.stderr)
                picture.set_paintable(None)
                return
            try:
                img = qrcode.make(url)
                with io.BytesIO() as buffer:
                    img.save(buffer, format="PNG")
                    png_data = buffer.getvalue()
                loader = GdkPixbuf.PixbufLoader.new_with_type("png")
                try:
                    loader.write(png_data)
                finally:
                    loader.close()
                pixbuf = loader.get_pixbuf()
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                picture.set_paintable(texture)
            except (TypeError, ValueError, OSError, RuntimeError) as exc:
                print(f"QR-Code für {url} konnte nicht erzeugt werden: {exc}", file=sys.stderr)
                picture.set_paintable(None)

        def _url_for_students(self) -> str:
            host = self.state.server_ip
            port = self.state.server_port
            if not host or not (1 <= port <= 65535):
                return ""
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return ""
            return f"http://{host}:{port}/"

        def _refresh_connect_ui(self) -> None:
            url = self._url_for_students()
            self.url_label.set_text(url or "Warten auf Server-Start …")
            self._set_qr(self.qr_picture, url)
            self.qr_hint_label.set_text(QR_INSTALL_HINT if qrcode is None else "")

        def _timer_combo(self) -> Gtk.DropDown:
            model = Gtk.StringList.new(["30", "60", "90"])
            combo = Gtk.DropDown.new(model, None)
            combo.set_selected(1)
            return combo

        def _build_task_tab(self) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.question_entry = Gtk.Entry(placeholder_text="Aufgabe (z.B. 3x + 7 = 22)")
            self.answer_entry = Gtk.Entry(placeholder_text="Richtige Antwort")
            self.timer_combo = self._timer_combo()
            start_btn = Gtk.Button(label="Aufgabe starten")
            reveal_btn = Gtk.Button(label="Auflösung zeigen")
            self.live_label = Gtk.Label(xalign=0)
            self.progress = Gtk.ProgressBar()

            start_btn.connect("clicked", self.on_start_custom)
            reveal_btn.connect("clicked", self.on_reveal)

            box.append(self.question_entry)
            box.append(self.answer_entry)
            box.append(Gtk.Label(label="Timer"))
            box.append(self.timer_combo)
            box.append(start_btn)
            box.append(reveal_btn)
            box.append(self.live_label)
            box.append(self.progress)
            return box

        def _build_random_tab(self) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.op_combo = Gtk.DropDown.new(Gtk.StringList.new(["+", "-", "×", "÷", "gemischt"]), None)
            self.op_combo.set_selected(4)
            self.range_from = Gtk.SpinButton.new_with_range(1, 1000, 1)
            self.range_to = Gtk.SpinButton.new_with_range(1, 1000, 1)
            self.range_from.set_value(DEFAULT_RANDOM_MIN)
            self.range_to.set_value(DEFAULT_RANDOM_MAX)
            self.random_timer_combo = self._timer_combo()
            start = Gtk.Button(label="Zufallsaufgabe starten")
            start.connect("clicked", self.on_start_random)
            box.append(Gtk.Label(label="Rechenart"))
            box.append(self.op_combo)
            box.append(Gtk.Label(label="Von"))
            box.append(self.range_from)
            box.append(Gtk.Label(label="Bis"))
            box.append(self.range_to)
            box.append(Gtk.Label(label="Timer"))
            box.append(self.random_timer_combo)
            box.append(start)
            return box

        def _build_hands_tab(self) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.hands_list = Gtk.ListBox()
            reset_btn = Gtk.Button(label="Alle zurücksetzen")
            reset_btn.connect("clicked", lambda *_: self.state.reset_hands())
            box.append(self.hands_list)
            box.append(reset_btn)
            return box

        def _build_more_time_tab(self) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.more_list = Gtk.ListBox()
            self.more_seconds = Gtk.SpinButton.new_with_range(5, 300, 5)
            self.more_seconds.set_value(30)
            extend_btn = Gtk.Button(label="Zeit für diese Schüler verlängern")
            extend_btn.connect("clicked", self.on_extend_time)
            box.append(self.more_list)
            box.append(self.more_seconds)
            box.append(extend_btn)
            return box

        def _build_rank_tab(self) -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.rank_list = Gtk.ListBox()
            reset = Gtk.Button(label="Ranking zurücksetzen")
            reset.connect("clicked", lambda *_: self.state.reset_ranking())
            box.append(self.rank_list)
            box.append(reset)
            return box

        def on_start_custom(self, *_: Any) -> None:
            q = self.question_entry.get_text().strip()
            a = self.answer_entry.get_text().strip()
            if not q or not a:
                return
            timer = int(self.timer_combo.get_selected_item().get_string())
            self.state.start_custom_task(q, a, timer, show_feedback=True)

        def on_start_random(self, *_: Any) -> None:
            op = self.op_combo.get_selected_item().get_string()
            frm = int(self.range_from.get_value())
            to = int(self.range_to.get_value())
            if to < frm:
                frm, to = to, frm
            timer = int(self.random_timer_combo.get_selected_item().get_string())
            self.state.start_random_task(op, frm, to, timer)

        def on_reveal(self, *_: Any) -> None:
            self.state.reveal()

        def on_extend_time(self, *_: Any) -> None:
            self.state.extend_for_more_time(int(self.more_seconds.get_value()))

        def refresh(self) -> bool:
            total = self.state.connected_total()
            ans = self.state.answered_count()
            cor = self.state.correct_count()
            wrong = self.state.wrong_count()
            pending = self.state.pending_count()
            self.live_label.set_text(f"✅ {cor} richtig | ❌ {wrong} falsch | ⏳ {pending} noch am Rechnen | 👥 {total} gesamt")
            self.progress.set_fraction((ans / total) if total else 0.0)

            while (row := self.hands_list.get_first_child()) is not None:
                self.hands_list.remove(row)
            for name, ts, seen in self.state.active_hands():
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.append(Gtk.Label(label=f"{name} ({ts})", xalign=0))
                btn = Gtk.Button(label="Gesehen" if not seen else "✅ Gesehen")
                btn.set_sensitive(not seen)
                btn.connect("clicked", lambda _btn, n=name: self.state.ack_hand(n))
                row.append(btn)
                self.hands_list.append(row)

            while (row := self.more_list.get_first_child()) is not None:
                self.more_list.remove(row)
            for name in self.state.more_time_requests():
                self.more_list.append(Gtk.Label(label=name, xalign=0))

            while (row := self.rank_list.get_first_child()) is not None:
                self.rank_list.remove(row)
            for idx, entry in enumerate(self.state.ranking(), 1):
                label = Gtk.Label(
                    label=f"{idx}. {entry['name']} | {entry['percent']}% | {entry['count']} Aufgaben",
                    xalign=0,
                )
                self.rank_list.append(label)

            return True


    class LayoutApp(Adw.Application):
        def __init__(self, state: LayoutState, local_ip: str, http_port: int, ws_port: int | None):
            super().__init__(application_id="org.tutornachhilfe.layout")
            self.state = state
            self.local_ip = local_ip
            self.http_port = http_port
            self.ws_port = ws_port

        def do_activate(self) -> None:  # noqa: N802
            win = LayoutWindow(self, self.state, self.local_ip, self.http_port, self.ws_port)
            win.present()


else:
    class LayoutWindow:  # pragma: no cover
        pass

    class LayoutApp:  # pragma: no cover
        pass


def main() -> None:
    ensure_desktop_entry()
    settings = load_settings()

    state = LayoutState()
    ws_server = None
    ws_port = None

    if websockets is not None:
        ws_server = LayoutWebSocketServer(state)
        ws_server.start()
        timeout = time.time() + WS_STARTUP_TIMEOUT
        while ws_server.port == 0 and time.time() < timeout:
            time.sleep(WS_POLL_INTERVAL)
        ws_port = ws_server.port or None

    http_server = start_http_server(state, ws_port)
    local_ip = get_local_ip()
    state.server_ip = local_ip
    state.server_port = http_server.server_address[1]

    settings.update(
        {
            "last_http_port": http_server.server_address[1],
            "last_ws_port": ws_port,
            "last_ip": local_ip,
        }
    )
    save_settings(settings)

    print(f"Layout läuft auf http://{local_ip}:{http_server.server_address[1]}")
    print(f"WebSocket: {'aktiv auf Port ' + str(ws_port) if ws_port else 'nicht verfügbar (HTTP-Polling aktiv)'}")

    if GI_AVAILABLE:
        app = LayoutApp(state, local_ip, http_server.server_address[1], ws_port)
        app.run(None)
        return

    print("GTK/Libadwaita nicht verfügbar. Server läuft im Konsolenmodus. [Ctrl+C zum Beenden]")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
