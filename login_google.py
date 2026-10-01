# -*- coding: utf-8 -*-
"""
Google 帳號登入與權重維護輔助工具
啟動持久化 Chrome 瀏覽器，讓您登入個人的 Google 帳號。
登入後的狀態 (Cookies、快取) 會保存在本機專屬 Profile，
供預約腳本共用登入狀態；是否需要額外驗證由平台決定，程式無法取得或保證評分。
"""

import os
import sys
from playwright.sync_api import sync_playwright
from booking_safety import file_lock

# 修正 Windows 主控台編碼
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def get_profile_dir():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    path = os.path.join(base, "youparking_chrome_profile")
    os.makedirs(path, exist_ok=True)
    return path

def main():
    profile_dir = get_profile_dir()
    print("=" * 60)
    print("      【YouParking】Google 帳號持久化登入工具")
    print("=" * 60)
    print(f"Profile 目錄: {profile_dir}")
    print("\n即將開啟 Chrome 瀏覽器視窗...")
    print("請在開啟的瀏覽器中登入您的常用 Google 帳號。")
    print("登入成功後，請直接關閉該瀏覽器視窗即可完成設定。\n")

    with file_lock(profile_dir + ".lock"), sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox"
            ],
            locale="zh-TW"
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            window.navigator.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
        """)
        
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://accounts.google.com/")

        print(">> 瀏覽器已開啟。完成登入並關閉瀏覽器後，本程式會自動結束。")
        try:
            page.wait_for_event("close", timeout=600000)
        except Exception:
            pass
        finally:
            try:
                context.close()
            except Exception:
                pass

    print("\n設定已保存！登入狀態將供預約程式共用；不保證免除額外驗證。")

if __name__ == "__main__":
    main()
