import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

from booking_safety import booking_marker_path, execute_booking, file_lock, matching_order
from dashboard import Dashboard, DashboardHandler, InputError, order_result, validate_interval, validate_reservation
from youparking_client import (OrderPreparationError, PlatformRejected,
                               VerificationIncomplete, VerificationRequired, YouParkingClient,
                               wait_for_browser_confirmation)


class FakeClient:
    created = 0
    browser_verification_called = 0
    fail_with_v2 = False
    last_kwargs = {}
    orders = []

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def check_period_availability(self, start, end):
        return {"all_available": True, "details": [{"date": start, "left": 1, "available": True}]}

    def get_order_records(self, _car):
        return list(type(self).orders)

    def _record(self, kwargs):
        type(self).orders.append({
            "Model_ReservedOrderID": 42,
            "Model_CarNo": kwargs["car_no"],
            "Model_SiteCode": type(self).last_kwargs["site_code"],
            "Model_SpaceID": type(self).last_kwargs["space_id"],
            "Model_StartDate": kwargs["start_date"],
            "Model_EndDate": kwargs["end_date"],
        })

    def create_order(self, **kwargs):
        if type(self).fail_with_v2:
            raise VerificationRequired("平台要求完成額外驗證。")
        type(self).created += 1
        self._record(kwargs)
        return {"OrderID": 42}

    def create_order_with_browser_verification(self, **kwargs):
        type(self).browser_verification_called += 1
        self._record(kwargs)
        return {"OrderID": 42, "verification": "recaptcha-v2"}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        tomorrow = date.today() + timedelta(days=1)
        config = {
            "space_id": "1", "site_code": 84416, "car_no": "ABC-1234",
            "contact_name": "測試者", "phone": "0912345678", "check_interval_seconds": 1,
            "start_date": tomorrow.isoformat(), "end_date": (tomorrow + timedelta(days=1)).isoformat(),
            "auto_book": False,
        }
        self.config_path = root / "config.json"
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        self.dashboard = Dashboard(self.config_path, root / "logs" / "events.jsonl", FakeClient)
        FakeClient.created = 0
        FakeClient.browser_verification_called = 0
        FakeClient.fail_with_v2 = False
        FakeClient.last_kwargs = {}
        FakeClient.orders = []

    def tearDown(self):
        self.dashboard.stop_event.set()
        self.dashboard.wake_event.set()
        if self.dashboard.thread:
            self.dashboard.thread.join(timeout=2)
        self.directory.cleanup()

    def test_validations_reject_invalid_interval_and_reversed_dates(self):
        for value in (0, -1, "1.5", 3601, True):
            with self.subTest(value=value), self.assertRaises(InputError):
                validate_interval(value)
        values = self.dashboard.state()["config"]
        values["end_date"] = (date.today() - timedelta(days=1)).isoformat()
        with self.assertRaises(InputError):
            validate_reservation(values)

    def test_manual_scan_never_books_and_saves_interval(self):
        values = self.dashboard.state()["config"]
        values["auto_book"] = True
        self.dashboard.save_reservation(values)
        event = self.dashboard.scan_once()
        self.assertEqual(event["status"], "success")
        self.assertEqual(FakeClient.created, 0)
        self.dashboard.save_interval("17")
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["check_interval_seconds"], 17)

    def test_clear_booking_state_only_removes_current_marker_and_allows_retry(self):
        state_dir = self.config_path.parent / '.booking-state'
        state_dir.mkdir()
        marker = booking_marker_path(self.dashboard.config, state_dir)
        other = booking_marker_path({**self.dashboard.config, 'car_no': 'OTHER-1'}, state_dir)
        marker.write_text('pending\n')
        other.write_text('pending\n')
        original = self.config_path.read_text(encoding='utf-8')
        event = self.dashboard.clear_booking_state()
        self.assertEqual(event['status'], 'success')
        self.assertFalse(marker.exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), original)
        self.assertEqual(self.dashboard.clear_booking_state()['status'], 'success')
        self.assertEqual(self.dashboard.book_now()['status'], 'success')
        self.assertEqual(FakeClient.created, 1)

    def test_clear_booking_state_rejects_active_operations(self):
        state_dir = self.config_path.parent / '.booking-state'
        state_dir.mkdir()
        marker = booking_marker_path(self.dashboard.config, state_dir)
        marker.write_text('pending\n')
        for lock in (self.dashboard.scan_lock, self.dashboard.booking_lock):
            with lock, self.assertRaises(InputError):
                self.dashboard.clear_booking_state()
            self.assertTrue(marker.exists())
        self.dashboard.thread = threading.current_thread()
        try:
            with self.assertRaises(InputError):
                self.dashboard.clear_booking_state()
            self.assertTrue(marker.exists())
        finally:
            self.dashboard.thread = None
        with file_lock(state_dir / 'booking.lock'), self.assertRaises(RuntimeError):
            self.dashboard.clear_booking_state()
        self.assertTrue(marker.exists())
        self.assertEqual(self.dashboard.clear_booking_state()['status'], 'success')

    def test_parking_lot_defaults_to_p4_and_switch_persists_api_ids(self):
        self.assertEqual(self.dashboard.state()["config"]["parking_lot"], "p4")
        values = self.dashboard.state()["config"]
        values["parking_lot"] = "peace"
        self.dashboard.save_reservation(values)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["parking_lot"], "peace")
        self.assertEqual(saved["space_id"], "9")
        self.assertEqual(saved["site_code"], 15272193788)
        self.dashboard._client(self.dashboard.config)
        self.assertEqual(FakeClient.last_kwargs, {"space_id": "9", "site_code": 15272193788})

    def test_new_dashboard_starts_with_empty_execution_results(self):
        self.dashboard.emit("測試", "success", "上一輪結果")
        self.dashboard.last_result = {"all_available": True}
        reopened = Dashboard(self.config_path, self.dashboard.log_path, FakeClient)
        state = reopened.state()
        self.assertEqual(state["events"], [])
        self.assertIsNone(state["monitor"]["last_result"])
        self.assertEqual(self.dashboard.log_path.read_text(encoding="utf-8"), "")

    def test_auto_booking_needs_confirmation_and_attempts_once(self):
        values = self.dashboard.state()["config"]
        values["auto_book"] = True
        self.dashboard.save_reservation(values)
        with self.assertRaises(InputError):
            self.dashboard.start()
        self.dashboard.start(confirm_auto_book=True)
        self.dashboard.thread.join(timeout=2)
        self.assertFalse(self.dashboard.thread.is_alive())
        self.assertEqual(FakeClient.created, 1)
        self.assertTrue(any(e["action"] == "自動預約" and e["status"] == "success" for e in self.dashboard.state()["events"]))

    def test_unconfirmed_response_does_not_claim_booking_success(self):
        status, _message = order_result({"message": "已受理"}, [], [])
        self.assertEqual(status, "info")
        status, _message = order_result({"success": False}, [], [])
        self.assertEqual(status, "failure")

    def test_unrelated_or_incomplete_order_never_confirms_from_record_alone(self):
        config = self.dashboard.config
        row = {
            "Model_ReservedOrderID": 72, "Model_SiteCode": config["site_code"],
            "Model_SpaceID": config["space_id"], "Model_CarNo": config["car_no"],
            "Model_StartDate": config["start_date"], "Model_EndDate": config["end_date"],
        }
        for changed in (
            {"Model_StartDate": "2030-01-01"},
            {"Model_SiteCode": 15272193788},
            {"Model_CarNo": "OTHER-1"},
            {"Model_CarNo": None},
            {"Model_SiteCode": None},
            {"Model_SpaceID": None},
        ):
            with self.subTest(changed=changed):
                other = {**row, **changed}
                self.assertIsNone(matching_order([other], config))
                self.assertEqual(order_result({}, [], [other], config)[0], "info")
        self.assertEqual(order_result({}, [row], [row], config)[0], "info")
        self.assertEqual(order_result({}, [], [row], config)[0], "success")
        status, message = order_result({"OrderID": 73}, [], [row], config)
        self.assertEqual(status, "success")
        self.assertIn("交易紀錄尚未核對", message)

    def test_platform_acknowledgement_is_success_before_record_matches(self):
        config = self.dashboard.config

        class WrongIdClient:
            def __init__(self):
                self.calls = 0

            def get_order_records(self, _car):
                if not self.calls:
                    return []
                return [{
                    "Model_ReservedOrderID": 73, "Model_SiteCode": config["site_code"],
                    "Model_SpaceID": config["space_id"], "Model_CarNo": config["car_no"],
                    "Model_StartDate": config["start_date"],
                    "Model_EndDate": config["end_date"],
                }]

            def create_order(self, **_kwargs):
                self.calls += 1
                return {"OrderID": 72, "success": True}

        state_dir = Path(self.directory.name) / ".booking-state"
        status, message = execute_booking(WrongIdClient(), config, state_dir,
                                          reconcile_delays=(0,))
        self.assertEqual(status, "success")
        self.assertIn("平台回覆預約成功", message)
        self.assertIn("交易紀錄尚未核對", message)
        self.assertNotIn("已核對場站", message)
        self.assertFalse(list(state_dir.glob("*.pending")))

    def test_platform_success_flag_or_order_id_without_record(self):
        for response in ({"success": True}, {"OrderID": 72}):
            with self.subTest(response=response):
                class AcceptedClient:
                    def get_order_records(self, _car):
                        return []

                    def create_order(self, **_kwargs):
                        return response

                state_dir = Path(self.directory.name) / ".booking-state"
                status, message = execute_booking(AcceptedClient(), self.dashboard.config,
                                                  state_dir, reconcile_delays=(0,))
                self.assertEqual(status, "success")
                self.assertIn("平台回覆預約成功", message)
                self.assertFalse(list(state_dir.glob("*.pending")))

    def test_lot_switch_during_scan_blocks_old_lot_submission(self):
        dashboard = self.dashboard

        class SwitchingClient(FakeClient):
            def check_period_availability(self, start, end):
                values = dashboard.state()["config"]
                values["parking_lot"] = "peace"
                dashboard.save_reservation(values)
                return super().check_period_availability(start, end)

        dashboard.client_factory = SwitchingClient
        values = dashboard.state()["config"]
        values["auto_book"] = True
        dashboard.save_reservation(values)
        event = dashboard.scan_once(automatic=True)
        self.assertEqual(event["status"], "info")
        self.assertEqual(FakeClient.created, 0)
        self.assertEqual(dashboard.config["parking_lot"], "peace")

    def test_manual_lot_switch_during_availability_check_blocks_submission(self):
        dashboard = self.dashboard

        class SwitchingClient(FakeClient):
            def check_period_availability(self, start, end):
                values = dashboard.state()["config"]
                values["parking_lot"] = "peace"
                dashboard.save_reservation(values)
                return super().check_period_availability(start, end)

        dashboard.client_factory = SwitchingClient
        event = dashboard.book_now()
        self.assertEqual(event["status"], "failure")
        self.assertEqual(FakeClient.created, 0)

    def test_timeout_reconciles_exact_order_and_blocks_uncertain_retry(self):
        config = self.dashboard.config

        class TimeoutClient:
            def __init__(self, record_after_timeout):
                self.records = []
                self.calls = 0
                self.record_after_timeout = record_after_timeout

            def get_order_records(self, _car):
                return list(self.records)

            def create_order(self, **_kwargs):
                self.calls += 1
                if self.record_after_timeout:
                    self.records.append({
                        "Model_ReservedOrderID": 91,
                        "Model_SiteCode": config["site_code"],
                        "Model_SpaceID": config["space_id"],
                        "Model_CarNo": config["car_no"],
                        "Model_StartDate": config["start_date"],
                        "Model_EndDate": config["end_date"],
                    })
                raise TimeoutError("送單回應逾時")

        state_dir = Path(self.directory.name) / ".booking-state"
        confirmed = TimeoutClient(True)
        status, _ = execute_booking(confirmed, config, state_dir, reconcile_delays=(0,))
        self.assertEqual(status, "success")
        self.assertEqual(confirmed.calls, 1)
        self.assertFalse(list(state_dir.glob("*.pending")))

        uncertain = TimeoutClient(False)
        status, message = execute_booking(uncertain, config, state_dir, reconcile_delays=(0,))
        self.assertEqual(status, "failure")
        self.assertIn("結果未確認", message)
        status, _ = execute_booking(uncertain, config, state_dir, reconcile_delays=(0,))
        self.assertEqual(status, "failure")
        self.assertEqual(uncertain.calls, 1)
        self.assertEqual(len(list(state_dir.glob("*.pending"))), 1)

    def test_existing_exact_booking_is_not_submitted_again(self):
        config = self.dashboard.config
        client = FakeClient(space_id=config["space_id"], site_code=config["site_code"])
        client._record(config)
        status, message = execute_booking(client, config, Path(self.directory.name) / ".booking-state")
        self.assertEqual(status, "success")
        self.assertIn("未重複送出", message)
        self.assertEqual(FakeClient.created, 0)

    def test_real_order_record_shape_confirms_existing_booking_without_post(self):
        config = self.dashboard.config
        record = {
            "Model_ReservedOrderID": 315276,
            "Model_SpaceName": "測試停車場",
            "Model_StartDate": config["start_date"] + "T00:00:00",
            "Model_EndDate": config["end_date"] + "T00:00:00",
        }
        test_case = self

        class ScopedClient(YouParkingClient):
            def __init__(self):
                self.space_id = str(config["space_id"])
                self.site_code = config["site_code"]

            def _get(self, path, params=None):
                if path.endswith("GetReservedSpaceLeft"):
                    return {"ParkingReservedSpace": {
                        "ParkingReservedSpace_ID": config["space_id"],
                        "ParkingReservedSpace_SiteInfoCode": config["site_code"],
                        "ParkingReservedSpace_Name": "測試停車場",
                    }}
                test_case.assertEqual(path, "/api/ParkingOnlyReserved/GetOrderRecord")
                test_case.assertEqual(params, {"Model_CarNo": config["car_no"],
                                               "Model_SiteCode": config["site_code"]})
                return [record]

            def create_order(self, **_kwargs):
                raise AssertionError("已有官方訂單時不得再次送單")

        client = ScopedClient()
        state_dir = Path(self.directory.name) / ".booking-state"
        status, message = execute_booking(client, config, state_dir)
        self.assertEqual(status, "success")
        self.assertIn("315276", message)
        self.assertIn("未重複送出", message)
        self.assertFalse(list(state_dir.glob("*.pending")))

        self.assertIsNone(matching_order([{**record, "Model_SpaceName": "其他停車場"}],
                                         config, scoped_space_name="測試停車場"))
        self.assertIsNone(matching_order([{**record, "Model_StartDate": "2030-01-01"}],
                                         config, scoped_space_name="測試停車場"))
        self.assertIsNone(matching_order([{**record, "Model_CarNo": "OTHER-1"}],
                                         config, scoped_space_name="測試停車場"))
        client.space_id = "other-space"
        self.assertIsNone(client.get_order_record_space_name())

    def test_ambiguous_post_response_reconciles_real_order_record_shape(self):
        config = self.dashboard.config

        class AcceptedClient:
            sent = False

            def get_order_record_space_name(self):
                return "測試停車場"

            def get_order_records(self, _car):
                if not self.sent:
                    return []
                return [{
                    "Model_ReservedOrderID": 91,
                    "Model_SpaceName": "測試停車場",
                    "Model_StartDate": config["start_date"] + "T00:00:00",
                    "Model_EndDate": config["end_date"] + "T00:00:00",
                }]

            def create_order(self, **_kwargs):
                self.sent = True
                return {"message": "已受理"}

        client = AcceptedClient()
        state_dir = Path(self.directory.name) / ".booking-state"
        status, message = execute_booking(client, config, state_dir,
                                          reconcile_delays=(0,))
        self.assertEqual(status, "success")
        self.assertTrue(client.sent)
        self.assertIn("預約訂單 91", message)
        self.assertFalse(list(state_dir.glob("*.pending")))

    def test_explicit_failure_response_never_claims_success(self):
        class RejectedClient:
            calls = 0

            def get_order_records(self, _car):
                return []

            def create_order(self, **_kwargs):
                self.calls += 1
                return {"success": False, "message": "已滿"}

        client = RejectedClient()
        state_dir = Path(self.directory.name) / ".booking-state"
        status, _ = execute_booking(client, self.dashboard.config, state_dir, reconcile_delays=(0,))
        self.assertEqual(status, "failure")
        self.assertEqual(client.calls, 1)
        self.assertFalse(list(state_dir.glob("*.pending")))

    def test_preparation_failure_does_not_leave_false_pending_marker(self):
        class PreparationClient:
            def get_order_records(self, _car):
                return []

            def create_order(self, **_kwargs):
                raise OrderPreparationError("Chrome Profile 正在使用")

        state_dir = Path(self.directory.name) / ".booking-state"
        status, message = execute_booking(PreparationClient(), self.dashboard.config,
                                          state_dir, reconcile_delays=(0,))
        self.assertEqual(status, "error")
        self.assertIn("未送出", message)
        self.assertFalse(list(state_dir.glob("*.pending")))

    def test_profile_lock_rejects_second_holder(self):
        path = Path(self.directory.name) / "profile.lock"
        probe = ("import sys\n"
                 "from booking_safety import file_lock\n"
                 "try:\n"
                 "    with file_lock(sys.argv[1]): print('acquired')\n"
                 "except RuntimeError:\n"
                 "    print('blocked')\n")
        with file_lock(path):
            child = subprocess.run([sys.executable, "-c", probe, str(path)],
                                   cwd=Path(__file__).resolve().parents[1],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(child.stdout.strip(), "blocked")

    def test_browser_observes_late_challenge_and_early_rejection(self):
        class Locator:
            def __init__(self, page, kind):
                self.page = page
                self.kind = kind

            def is_visible(self):
                return (self.page.tick >= 4 if self.kind == "success"
                        else self.page.tick >= 2 if self.kind == "anchor" else False)

            def all_inner_texts(self):
                return ["預約失敗"] if self.page.reject and self.page.tick >= 1 else []

            def click(self):
                self.page.clicks.append(self.kind)

            def get_attribute(self, _name):
                return "true" if self.page.tick >= 3 else "false"

        class Page:
            def __init__(self, reject=False):
                self.tick = 0
                self.reject = reject
                self.clicks = []

            def get_by_text(self, *_args, **_kwargs):
                return Locator(self, "success")

            def frame_locator(self, _selector):
                self.frame_selector = _selector
                return self

            @property
            def first(self):
                return self

            def locator(self, selector):
                return Locator(self, "alert" if "v-snack" in selector else "anchor")

            def get_by_role(self, *_args, **_kwargs):
                return Locator(self, "submit")

            def wait_for_timeout(self, _milliseconds):
                self.tick += 1

        page = Page()
        notices = []
        wait_for_browser_confirmation(page, notify=notices.append, timeout_seconds=2)
        self.assertEqual(page.clicks, ["anchor", "submit"])
        self.assertEqual(page.frame_selector, 'iframe[src*="recaptcha/api2/anchor"]:visible')
        self.assertTrue(any("等待驗證" in notice for notice in notices))
        with self.assertRaises(PlatformRejected):
            wait_for_browser_confirmation(Page(reject=True), notify=lambda _message: None,
                                          timeout_seconds=2)
        pending = Page()
        pending.tick = 2
        pending.wait_for_timeout = lambda _milliseconds: time.sleep(0.03)
        with self.assertRaises(VerificationIncomplete):
            wait_for_browser_confirmation(pending, notify=lambda _message: None,
                                          timeout_seconds=0.02)

    def test_http_save_and_validation_failure_appear_in_state(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        server.dashboard = self.dashboard
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def send(path, data):
            payload = json.dumps(data).encode("utf-8")
            req = request.Request(base + path, data=payload, headers={"Content-Type": "application/json"})
            try:
                with request.urlopen(req, timeout=2) as response:
                    return response.status, json.load(response)
            except error.HTTPError as response:
                try:
                    return response.code, json.load(response)
                finally:
                    response.close()

        try:
            state_dir = self.config_path.parent / '.booking-state'
            state_dir.mkdir(exist_ok=True)
            marker = booking_marker_path(self.dashboard.config, state_dir)
            marker.write_text('pending\n')
            status, body = send("/api/booking/reset", {})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertFalse(marker.exists())
            status, body = send("/api/scan-settings", {"check_interval_seconds": 12})
            self.assertEqual(status, 200)
            self.assertEqual(body["state"]["config"]["check_interval_seconds"], 12)
            values = self.dashboard.state()["config"]
            values["end_date"] = (date.today() - timedelta(days=1)).isoformat()
            status, body = send("/api/reservation", values)
            self.assertEqual(status, 400)
            self.assertEqual(body["event"]["status"], "failure")
            self.assertEqual(body["state"]["config"]["end_date"], self.dashboard.config["end_date"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_session_reset_clears_results_without_changing_config(self):
        self.dashboard.emit("測試", "error", "上一輪錯誤")
        self.dashboard.last_result = {"all_available": False}
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        server.dashboard = self.dashboard
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        payload = json.dumps({}).encode("utf-8")
        req = request.Request(base + "/api/session/reset", data=payload,
                              headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(base + "/api/instance", timeout=2) as response:
                instance = json.load(response)
            self.assertEqual(instance["root"], str(Path(__file__).resolve().parents[1]))
            self.assertIsInstance(instance["pid"], int)
            with request.urlopen(req, timeout=2) as response:
                body = json.load(response)
            self.assertEqual(body["state"]["events"], [])
            self.assertIsNone(body["state"]["monitor"]["last_result"])
            self.assertEqual(body["state"]["config"]["car_no"], "ABC-1234")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_v2_verification_fallback_automatically_calls_browser(self):
        FakeClient.fail_with_v2 = True
        try:
            event = self.dashboard.book_now()
            self.assertEqual(event["status"], "success")
            self.assertEqual(FakeClient.created, 0)
            self.assertEqual(FakeClient.browser_verification_called, 1)
        finally:
            FakeClient.fail_with_v2 = False


if __name__ == "__main__":
    unittest.main()
