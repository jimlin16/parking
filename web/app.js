const $ = (id) => document.getElementById(id);
const state = { data: null, loaded: false, intervalDirty: false, reservationDirty: false, lastSeenId: 0, filter: 'all', busy: false, connectionLost: false };
const statusNames = { success: '成功', failure: '失敗', error: '錯誤', info: '資訊' };

function localTime(value, options = { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) {
  if (!value) return '—';
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? '—' : new Intl.DateTimeFormat('zh-TW', options).format(time);
}

function showToast(event) {
  if (!event || (event.status === 'info' && event.action === '車位掃描')) return;
  const toast = document.createElement('div');
  toast.className = `toast ${event.status}`;
  const title = document.createElement('strong');
  title.textContent = `${event.action} · ${statusNames[event.status] || '通知'}`;
  const text = document.createElement('p');
  text.textContent = event.message;
  toast.append(title, text);
  $('toast-stack').prepend(toast);
  setTimeout(() => toast.remove(), 6500);
}

function renderParkingLots(options, selected) {
  const select = $('parking-lot');
  if (!select || !Array.isArray(options)) return;
  const previous = select.value;
  select.replaceChildren();
  options.forEach(lot => {
    const option = document.createElement('option');
    option.value = lot.key;
    option.textContent = lot.label;
    select.append(option);
  });
  const preferred = selected || previous;
  if (options.some(lot => lot.key === preferred)) select.value = preferred;
}

function applyState(next, announce = false) {
  if (!next) return;
  if (state.loaded && announce) {
    [...next.events].filter(event => event.id > state.lastSeenId).reverse().forEach(showToast);
  }
  state.lastSeenId = Math.max(state.lastSeenId, ...next.events.map(event => event.id), 0);
  state.data = next;
  if (!state.loaded || !state.intervalDirty) $('interval').value = next.config.check_interval_seconds;
  if (!state.loaded || !state.reservationDirty) {
    renderParkingLots(next.parking_lots, next.config.parking_lot);
    $('start-date').value = next.config.start_date || '';
    $('end-date').value = next.config.end_date || '';
    $('contact-name').value = next.config.contact_name || '';
    $('phone').value = next.config.phone || '';
    $('car-no').value = next.config.car_no || '';
    $('auto-book').checked = !!next.config.auto_book;
  }
  state.loaded = true;
  render();
}

function render() {
  if (!state.data) return;
  const { config, monitor, events } = state.data;
  const running = monitor.running && !monitor.stopping;
  const statusText = state.connectionLost ? '連線中斷' : monitor.stopping ? '停止中' : monitor.phase === 'booking' ? '送出預約中' : monitor.phase === 'scanning' ? '掃描中' : running ? '監控中' : '已停止';
  $('monitor-status').textContent = statusText;
  $('status-pill').textContent = state.connectionLost ? '請檢查程式' : monitor.stopping ? '即將停止' : running ? config.auto_book ? '自動預約開啟' : '運作正常' : '未啟動';
  $('status-pill').className = `status-pill ${state.connectionLost ? 'error' : running ? '' : 'off'}`;
  $('status-orb').className = `status-orb ${state.connectionLost ? 'error' : running ? '' : 'off'}`;
  $('monitor-toggle').textContent = monitor.running ? '停止監控' : '啟動監控';
  $('monitor-toggle').disabled = state.busy || state.connectionLost || monitor.stopping;
  $('scan-now').disabled = state.busy || state.connectionLost || monitor.phase === 'scanning' || monitor.phase === 'booking';
  $('book-now').disabled = state.busy || state.connectionLost || monitor.running || monitor.phase === 'booking';
  $('clear-booking-state').disabled = state.busy || state.connectionLost || monitor.running || monitor.phase === 'scanning' || monitor.phase === 'booking';
  $('auto-book').disabled = monitor.running;
  $('interval-applied').textContent = `目前間隔 ${config.check_interval_seconds} 秒${state.intervalDirty ? ' · 尚未儲存修改' : ' · 設定已套用'}`;
  updateCountdown();
  renderAvailability(monitor);
  renderNotifications(events);
  renderLogs(events);
}

function updateCountdown() {
  const monitor = state.data?.monitor;
  if (state.connectionLost) {
    $('next-scan').textContent = '—';
    $('countdown').textContent = '無法取得';
    return;
  }
  if (!monitor?.running || !monitor.next_scan_at || monitor.stopping) {
    $('next-scan').textContent = '—';
    $('countdown').textContent = monitor?.phase === 'scanning' ? '正在查詢' : '尚未啟動';
    return;
  }
  $('next-scan').textContent = localTime(monitor.next_scan_at);
  const seconds = Math.max(0, Math.ceil((new Date(monitor.next_scan_at) - Date.now()) / 1000));
  $('countdown').textContent = `倒數 ${seconds} 秒`;
}

function renderAvailability(monitor) {
  const list = $('availability-list');
  list.replaceChildren();
  $('last-scan-time').textContent = monitor.last_scan_at ? `掃描時間 ${localTime(monitor.last_scan_at)}` : '尚未掃描';
  const result = monitor.last_result;
  const tag = $('availability-status');
  if (!result?.details?.length) {
    tag.textContent = '等待查詢';
    tag.className = 'result-tag neutral';
    const message = document.createElement('p');
    message.className = 'empty-copy';
    message.textContent = '啟動監控或點選「立即掃描」，即可查看每日剩餘車位。';
    list.append(message);
    return;
  }
  tag.textContent = result.all_available ? '入場日可預約' : '入場日尚無空位';
  tag.className = `result-tag ${result.all_available ? 'success' : 'failure'}`;
  result.details.forEach(day => {
    const card = document.createElement('div');
    card.className = `availability-day ${day.available ? 'available' : 'unavailable'}`;
    const label = document.createElement('span');
    label.textContent = day.date;
    const amount = document.createElement('strong');
    amount.textContent = day.left == null ? '未提供資料' : day.left > 0 ? `剩餘 ${day.left} 位` : '已滿';
    card.append(label, amount);
    list.append(card);
  });
}

function renderNotifications(events) {
  const notices = events.filter(event => ['success', 'failure', 'error'].includes(event.status)).slice(0, 3);
  $('notice-count').textContent = notices.length;
  const container = $('notifications');
  container.replaceChildren();
  if (!notices.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-copy';
    empty.textContent = '尚無操作通知。';
    container.append(empty);
    return;
  }
  notices.forEach(event => {
    const notice = document.createElement('div');
    notice.className = `notice ${event.status}`;
    const mark = document.createElement('span');
    mark.className = 'notice-mark';
    mark.textContent = event.status === 'success' ? '✓' : event.status === 'failure' ? '!' : '×';
    const detail = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = `${event.action}${statusNames[event.status]}`;
    const message = document.createElement('p');
    message.textContent = event.message;
    const time = document.createElement('time');
    time.textContent = localTime(event.time);
    detail.append(title, message, time);
    notice.append(mark, detail);
    container.append(notice);
  });
}

function renderLogs(events) {
  const filtered = state.filter === 'all' ? events : events.filter(event => event.status === state.filter);
  $('log-total').textContent = `${filtered.length} 筆`;
  const list = $('log-list');
  list.replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement('li');
    empty.className = 'empty-copy';
    empty.textContent = '沒有符合條件的紀錄。';
    list.append(empty);
    return;
  }
  filtered.slice(0, 60).forEach(event => {
    const item = document.createElement('li');
    item.className = event.status;
    const heading = document.createElement('div');
    heading.className = 'log-title';
    const action = document.createElement('span');
    action.textContent = event.action;
    const status = document.createElement('em');
    status.textContent = statusNames[event.status] || '資訊';
    heading.append(action, status);
    const message = document.createElement('p');
    message.textContent = event.message;
    const time = document.createElement('time');
    time.textContent = localTime(event.time);
    item.append(heading, message, time);
    list.append(item);
  });
}

