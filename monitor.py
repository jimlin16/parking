# -*- coding: utf-8 -*-
"""
桃園機場 P4 停車場「車位監控與自動預約」腳本
可全天候高頻監控指定日期區間，一旦有人退訂釋出車位，即可發出提示或自動秒殺預約。
"""

import os
import sys
import time
import json
import argparse
from datetime import datetime
from pathlib import Path
from booking_safety import execute_booking
from dashboard import PARKING_LOTS, parking_lot_key
from youparking_client import YouParkingClient

# 修正 Windows 主控台編碼
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def main():
    config = load_config()
    lot = PARKING_LOTS[parking_lot_key(config)]
    
    parser = argparse.ArgumentParser(description="停車場車位監控與自動預約程式")
    parser.add_argument("--start", type=str, default=config.get("start_date", "2026-10-11"), help="預約起始日 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=config.get("end_date", "2026-10-13"), help="預約結束日 (YYYY-MM-DD)")
    parser.add_argument("--car", type=str, default=config.get("car_no", "BLH-8667"), help="車牌號碼")
    parser.add_argument("--contact", type=str, default=config.get("contact_name", "林俊豪"), help="聯絡人姓名")
    parser.add_argument("--phone", type=str, default=config.get("phone", None), help="手機號碼")
    parser.add_argument("--auto-book", action="store_true", help="若有車位是否立即自動下單預約")
    parser.add_argument("--interval", type=int, default=config.get("check_interval_seconds", 10), help="查詢輪詢間隔 (秒)")
    args = parser.parse_args()

    client = YouParkingClient(
        space_id=lot["space_id"],
        site_code=lot["site_code"],
    )

    print("==================================================")
    print(f"      {lot['label']} 車位監控系統啟動")
    print("==================================================")
    print(f"監控區間 : {args.start} 至 {args.end}")
    print(f"車牌號碼 : {args.car}")
    print(f"聯絡人   : {args.contact}")
    print(f"自動預約 : {'【已開啟】釋出空位時立即秒殺下單' if args.auto_book else '【關閉】僅監控顯示'}")
    print(f"輪詢間隔 : {args.interval} 秒")
    print("==================================================")
    print("正在持續監控中... (按 Ctrl+C 可停止)\n")

    check_count = 0
    try:
        while True:
            check_count += 1
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                res = client.check_period_availability(args.start, args.end)
                details = res["details"]
                summary_str = " | ".join([f"{d['date']}: {d['left'] if d['left'] is not None else '?'}" for d in details])
                
                if res["all_available"]:
                    print(f"\n[{now_str}] 🎯【發現空位！】{summary_str}")
                    # Windows 發出提示音
                    try:
                        import winsound
                        winsound.Beep(1000, 800)
                    except Exception:
                        pass
                    
                    if args.auto_book:
                        print(">>> 正在自動進行背景 Token 驗證並建立預約訂單...")
                        booking = {**config, 'start_date': args.start, 'end_date': args.end,
                                   'car_no': args.car, 'contact_name': args.contact,
                                   'phone': args.phone, 'space_id': client.space_id,
                                   'site_code': client.site_code}
                        try:
                            status, message = execute_booking(
                                client, booking, Path(__file__).parent / '.booking-state', notify=print)
                            print(f"[{status}] {message}")
                        except Exception as exc:
                            print(f"預約操作失敗：{exc}")
                        # Never repeat a write merely because the response was lost.
                        break
                    else:
                        print("提示: 目前未開啟 --auto-book，請儘速手動前往網站預約！")
                else:
                    sys.stdout.write(f"\r[{now_str}] 第 {check_count} 次檢查 | {summary_str} (目前入場日尚無空位)")
                    sys.stdout.flush()

            except Exception as e:
                print(f"\n[{now_str}] 查詢出錯: {e}")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\n監控程式已由使用者手動終止。")

if __name__ == "__main__":
    main()
