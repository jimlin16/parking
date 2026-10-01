# -*- coding: utf-8 -*-
"""
桃園國際機場 P4 停車場預約 (youparking.com.tw) 純程式 API 自動化客戶端
支援：
1. 即時車位查詢與多日區間可用性檢測
2. 訂單查詢與線上退訂
3. 背景無頭自動取得 Google reCAPTCHA v3 Token
4. 全自動一鍵預約下單 (免開瀏覽器)
"""

import os
from booking_safety import file_lock
import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta

# 修正 Windows 主控台編碼
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

BASE_URL = "https://pcc.youparking.com.tw"
RECAPTCHA_SITE_KEY = "6Ld6lVIsAAAAACXJH7xj0G46Ygh-zDI9lQz7s6qO"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/parkingreserve/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});
window.navigator.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};
"""


def get_default_profile_dir():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    path = os.path.join(base, "youparking_chrome_profile")
    os.makedirs(path, exist_ok=True)
    return path


class VerificationRequired(RuntimeError):
    """平台接受請求格式，但要求使用者完成 reCAPTCHA v2。"""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class VerificationIncomplete(RuntimeError):
    """The browser still required human verification at the deadline."""


class PlatformRejected(RuntimeError):
    """The official page visibly rejected the reservation."""


class OrderPreparationError(RuntimeError):
    """The API write has not been attempted yet."""


def wait_for_browser_confirmation(page, notify=print, timeout_seconds=300):
    """Observe late challenges, success, and visible rejection after submit."""
    success = page.get_by_text("您已完成線上登記", exact=False)
    # 官方頁面可能同時保留隱藏與顯示中的驗證框。
    anchor = page.frame_locator('iframe[src*="recaptcha/api2/anchor"]:visible').first.locator('#recaptcha-anchor')
    alerts = page.locator('.v-snack__content:visible, .v-alert:visible, [role="alert"]:visible')
    deadline = time.monotonic() + timeout_seconds
    verification_seen = False
    checkbox_clicked = False
    resubmitted = False
    manual_notice_sent = False
    checkbox_deadline = None
    notify("等待官方確認；若出現圖形驗證，請在瀏覽器中完成。")
    while time.monotonic() < deadline:
        if success.is_visible():
            return
        for message in alerts.all_inner_texts():
            if any(word in message for word in ("失敗", "錯誤", "已滿", "額滿", "拒絕")):
                raise PlatformRejected(f"平台拒絕預約：{message[:120]}")
        if anchor.is_visible():
            if not verification_seen:
                verification_seen = True
                notify("等待驗證：請留意瀏覽器中的 reCAPTCHA。")
            if not checkbox_clicked:
                anchor.click()
                checkbox_clicked = True
                checkbox_deadline = time.monotonic() + 30
            if anchor.get_attribute("aria-checked") == "true" and not resubmitted:
                if success.is_visible():
                    return
                page.get_by_role("button", name="送出", exact=True).click()
                resubmitted = True
                notify("驗證已完成，等待平台確認訂單。")
            elif (checkbox_clicked and not manual_notice_sent
                  and time.monotonic() >= checkbox_deadline):
                manual_notice_sent = True
                notify("驗證尚未完成，請在瀏覽器中完成圖形驗證。")
        page.wait_for_timeout(500)
    if verification_seen and not resubmitted:
        raise VerificationIncomplete("驗證未完成；送單結果仍須查詢官方訂單。")
    raise RuntimeError("官方頁面未確認送單結果；請核對官方訂單。")


class YouParkingClient:
    def __init__(self, space_id="1", site_code=84416, profile_dir=None):
        """
        :param space_id: 停車場代號 (1: 第二航廈 P4 停車場)
        :param site_code: 停車場站點代碼 (84416)
        :param profile_dir: 持久化 Chrome Profile 資料夾路徑
        """
        self.space_id = str(space_id)
        self.site_code = site_code
        self.headers = DEFAULT_HEADERS.copy()
        self.profile_dir = os.path.abspath(profile_dir) if profile_dir else get_default_profile_dir()
        os.makedirs(self.profile_dir, exist_ok=True)

    def _get(self, path, params=None):
        url = f"{BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {err_msg}")

    def _post(self, path, payload):
        url = f"{BASE_URL}{path}"
        headers = self.headers.copy()
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="replace")
            if e.code == 428:
                try:
                    details = json.loads(err_msg)
                except (TypeError, ValueError):
                    details = {}
                raise VerificationRequired(
                    details.get("Message") or "平台要求完成額外驗證。",
                    details,
                ) from e
            raise RuntimeError(f"HTTP {e.code}: {err_msg}")

    def get_space_info(self):
        """取得停車場規範、行事曆與假日加價規則"""
        return self._get("/api/ParkingOnlyReserved/GetReservedSpace", {"LinkID": self.space_id})

    def get_space_left(self):
        """
        查詢各日期的剩餘車位列表 (純 HTTP，不需驗證碼，毫秒回傳)
        :return: { "2026-10-11": 5, "2026-10-12": 0, ... }
        """
        data = self._get_reserved_space_left()
        left_list = data.get("List_ParkingReservedLeft", [])
        
        result = {}
        for item in left_list:
            dt_str = item["Model_Date"].split("T")[0]
            result[dt_str] = item["Model_Left"]
        return result

    def _get_reserved_space_left(self):
        """取得車位、日曆與計價資料；預約送單需要同一份平台資料。"""
        return self._get(
            "/api/ParkingOnlyReserved/GetReservedSpaceLeft",
            {"SpaceID": self.space_id},
        )

    @staticmethod
    def _date_only(value):
        return str(value).split("T", 1)[0]

    def _calculate_order_amount(self, start_date, end_date):
        """依平台回傳的工作日、週末與假日規則計算送單金額。"""
        data = self._get_reserved_space_left()
        parts = data.get("ListParkingReservedSpacePart", [])
        site_calendar = data.get("List_ParkingReservedCalendar", [])
        system_calendar = data.get("List_Calendar", [])
        if not parts:
            raise RuntimeError("平台未回傳計價規則，為避免金額錯誤而停止送單。")

        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        total = 0.0
        current = start
        while current <= end:
            day = current.strftime("%Y-%m-%d")
            calendar_type = None
            for item in site_calendar:
                if self._date_only(item.get("ParkingReservedCalendar_Date")) == day:
                    calendar_type = item.get("ParkingReservedCalendar_Type")
                    break
            if calendar_type is None:
                for item in system_calendar:
                    if self._date_only(item.get("Calendar_Date")) == day:
                        calendar_type = item.get("Calendar_Type")
                        break
            if calendar_type is None:
                # Python weekday: Monday=0, Sunday=6; platform weekend type is 2.
                calendar_type = 2 if current.weekday() in (5, 6) else 1

            eligible_parts = [
                item for item in parts
                if self._date_only(item.get("ParkingReservedSpacePart_Date")) <= day
            ]
            if not eligible_parts:
                raise RuntimeError(f"平台未回傳 {day} 適用的計價規則，為避免金額錯誤而停止送單。")
            part = max(
                eligible_parts,
                key=lambda item: self._date_only(item.get("ParkingReservedSpacePart_Date")),
            )
            amount_key = {
                1: "ParkingReservedSpacePart_WorkDayAmount",
                2: "ParkingReservedSpacePart_WeekendAmount",
                3: "ParkingReservedSpacePart_HolidayAmount",
            }.get(calendar_type)
            if not amount_key or part.get(amount_key) is None:
                raise RuntimeError(f"平台未回傳 {day} 的有效計價金額，為避免金額錯誤而停止送單。")
            total += float(part[amount_key])
            current += timedelta(days=1)

        return int(total) if total.is_integer() else total

    def check_period_availability(self, start_date_str, end_date_str):
        """
        依入場日期判斷是否可預約；區間內其他日期僅供顯示。
        :param start_date_str: "2026-10-11"
        :param end_date_str: "2026-10-13"
        :return: { "all_available": True/False, "details": [...] }
        """
        availability = self.get_space_left()
        start = datetime.strptime(start_date_str, "%Y-%m-%d")
        end = datetime.strptime(end_date_str, "%Y-%m-%d")
        if end < start:
            raise ValueError("離場日期不能早於入場日期")
        
        cur = start
        daily_status = []
        # 保留既有回傳欄位，供手動預約與自動監控共用入場日判斷。
        is_all_available = availability.get(start_date_str) is not None and availability[start_date_str] > 0
        
        while cur <= end:
            d_str = cur.strftime("%Y-%m-%d")
            left = availability.get(d_str, None)
            available = (left is not None and left > 0)
            daily_status.append({"date": d_str, "left": left, "available": available})
            cur += timedelta(days=1)
            
        return {
            "all_available": is_all_available,
            "details": daily_status
        }

    def get_order_records(self, car_no):
        """查詢指定車牌的所有有效預約訂單紀錄 (純 HTTP)"""
        params = {
            "Model_CarNo": car_no.upper(),
            "Model_SiteCode": self.site_code
        }
        return self._get("/api/ParkingOnlyReserved/GetOrderRecord", params)

    def get_order_record_space_name(self):
        """Return the official space name only when its IDs match this client."""
        data = self._get_reserved_space_left()
        space = data.get("ParkingReservedSpace") if isinstance(data, dict) else None
        if not isinstance(space, dict):
            return None
        if (str(space.get("ParkingReservedSpace_ID")) != self.space_id or
                str(space.get("ParkingReservedSpace_SiteInfoCode")) != str(self.site_code)):
            return None
        name = space.get("ParkingReservedSpace_Name")
        return str(name).strip() if name else None

    def cancel_order(self, order_id, car_no):
        """取消指定預約訂單 (純 HTTP)"""
        params = {
            "OrderID": order_id,
            "Model_CarNo": car_no.upper()
        }
        return self._get("/api/ParkingOnlyReserved/OrderCancel", params)

    def fetch_recaptcha_token(self):
        """
        透過 Playwright 於背景啟動持久化 Chrome 取得合法 reCAPTCHA v3 Token (耗時約 3~8 秒)
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("未安裝 playwright 套件，請先執行: pip install playwright")

        with file_lock(self.profile_dir + ".lock"), sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=self.profile_dir,
                channel="chrome",
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox"
                ],
                user_agent=self.headers["User-Agent"],
                locale="zh-TW"
            )
            try:
                context.add_init_script(STEALTH_SCRIPT)
                page = context.pages[0] if context.pages else context.new_page()

                # 載入預約頁面
                page.goto(f"{BASE_URL}/parkingreserve/", wait_until="networkidle")

                # 注入 Google reCAPTCHA 腳本
                page.evaluate(f"""
                    const s = document.createElement('script');
                    s.src = 'https://www.google.com/recaptcha/api.js?render={RECAPTCHA_SITE_KEY}';
                    document.head.appendChild(s);
                """)

                # 等待 grecaptcha 物件就緒
                page.wait_for_function(
                    "typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha.execute === 'function'",
                    timeout=15000
                )

                # 產生 Token
                token = page.evaluate(f"""
                    window.grecaptcha.execute('{RECAPTCHA_SITE_KEY}', {{ action: 'reserve' }})
                """)
                return token
            finally:
                context.close()

    def create_order(self, start_date, end_date, car_no, contact_name, 
                     phone=None, inv_type="紙本", vehicle_code=None, buyer_num=None,
                     order_amount=None, recaptcha_token=None):
        """
        建立預約訂單 (API 直送)
        :param recaptcha_token: Google reCAPTCHA v3 產生的 Token。若為 None，將自動於背景啟動無頭 Chrome 獲取。
        """
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            reserved_days = (end - start).days + 1
            if order_amount is None:
                order_amount = self._calculate_order_amount(start_date, end_date)
            # 僅在預約資料準備完成後取得 Token；取得失敗最多重試一次。
            if not recaptcha_token:
                for attempt in range(2):
                    try:
                        recaptcha_token = self.fetch_recaptcha_token()
                        if not isinstance(recaptcha_token, str) or not recaptcha_token.strip():
                            raise RuntimeError("未取得有效的 reCAPTCHA Token")
                        break
                    except Exception:
                        if attempt == 1:
                            raise
        except Exception as exc:
            raise OrderPreparationError(str(exc)) from exc

        payload = {
            "Model_SpaceID": self.space_id,
            "Model_StartDate": start_date,
            "Model_EndDate": end_date,
            "Model_ReservedDay": reserved_days,
            "Model_CarShow": car_no.upper(),
            "Model_Contact": contact_name,
            "Model_Phone": phone,
            "invType": inv_type,
            "Model_VehicleCode": vehicle_code,
            "Model_BuyerNum": buyer_num,
            "Model_OrderAmount": order_amount,
            "ReCAPTCHAToken": recaptcha_token,
            "ReCAPTCHAV2Token": None
        }

        return self._post("/api/ParkingOnlyReserved/CreateOrder", payload)

    def create_order_with_browser_verification(
            self, start_date, end_date, car_no, contact_name, phone=None,
            notify=print):
        """開啟官方預約頁完成免費 v2 驗證，再由官方頁面送出預約。

        reCAPTCHA v2 需要真人完成勾選或圖形驗證，無法由背景 API 合法自動繞過。
        此 fallback 會自動填入日期與聯絡資料，保留官方頁面讓使用者完成驗證與送出。
        """
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("未安裝 playwright 套件，請先執行: pip install -r requirements.txt") from exc

        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        if end < start:
            raise ValueError("離場日期不能早於入場日期")
        reserved_days = (end - start).days + 1

        # reservedlist 需要先從 reservedindex 同意規則；由車位 API 回傳資料找出上層 LinkID。
        space_data = self._get_reserved_space_left()
        site = space_data.get("ParkingReserved") or {}
        space = space_data.get("ParkingReservedSpace") or {}
        link_id = site.get("ParkingReserved_ID") or space.get("ParkingReservedSpace_LinkID") or self.space_id
        space_name = space.get("ParkingReservedSpace_Name")

        with file_lock(self.profile_dir + ".lock"), sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=self.profile_dir,
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                user_agent=self.headers["User-Agent"],
                locale="zh-TW",
            )
            try:
                context.add_init_script(STEALTH_SCRIPT)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(
                    f"{BASE_URL}/parkingreserve/#/reservedindex/{link_id}",
                    wait_until="domcontentloaded",
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=30_000)
                except PlaywrightTimeoutError:
                    pass

                consent = page.get_by_role("checkbox")
                if consent.count() and not consent.first.is_checked():
                    # Vuetify 的 ripple layer 會攔截原生 input，force 只作用於本地頁面勾選。
                    consent.first.check(force=True)

                entry_buttons = page.get_by_role("button", name="前往登記", exact=True)
                if not entry_buttons.count():
                    raise RuntimeError("官方頁面找不到「前往登記」按鈕。")
                if entry_buttons.count() > 1 and space_name:
                    card = page.locator(".v-card").filter(has_text=space_name).first
                    scoped_button = card.get_by_role("button", name="前往登記", exact=True)
                    (scoped_button if scoped_button.count() else entry_buttons.first).click()
                else:
                    entry_buttons.first.click()
                page.wait_for_url(
                    f"**/parkingreserve/#/reservedlist/{self.space_id}",
                    timeout=30_000,
                )

                # reservedlist 會在 URL 切換後再非同步載入日期表格；不能在
                # wait_for_url 返回後立刻查詢，否則會誤判成找不到可登記日期。
                try:
                    page.wait_for_load_state("networkidle", timeout=30_000)
                except PlaywrightTimeoutError:
                    # reCAPTCHA 或其他長連線不應阻止我們繼續等待指定日期列。
                    pass
                row = page.locator("tr").filter(has_text=start_date).first
                try:
                    row.wait_for(state="visible", timeout=60_000)
                except PlaywrightTimeoutError as exc:
                    raise RuntimeError(f"官方頁面載入後找不到 {start_date} 的日期列。") from exc
                register_button = row.get_by_role("button", name="登記", exact=True)
                try:
                    register_button.wait_for(state="visible", timeout=10_000)
                except PlaywrightTimeoutError as exc:
                    row_text = " ".join(row.inner_text().split())
                    raise RuntimeError(
                        f"官方頁面的 {start_date} 目前不可登記（{row_text[:120]}）。"
                    ) from exc
                register_button.click()

                page.get_by_label("停放天數", exact=True).fill(str(reserved_days))
                page.get_by_label("姓名", exact=True).fill(contact_name)
                page.get_by_label("車牌號碼 (例: AA-1234)", exact=True).fill(car_no.upper())
                if phone:
                    phone_field = page.get_by_label("電話", exact=False)
                    if phone_field.count():
                        phone_field.first.fill(phone)

                print("正在送出預約登記...")
                page.get_by_role("button", name="送出", exact=True).click()

                wait_for_browser_confirmation(page, notify=notify)

                # The shared booking service reconciles exact official records.
                order_id = None
                return {
                    "success": True,
                    "OrderID": order_id,
                    "message": "官方頁面已完成線上登記。",
                    "verification": "recaptcha-v2",
                }
            finally:
                context.close()


if __name__ == "__main__":
    client = YouParkingClient()
    
    print("=" * 45)
    print("【1. 查詢即時剩餘車位 (前 7 天)】")
    availability = client.get_space_left()
    for d, left in list(availability.items())[:7]:
        status_text = f"剩餘 {left} 個車位" if left > 0 else "已客滿 (0)"
        print(f"  {d} : {status_text}")
        
    print("\n" + "=" * 45)
    print("【2. 查詢指定區間是否有車位 (2026-10-11 ~ 2026-10-13)】")
    res = client.check_period_availability("2026-10-11", "2026-10-13")
    print(f"  入場日可預約: {res['all_available']}")
    for d in res["details"]:
        print(f"  - {d['date']}: 剩餘 {d['left']}")

    print("\n" + "=" * 45)
    print("【3. 車牌訂單查詢 (BLH-8667)】")
    orders = client.get_order_records("BLH-8667")
    print(f"  目前有效訂單數: {len(orders)}")