function reservationPayload() {
  return {
    parking_lot: $('parking-lot').value,
    start_date: $('start-date').value,
    end_date: $('end-date').value,
    contact_name: $('contact-name').value,
    phone: $('phone').value,
    car_no: $('car-no').value,
    auto_book: $('auto-book').checked,
  };
}

async function post(route, payload = {}) {
  state.busy = true;
  render();
  try {
    const response = await fetch(route, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const result = await response.json();
    if (result.state) applyState(result.state, true);
    else if (result.error) showToast({ action: '系統', status: 'error', message: result.error });
    return result;
  } catch (error) {
    state.connectionLost = true;
    showToast({ action: '連線', status: 'error', message: '無法連接本機控制台，請確認程式仍在執行。' });
    return { ok: false };
  } finally {
    state.busy = false;
    render();
  }
}

async function saveReservation() {
  if (!$('reservation-form').reportValidity()) return false;
  const result = await post('/api/reservation', reservationPayload());
  if (result.ok) state.reservationDirty = false;
  return !!result.ok;
}

function confirmAction(title, message, buttonText) {
  const dialog = $('confirm-dialog');
  $('confirm-title').textContent = title;
  $('confirm-message').textContent = message;
  $('confirm-submit').textContent = buttonText;
  if (typeof dialog.showModal !== 'function') return Promise.resolve(window.confirm(message));
  return new Promise(resolve => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    dialog.showModal();
  });
}

