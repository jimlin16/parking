# -*- coding: utf-8 -*-
"""Local web dashboard for the existing YouParking monitor and booking client."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from youparking_client import YouParkingClient
from booking_safety import (booking_marker_path, file_lock, execute_booking, matching_order, platform_success_message,
                            rejected_response, response_order_id)


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DEFAULT_START = "2026-10-11"
DEFAULT_END = "2026-10-13"
DEFAULT_PARKING_LOT = "p4"
PARKING_LOTS = {
    "p4": {
        "label": "桃園機場第二航廈 P4（原本場站）",
        "space_id": "1",
        "site_code": 84416,
    },
    "peace": {
        "label": "日月亭平安（大園）",
        "space_id": "9",
        "site_code": 15272193788,
    },
}


class InputError(ValueError):
    pass


def parse_date(value, label):
    if not isinstance(value, str):
        raise InputError(f"{label}格式不正確。")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InputError(f"{label}格式不正確。") from exc
    if parsed.isoformat() != value:
        raise InputError(f"{label}格式不正確。")
    return parsed


def validate_interval(value):
    if isinstance(value, bool) or not str(value).isdigit():
        raise InputError("掃描間隔必須是整數秒。")
    interval = int(value)
    if not 1 <= interval <= 3600:
        raise InputError("掃描間隔請設定為 1 至 3600 秒。")
    return interval


def parking_lot_key(data):
    """Resolve a saved lot key while keeping old config files compatible."""
    requested = str(data.get("parking_lot", "")).strip().lower()
    if requested in PARKING_LOTS:
        return requested
    for key, lot in PARKING_LOTS.items():
        if (str(data.get("space_id", "")) == lot["space_id"]
                and data.get("site_code") == lot["site_code"]):
            return key
    return DEFAULT_PARKING_LOT


def validate_reservation(data):
    if not isinstance(data, dict):
        raise InputError("預約資料格式不正確。")
    start = parse_date(data.get("start_date"), "入場日期")
    end = parse_date(data.get("end_date"), "離場日期")
    if start < date.today():
        raise InputError("入場日期不能早於今天。")
    if end < start:
        raise InputError("離場日期不能早於入場日期。")

    name = str(data.get("contact_name", "")).strip()
    car = str(data.get("car_no", "")).strip().upper()
    phone = str(data.get("phone", "")).strip()
    auto_book = data.get("auto_book", False)
    lot_key = str(data.get("parking_lot", DEFAULT_PARKING_LOT)).strip().lower()
    if lot_key not in PARKING_LOTS:
        raise InputError("請選擇有效的停車場。")
    if not name or len(name) > 60:
        raise InputError("請填寫 1 至 60 字的聯絡人姓名。")
    if not re.fullmatch(r"[A-Z0-9-]{2,20}", car):
        raise InputError("請填寫有效車牌，僅可使用英文字母、數字與連字號。")
    if len(phone) > 30 or (phone and not re.fullmatch(r"[0-9+()\- ]+", phone)):
        raise InputError("聯絡電話格式不正確。")
    if not isinstance(auto_book, bool):
        raise InputError("自動預約設定格式不正確。")
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "contact_name": name,
        "car_no": car,
        "phone": phone,
        "auto_book": auto_book,
        "parking_lot": lot_key,
    }


def order_result(response, before, after, config=None):
    """Prefer a matching record, then honor an explicit platform acknowledgement."""
    if config is not None:
        before_ids = {str(item.get('Model_ReservedOrderID')) for item in before
                      if isinstance(item, dict) and item.get('Model_ReservedOrderID')} if isinstance(before, list) else set()
        row = matching_order(after, config, expected_id=response_order_id(response),
                             exclude_ids=before_ids)
        if row:
            return "success", f"已核對預約訂單 {row['Model_ReservedOrderID']}。"
    if rejected_response(response):
        return "failure", "平台回覆預約失敗。"
    platform_message = platform_success_message(response)
    if platform_message:
        return "success", platform_message
    return "info", "預約結果未確認，請先核對官方交易紀錄。"


class Dashboard:
    def __init__(self, config_path=ROOT / "config.json", log_path=ROOT / "logs" / "dashboard.jsonl",
                 client_factory=YouParkingClient):
        self.config_path = Path(config_path)
        self.log_path = Path(log_path)
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.booking_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.phase = "idle"
        self.next_scan_at = None
        self.last_scan_at = None
        self.last_result = None
        self.events = deque(maxlen=200)
        self.event_id = 0
        self.google_login = {"status": "checking", "checked_at": None, "login_open": False}
        self.google_login_process = None
        self.config = self._read_config()
        self._clear_runtime_results()

    def _read_config(self):
        with self.config_path.open("r", encoding="utf-8") as file:
            config = json.load(file)
        if not isinstance(config, dict):
            raise ValueError("config.json 必須是 JSON 物件。")
        return config

    def check_google_login(self):
        from google_login_status import check_google_login
        try:
            status = check_google_login()
        except Exception:
            status = "unknown"
        with self.lock:
            self.google_login.update(status=status, checked_at=datetime.now().astimezone().isoformat())

    def open_google_login(self):
        with self.lock:
            if self.google_login["login_open"] or self.google_login["status"] == "checking":
                raise InputError("登入視窗已開啟或正在檢查，請稍候。")
            if self.phase == "booking":
                raise InputError("正在預約，請稍後再登入。")
            self.google_login_process = subprocess.Popen(
                [sys.executable, str(ROOT / "login_google.py")], cwd=str(ROOT),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.google_login["login_open"] = True
        def wait_for_login():
            self.google_login_process.wait()
            with self.lock:
                self.google_login.update(login_open=False, status="checking")
            self.check_google_login()
        threading.Thread(target=wait_for_login, daemon=True).start()
        return self.emit("Google 登入", "info", "已開啟登入視窗，完成後請關閉視窗。")

    def _public_config(self):
        config = self.config
        return {
            "parking_lot": parking_lot_key(config),
            "start_date": config.get("start_date", DEFAULT_START),
            "end_date": config.get("end_date", DEFAULT_END),
            "car_no": config.get("car_no", ""),
            "contact_name": config.get("contact_name", ""),
            "phone": config.get("phone", ""),
            "check_interval_seconds": config.get("check_interval_seconds", 10),
            "auto_book": config.get("auto_book", False),
        }

    def _write_config(self, config):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=self.config_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                json.dump(config, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self.config_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _clear_runtime_results(self):
        """Start a fresh execution session without changing saved reservation settings."""
        self.events.clear()
        self.event_id = 0
        self.last_result = None
        self.last_scan_at = None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")

    def reset_runtime(self):
        """Clear visible execution results when a new dashboard page is opened."""
        with self.lock:
            self._clear_runtime_results()

    def emit(self, action, status, message):
        with self.lock:
            self.event_id += 1
            event = {
                "id": self.event_id,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "action": action,
                "status": status,
                "message": str(message)[:240],
            }
            self.events.append(event)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > 2_000_000:
                rotated = self.log_path.with_suffix(".jsonl.1")
                os.replace(self.log_path, rotated)
            with self.log_path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(event, ensure_ascii=False) + "\n")
            return event

    def clear_booking_state(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise InputError("請先停止監控，再清除預約狀態。")
            if not self.scan_lock.acquire(blocking=False):
                raise InputError("掃描進行中，請稍後再清除預約狀態。")
            try:
                if not self.booking_lock.acquire(blocking=False):
                    raise InputError("預約送單中，請稍後再清除預約狀態。")
                try:
                    config = {**self.config, **self._public_config()}
                    lot = PARKING_LOTS[parking_lot_key(config)]
                    config.update(space_id=lot["space_id"], site_code=lot["site_code"])
                    state_dir = self.config_path.parent / '.booking-state'
                    with file_lock(state_dir / 'booking.lock'):
                        marker = booking_marker_path(config, state_dir)
                        existed = marker.exists()
                        marker.unlink(missing_ok=True)
                    message = "已清除目前預約的防重複狀態，可重新操作。" if existed else "目前預約沒有待清除的防重複狀態。"
                    return self.emit("清除預約狀態", "success", message)
                finally:
                    self.booking_lock.release()
            finally:
                self.scan_lock.release()

    def state(self):
        with self.lock:
            running = self.thread is not None and self.thread.is_alive()
            return {
                "config": self._public_config(),
                "google_login": dict(self.google_login),
                "parking_lots": [
                    {"key": key, "label": lot["label"]}
                    for key, lot in PARKING_LOTS.items()
                ],
                "monitor": {
                    "running": running,
                    "stopping": running and self.stop_event.is_set(),
                    "phase": self.phase,
                    "next_scan_at": self.next_scan_at,
                    "last_scan_at": self.last_scan_at,
                    "last_result": self.last_result,
                },
                "events": list(reversed(self.events)),
            }

    def _client(self, config):
        lot = PARKING_LOTS[parking_lot_key(config)]
        return self.client_factory(space_id=lot["space_id"],
                                   site_code=lot["site_code"])

    def save_interval(self, value):
        interval = validate_interval(value)
        with self.lock:
            updated = {**self.config, "check_interval_seconds": interval}
            self._write_config(updated)
            self.config = updated
            self.wake_event.set()
        return self.emit("掃描設定", "success", f"掃描間隔已設定為 {interval} 秒。")

    def save_reservation(self, payload):
        values = validate_reservation(payload)
        with self.lock:
            if self.phase == "booking":
                raise InputError("預約送單中，請稍後再修改資料。")
            if (self.thread and self.thread.is_alive() and values["auto_book"]
                    and not self.config.get("auto_book", False)):
                raise InputError("請先停止監控，再啟用自動預約。")
            lot = PARKING_LOTS[values["parking_lot"]]
            updated = {
                **self.config,
                **values,
                "space_id": lot["space_id"],
                "site_code": lot["site_code"],
            }
            self._write_config(updated)
            self.config = updated
        return self.emit("預約資料", "success", f"{lot['label']}與預約資料已儲存。")

    def start(self, confirm_auto_book=False):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise InputError("監控已在執行中。")
            if self.scan_lock.locked() or self.booking_lock.locked():
                raise InputError("目前有掃描或預約正在進行，請稍後再啟動監控。")
            validate_interval(self.config.get("check_interval_seconds", 10))
            validate_reservation(self._public_config())
            if self.config.get("auto_book", False) and confirm_auto_book is not True:
                raise InputError("啟動自動預約前需要確認。")
            self.stop_event = threading.Event()
            self.wake_event = threading.Event()
            self.thread = threading.Thread(target=self._worker, daemon=True, name="parking-monitor")
            self.thread.start()
        return self.emit("自動監控", "success", "監控已啟動，將立即進行第一次掃描。")

    def stop(self):
        with self.lock:
            if not self.thread or not self.thread.is_alive():
                raise InputError("監控目前未執行。")
            self.stop_event.set()
            self.wake_event.set()
            self.next_scan_at = None
        return self.emit("自動監控", "info", "正在停止監控；進行中的查詢結束後會停止。")

    def _worker(self):
        try:
            while not self.stop_event.is_set():
                self.scan_once(automatic=True)
                finished = time.monotonic()
                while not self.stop_event.is_set():
                    with self.lock:
                        interval = validate_interval(self.config.get("check_interval_seconds", 10))
                        remaining = max(0.0, interval - (time.monotonic() - finished))
                        self.next_scan_at = (datetime.now().astimezone() + timedelta(seconds=remaining)).isoformat(timespec="seconds")
                    if remaining <= 0:
                        break
                    self.wake_event.wait(remaining)
                    self.wake_event.clear()
        finally:
            with self.lock:
                self.phase = "idle"
                self.next_scan_at = None
            self.emit("自動監控", "info", "監控已停止。")

    def scan_once(self, automatic=False):
        if not self.scan_lock.acquire(blocking=False):
            raise InputError("已有掃描正在進行，請稍後再試。")
        try:
            with self.lock:
                config = {**self.config, **self._public_config()}
                self.phase = "scanning"
            validate_reservation(config)
            client = self._client(config)
            result = client.check_period_availability(config["start_date"], config["end_date"])
            if not isinstance(result, dict) or not isinstance(result.get("details"), list):
                raise RuntimeError("平台回傳的車位資料格式不正確。")
            with self.lock:
                self.last_scan_at = datetime.now().astimezone().isoformat(timespec="seconds")
                self.last_result = result
            if result.get("all_available"):
                event = self.emit("車位掃描", "success", "入場日期有可用車位。")
                if automatic and config.get("auto_book") and not self.stop_event.is_set():
                    with self.lock:
                        current = {**self.config, **self._public_config()}
                        unchanged = all(current.get(key) == config.get(key) for key in
                                        ("parking_lot", "start_date", "end_date", "car_no", "contact_name", "phone", "auto_book"))
                    if unchanged:
                        event = self._place_order(client, config, "自動預約")
                        self.stop_event.set()  # One booking attempt per monitoring run.
                        self.wake_event.set()
                    else:
                        event = self.emit("自動預約", "info", "掃描期間資料已變更，本次未送單；下次掃描使用新設定。")
                return event
            return self.emit("車位掃描", "info", "入場日期尚無可用車位。")
        except InputError:
            raise
        except Exception as exc:
            return self.emit("車位掃描", "error", f"查詢失敗：{str(exc)[:180]}")
        finally:
            with self.lock:
                if self.phase == "scanning":
                    self.phase = "idle"
            self.scan_lock.release()

    def _place_order(self, client, config, action):
        if not self.booking_lock.acquire(blocking=False):
            return self.emit(action, "failure", "已有一筆預約正在送出，未再次送單。")
        try:
            with self.lock:
                current = {**self.config, **self._public_config()}
                keys = ("parking_lot", "start_date", "end_date", "car_no", "contact_name",
                        "phone", "auto_book", "inv_type", "vehicle_code", "buyer_num",
                        "space_id", "site_code")
                if any(current.get(k) != config.get(k) for k in keys):
                    return self.emit(action, "failure", "預約資料已變更，本次未送單。")
                self.phase = "booking"
            status, message = execute_booking(
                client, config, self.config_path.parent / '.booking-state',
                notify=lambda message: self.emit("平台驗證", "info", message),
                cancelled=lambda: action == "自動預約" and self.stop_event.is_set())
            return self.emit(action, status, message)
        except Exception as exc:
            return self.emit(action, "error", f"預約操作失敗：{str(exc)[:180]}")
        finally:
            with self.lock:
                self.phase = "idle"
            self.booking_lock.release()

    def book_now(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise InputError("請先停止監控，再手動送出預約。")
            config = {**self.config, **self._public_config()}
        validate_reservation(config)
        if not self.scan_lock.acquire(blocking=False):
            raise InputError("目前正在掃描，請稍後再試。")
        try:
            client = self._client(config)
            try:
                result = client.check_period_availability(config["start_date"], config["end_date"])
            except Exception as exc:
                return self.emit("立即預約", "error", f"送單前查詢失敗：{str(exc)[:180]}")
            with self.lock:
                self.last_scan_at = datetime.now().astimezone().isoformat(timespec="seconds")
                self.last_result = result
            if not result.get("all_available"):
                return self.emit("立即預約", "failure", "入場日期尚無可用車位，未送出預約。")
            with self.lock:
                current = {**self.config, **self._public_config()}
                unchanged = all(current.get(key) == config.get(key) for key in
                                ("parking_lot", "start_date", "end_date", "car_no", "contact_name", "phone"))
            if not unchanged:
                return self.emit("立即預約", "failure", "查詢期間資料已變更，請重新確認後再送出。")
            return self._place_order(client, config, "立即預約")
        finally:
            self.scan_lock.release()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ParkingDashboard/1.0"

    def log_message(self, format_string, *args):
        pass

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/instance":
            return self._json(200, {"root": str(ROOT.resolve()), "pid": os.getpid()})
        if route == "/api/state":
            return self._json(200, self.server.dashboard.state())
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        if route not in files:
            return self._json(404, {"error": "找不到頁面。"})
        filename, content_type = files[route]
        return self._send(200, (WEB / filename).read_bytes(), content_type)

    def do_POST(self):
        route = urlparse(self.path).path
        port = self.server.server_port
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self._json(403, {"ok": False, "error": "不允許跨站操作。"})
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"ok": False, "error": "請使用 JSON 格式。"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 16_384:
                raise InputError("請求資料過大。")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise InputError("請求資料格式不正確。")
            dashboard = self.server.dashboard
            if route == "/api/session/reset":
                dashboard.reset_runtime()
                return self._json(200, {"ok": True, "state": dashboard.state()})
            if route == "/api/scan-settings":
                event = dashboard.save_interval(payload.get("check_interval_seconds"))
            elif route == "/api/reservation":
                event = dashboard.save_reservation(payload)
            elif route == "/api/monitor/start":
                event = dashboard.start(confirm_auto_book=payload.get("confirm_auto_book"))
            elif route == "/api/monitor/stop":
                event = dashboard.stop()
            elif route == "/api/scan":
                event = dashboard.scan_once()
            elif route == "/api/book":
                if payload.get("confirm") is not True:
                    raise InputError("送出預約前需要確認。")
                event = dashboard.book_now()
            elif route == "/api/booking/reset":
                event = dashboard.clear_booking_state()
            elif route == "/api/google/login":
                event = dashboard.open_google_login()
            else:
                return self._json(404, {"ok": False, "error": "找不到操作。"})
            status = 409 if event["status"] in ("failure", "error") else 200
            return self._json(status, {"ok": status == 200, "event": event, "state": dashboard.state()})
        except (InputError, json.JSONDecodeError, ValueError) as exc:
            event = self.server.dashboard.emit("操作檢查", "failure", str(exc) or "資料格式不正確。")
            return self._json(400, {"ok": False, "event": event, "state": self.server.dashboard.state()})
        except Exception as exc:
            event = self.server.dashboard.emit("系統", "error", f"操作失敗：{str(exc)[:180]}")
            return self._json(500, {"ok": False, "event": event, "state": self.server.dashboard.state()})

def main():
    parser = argparse.ArgumentParser(description="桃園機場 P4 停車預約控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="啟動後開啟瀏覽器")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    server.dashboard = Dashboard()
    threading.Thread(target=server.dashboard.check_google_login, daemon=True).start()
    print(f"控制台已啟動：http://127.0.0.1:{server.server_port}")
    print("按 Ctrl+C 關閉。")
    if args.open:
        webbrowser.open_new_tab(f"http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.dashboard.thread and server.dashboard.thread.is_alive():
            server.dashboard.stop_event.set()
            server.dashboard.wake_event.set()
            server.dashboard.thread.join(timeout=12)
        server.server_close()


if __name__ == "__main__":
    main()
