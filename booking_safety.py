"""Order reconciliation and cross-process locks; never blindly retry a write."""
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('同一份瀏覽器資料或預約正在由其他程式使用，請先結束該操作。') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def matching_order(records, config, expected_id=None, exclude_ids=(), scoped_space_name=None):
    if not isinstance(records, list):
        return None
    for row in records:
        if not isinstance(row, dict) or not row.get('Model_ReservedOrderID'):
            continue
        order_id = str(row['Model_ReservedOrderID'])
        if order_id in exclude_ids or (expected_id is not None and order_id != str(expected_id)):
            continue
        # GetOrderRecord filters by plate/site, but its real rows omit those
        # fields and the space ID. Only use that query scope when official
        # metadata also confirms the configured space name and IDs.
        car = row.get('Model_CarNo', row.get('Model_CarShow'))
        if car and str(car).strip().upper() != config['car_no'].strip().upper():
            continue
        site = row.get('Model_SiteCode')
        if site is not None and str(site) != str(config['site_code']):
            continue
        space = row.get('Model_SpaceID')
        if space is not None and str(space) != str(config['space_id']):
            continue
        if (not car or site is None or space is None) and (
                not scoped_space_name or
                str(row.get('Model_SpaceName', '')).strip() != scoped_space_name):
            continue
        if all(str(row.get(field, '')).split('T')[0].split(' ')[0] == config[key]
               for field, key in [('Model_StartDate', 'start_date'), ('Model_EndDate', 'end_date')]):
            return row
    return None


def rejected_response(response):
    """Return a visible platform rejection, without treating mere receipt as success."""
    if isinstance(response, dict):
        message = next((response[k] for k in ('message', 'Message', 'msg', 'Msg')
                        if isinstance(response.get(k), str)), '')
        if any(response.get(k) is False for k in ('success', 'Success', 'isSuccess', 'IsSuccess')):
            return message or '平台回覆預約失敗'
    elif isinstance(response, str):
        message = response
    else:
        return None
    if any(word in message for word in ('失敗', '錯誤', '已滿', '額滿', '拒絕')):
        return message
    return None


def response_order_id(response):
    if not isinstance(response, dict):
        return None
    return next((response[k] for k in ('OrderID', 'OrderId', 'orderId', 'order_id')
                 if response.get(k)), None)


def platform_success_message(response):
    """Restore the platform's explicit success acknowledgement as a success result."""
    if not isinstance(response, dict) or rejected_response(response):
        return None
    order_id = response_order_id(response)
    accepted = any(response.get(key) is True for key in
                   ('success', 'Success', 'isSuccess', 'IsSuccess'))
    if not accepted and order_id is None:
        return None
    order_detail = f'，訂單編號 {order_id}' if order_id is not None else ''
    return f'平台回覆預約成功{order_detail}；交易紀錄尚未核對，請稍後於官網確認。'


def booking_marker_path(config, state_dir):
    identity = [str(config[k]).upper() for k in
                ('site_code', 'space_id', 'car_no', 'start_date', 'end_date')]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return Path(state_dir) / (key + '.pending')


def execute_booking(client, config, state_dir, notify=lambda message: None,
                    cancelled=lambda: False, reconcile_delays=(0, 1, 2)):
    """One attempt, shared by dashboard/CLI. A durable marker survives timeout/crash."""
    from youparking_client import (OrderPreparationError, PlatformRejected,
                                   VerificationIncomplete, VerificationRequired)
    state_dir = Path(state_dir)
    marker = booking_marker_path(config, state_dir)
    with file_lock(state_dir / 'booking.lock'):
        scope_lookup = getattr(client, 'get_order_record_space_name', None)
        try:
            scoped_space_name = scope_lookup() if callable(scope_lookup) else None
        except Exception:
            scoped_space_name = None
        try:
            before = client.get_order_records(config['car_no'])
            if not isinstance(before, list):
                raise ValueError('訂單查詢格式不正確')
        except Exception as exc:
            return 'error', f'送單前無法核對既有訂單，未送出：{exc}'
        existing = matching_order(before, config, scoped_space_name=scoped_space_name)
        if existing:
            marker.unlink(missing_ok=True)
            return 'success', f'已確認相同預約訂單 {existing["Model_ReservedOrderID"]}，未重複送出。'
        if marker.exists():
            return 'failure', f'上次送單結果未確認，已阻止重複送出；請核對官方交易紀錄。防重複檔：{marker.name}'
        if cancelled():
            return 'info', '已停止，未送出預約。'
        before_ids = {str(row['Model_ReservedOrderID']) for row in before
                      if isinstance(row, dict) and row.get('Model_ReservedOrderID')}
        # No personal information in the marker; existence means outcome unknown.
        with marker.open('w', encoding='utf-8') as handle:
            handle.write('pending\n')
            handle.flush()
            os.fsync(handle.fileno())
        response = None
        error = None
        try:
            try:
                response = client.create_order(
                    start_date=config['start_date'], end_date=config['end_date'],
                    car_no=config['car_no'], contact_name=config['contact_name'],
                    phone=config.get('phone') or None, inv_type=config.get('inv_type', '紙本'),
                    vehicle_code=config.get('vehicle_code') or None,
                    buyer_num=config.get('buyer_num') or None)
            except OrderPreparationError as exc:
                marker.unlink(missing_ok=True)
                return 'error', f'送單前準備失敗，未送出預約：{str(exc)[:120]}'
            except VerificationRequired as exc:
                if cancelled():
                    error = exc
                    notify('已停止後續瀏覽器驗證；正在查詢 API 送單結果。')
                else:
                    notify('平台要求額外驗證，正在開啟官方頁面；是否通過以平台結果為準。')
                    response = client.create_order_with_browser_verification(
                        start_date=config['start_date'], end_date=config['end_date'],
                        car_no=config['car_no'], contact_name=config['contact_name'],
                        phone=config.get('phone') or None, notify=notify)
        except Exception as exc:
            error = exc
        for delay in reconcile_delays:
            if delay:
                time.sleep(delay)
            try:
                row = matching_order(client.get_order_records(config['car_no']), config,
                                     expected_id=response_order_id(response),
                                     exclude_ids=before_ids,
                                     scoped_space_name=scoped_space_name)
            except Exception:
                row = None
            if row:
                marker.unlink(missing_ok=True)
                return 'success', f'已核對場站與日期，預約訂單 {row["Model_ReservedOrderID"]}。'
        rejection = rejected_response(response)
        if rejection:
            marker.unlink(missing_ok=True)
            return 'failure', f'平台拒絕預約：{rejection[:100]}；未查到符合資料的訂單。'
        if isinstance(error, PlatformRejected):
            marker.unlink(missing_ok=True)
            return 'failure', f'{str(error)[:120]}；未查到符合資料的訂單。'
        platform_message = platform_success_message(response)
        if platform_message:
            marker.unlink(missing_ok=True)
            return 'success', platform_message
        if isinstance(error, VerificationIncomplete):
            return 'failure', f'驗證未完成；送單結果未確認，已阻止重複送出。防重複檔：{marker.name}'
        detail = f'（{str(error)[:100]}）' if error else ''
        return 'failure', f'送單結果未確認，已阻止重複送出。防重複檔：{marker.name}。' + detail