async function ensureReservationSaved() {
  if (!state.reservationDirty) return true;
  return saveReservation();
}

async function refresh() {
  try {
    const response = await fetch('/api/state', { cache: 'no-store' });
    if (!response.ok) throw new Error('state request failed');
    if (state.connectionLost) showToast({ action: '連線', status: 'success', message: '已重新連接本機控制台。' });
    state.connectionLost = false;
    applyState(await response.json(), state.loaded);
  } catch {
    if (!state.connectionLost) {
      state.connectionLost = true;
      showToast({ action: '連線', status: 'error', message: '與本機控制台的連線中斷，請確認程式仍在執行。' });
    }
    if (!state.loaded) {
      $('monitor-status').textContent = '連線失敗';
      $('status-pill').textContent = '無法載入';
      $('status-pill').classList.add('error');
      $('status-orb').classList.add('error');
    } else render();
  }
}

function updateClock() {
  const now = new Date();
  const dateText = new Intl.DateTimeFormat('zh-TW', { year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'long' }).format(now);
  $('clock').textContent = `${dateText}\n${localTime(now.toISOString(), { hour: '2-digit', minute: '2-digit', hour12: false })}`;
  updateCountdown();
}

$('interval').addEventListener('input', () => { state.intervalDirty = true; if (state.data) render(); });
$('reservation-form').addEventListener('input', () => { state.reservationDirty = true; });
$('reservation-form').addEventListener('change', () => { state.reservationDirty = true; });
$('interval-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!$('interval-form').reportValidity()) return;
  const result = await post('/api/scan-settings', { check_interval_seconds: $('interval').value });
  if (result.ok) state.intervalDirty = false;
  render();
});
$('reservation-form').addEventListener('submit', async event => { event.preventDefault(); await saveReservation(); render(); });
$('scan-now').addEventListener('click', async () => {
  if (!await ensureReservationSaved()) return;
  await post('/api/scan');
});
$('monitor-toggle').addEventListener('click', async () => {
  if (state.data?.monitor.running) return post('/api/monitor/stop');
  if (!await ensureReservationSaved()) return;
  if ($('auto-book').checked) {
    const accepted = await confirmAction('啟動自動預約？', '入場日有空位時，系統會向停車平台自動送出一筆預約。送單後監控會停止。', '啟動並同意送單');
    if (!accepted) return;
  }
  await post('/api/monitor/start', { confirm_auto_book: !!$('auto-book').checked });
});
$('book-now').addEventListener('click', async () => {
  if (!$('reservation-form').reportValidity()) return;
  const accepted = await confirmAction('送出預約？', '系統會先確認入場日期有空位，再向停車平台送出預約。送出後請以平台訂單紀錄確認結果。', '確認送出');
  if (!accepted) return;
  if (!await ensureReservationSaved()) return;
  await post('/api/book', { confirm: true });
});
$('clear-booking-state').addEventListener('click', async () => {
  if (!await ensureReservationSaved()) return;
  await post('/api/booking/reset');
});

document.querySelectorAll('.log-filters button').forEach(button => button.addEventListener('click', () => {
  state.filter = button.dataset.filter;
  document.querySelectorAll('.log-filters button').forEach(other => { other.classList.toggle('selected', other === button); other.setAttribute('aria-pressed', String(other === button)); });
  if (state.data) renderLogs(state.data.events);
}));
document.querySelectorAll('.side-nav a').forEach(link => link.addEventListener('click', () => {
  document.querySelectorAll('.side-nav a').forEach(other => other.classList.toggle('active', other === link));
}));

updateClock();
post('/api/session/reset').then(result => {
  if (!result.ok) refresh();
});
setInterval(updateClock, 1000);
setInterval(refresh, 2500);
