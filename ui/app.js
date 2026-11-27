'use strict';
let view = null;
let busy = false;
const $ = id => document.getElementById(id);
const node = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; };
const fmt = new Intl.NumberFormat('vi-VN', {maximumFractionDigits: 1});
const pct = n => `${fmt.format(n)}%`;
function until(ts) {
  if (!Number.isFinite(ts)) return 'Chưa có lịch reset';
  const sec = ts - Date.now() / 1000;
  if (sec <= 0) return 'Đến lịch reset · cần xác nhận lại';
  const minutes = Math.ceil(sec / 60);
  if (minutes >= 1440) return `Reset sau ${Math.floor(minutes / 1440)}n ${Math.floor(minutes % 1440 / 60)}h`;
  if (minutes >= 60) return `Reset sau ${Math.floor(minutes / 60)}h ${minutes % 60}p`;
  return `Reset sau ${minutes} phút`;
}
function account(id) { return view.accounts.find(a => a.id === id); }
function setBusy(value) {
  busy = value;
  for (const el of document.querySelectorAll('#refresh,#import,#demo,.account-refresh,.use-account,.antigravity-refresh')) el.disabled = value || el.dataset.refreshing === 'true' || (!view && el.id !== 'refresh') || (view?.live && ['import','demo'].includes(el.id));
}
let pendingUse = null;
function canSwitchAccounts() {
  return !window.RELAY_LOCAL && !!view?.account_management && !busy;
}
function useAccountButton(kind, id, name, current, blocked) {
  const button = node('button', current ? 'button secondary use-account' : 'button primary use-account', current ? 'Đang dùng' : 'Dùng account này');
  button.disabled = !!current || !!blocked || !canSwitchAccounts();
  if (blocked) button.title = blocked;
  button.addEventListener('click', () => askUseAccount(kind, id, name));
  return button;
}
function useAccountCell(kind, id, name, current, blocked) {
  const cell = node('td', 'toggle-cell');
  cell.append(useAccountButton(kind, id, name, current, blocked));
  return cell;
}
function askUseAccount(kind, id, name) {
  if (!canSwitchAccounts()) return;
  pendingUse = {kind, id, name};
  $('use-account-title').textContent = 'Dùng account này';
  $('use-account-message').textContent = kind === 'antigravity'
    ? `Đóng Antigravity IDE, ghi token của ${name} vào Keychain, rồi mở lại? Token đã lưu tương đương đăng nhập sẵn.`
    : `Đổi đăng nhập Codex sang ${name}? Bật force kill sẽ SIGKILL process tên codex, giữ VS Code, rồi ghi auth.json. Không chuyển context.`;
  if ($('codex-force-wrap')) $('codex-force-wrap').hidden = kind !== 'codex';
  $('use-account-error').textContent = '';
  $('use-account-dialog').showModal();
}
let resetDialogResolve = null;
let resetPending = false;
function showResetDialog(title, message, confirmation = false) {
  const dialog = $('reset-dialog');
  if (dialog.open) return Promise.resolve(false);
  const previousFocus = document.activeElement;
  $('reset-dialog-title').textContent = title;
  $('reset-dialog-message').textContent = message;
  dialog.returnValue = '';
  $('reset-dialog-confirm').hidden = !confirmation;
  $('reset-dialog-cancel').disabled = false;
  $('reset-dialog-cancel').textContent = confirmation ? 'Hủy' : 'Đóng';
  dialog.showModal();
  $('reset-dialog-cancel').focus();
  return new Promise(resolve => {
    resetDialogResolve = value => {
      resetDialogResolve = null;
      resolve(value);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  });
}
$('reset-dialog-cancel').addEventListener('click', () => $('reset-dialog').close('cancel'));
$('reset-dialog-confirm').addEventListener('click', () => $('reset-dialog').close('confirm'));
$('reset-dialog').addEventListener('close', () => {
  if (resetDialogResolve) resetDialogResolve($('reset-dialog').returnValue === 'confirm');
});
$('reset-dialog').addEventListener('cancel', event => {
  event.preventDefault();
  if (!resetPending) $('reset-dialog').close('cancel');
});

async function request(path, data) {
  if (busy) return;
  setBusy(true);
  try {
    const response = window.RELAY_LOCAL ? await window.RELAY_LOCAL.request(path, data) : await fetch(path, data === undefined ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Relay-CSRF': view?.csrf || ''},
      body: JSON.stringify(data)
    });
    if(path === '/api/quota/reset' && response.status === 404) throw new Error('Backend đang chạy chưa có API reset. Khởi động lại server Python rồi refresh trang.');
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Không đọc được dữ liệu.');
    view = payload;
    $('error').hidden = true;
    render();
    if(path === '/api/quota/reset') { resetPending=false; $('reset-dialog').close('done'); }
    if(payload.reset_outcome)showResetDialog('Kết quả dùng lượt reset', ({reset:'Đã dùng lượt reset và đọc lại quota.',alreadyRedeemed:'Yêu cầu reset đã hoàn tất trước đó; quota đã được đọc lại.',nothingToReset:'Không có giới hạn đủ điều kiện reset; provider không thực hiện reset.',noCredit:'Provider báo không còn lượt reset khả dụng.'})[payload.reset_outcome]);
    if((location.hash==='#login' || location.hash==='#codex-provider') && !$('saved-accounts').children.length)routePage();
  } catch (error) {
    if(path === '/api/quota/reset') {
      resetPending=false; $('reset-dialog').close('error');
      showResetDialog('Không xác nhận được reset', `${error.message}\nKhông nhấn dùng lại ngay. Kiểm tra quota và lịch sử reset trên Codex trước.`);
    }
    $('error').hidden = false;
    $('error').textContent = `Dừng thao tác: ${error.message} Kiểm tra trạng thái trước khi thử lại.`;
    $('snapshot-badge').textContent = 'KHÔNG XÁC NHẬN';
  } finally { setBusy(false); }
}
function resetCreditsCell(value, identity) {
  const cell = node('td', 'reset-credits-cell');
  if (!value || !Number.isInteger(value.available_count) || value.available_count < 0) {
    cell.append(node('span', 'quota-unknown', '— Chưa có dữ liệu'));
    return cell;
  }
  cell.append(node('strong', '', `${value.available_count} lượt`));
  if (value.available_count === 0) return cell;
  const details = node('details'), summary = node('summary', '', 'Hạn dùng lượt reset');
  details.append(summary);
  const credits = Array.isArray(value.credits) ? value.credits.filter(c => c && typeof c === 'object') : [];
  if (!credits.length) details.append(node('div', '', 'Chưa có chi tiết hạn dùng'));
  for (const credit of credits) {
    const expiry = Number.isFinite(credit.expires_at)
      ? new Date(credit.expires_at * 1000).toLocaleString('vi-VN', {timeZoneName:'short'})
      : 'Chưa được báo cáo';
    details.append(node('div', '', `${credit.title || 'Lượt reset'} · Hết hạn: ${expiry}`));
  }
  if (credits.length && credits.length < value.available_count)
    details.append(node('small', '', `Chi tiết ${credits.length}/${value.available_count} lượt`));
  cell.append(details);
  if (view?.live && credits.length === value.available_count && credits.every(c => Number.isFinite(c.expires_at) && c.expires_at > Date.now()/1000)) {
    const earliest = [...credits].sort((a,b)=>a.expires_at-b.expires_at)[0];
    const button = node('button', 'button secondary account-refresh', 'Dùng reset sắp hết hạn');
    button.disabled = busy;
    button.addEventListener('click', async () => {
      if(busy)return;
      const name=account(identity)?.name || 'Account đã chọn';
      const expiry=new Date(earliest.expires_at*1000).toLocaleString('vi-VN',{timeZoneName:'short'});
      if(!await showResetDialog('Xác nhận dùng lượt reset', `Dùng 1 lượt reset cho ${name}?\nLượt sắp hết hạn nhất: ${earliest.title || 'Lượt reset'} · ${expiry}.\nThao tác khôi phục giới hạn đủ điều kiện (5h, tuần hoặc cả hai), tiêu thụ lượt reset và không hoàn tác. App sẽ kiểm tra lại hạn dùng trước khi gửi, sau đó refresh quota. Không đổi account hay handoff.`, true))return;
      if(busy) {
        showResetDialog('Chưa gửi yêu cầu reset', 'App đang cập nhật dữ liệu. Chờ hoàn tất rồi thử lại; chưa dùng lượt reset.');
        return;
      }
      let operation;
      try { operation=crypto.randomUUID(); }
      catch(error) { showResetDialog('Chưa gửi yêu cầu reset', 'Trình duyệt không hỗ trợ tạo mã thao tác an toàn. Mở app bằng localhost hoặc 127.0.0.1.'); return; }
      resetPending=true;
      showResetDialog('Đang sử dụng lượt reset', `Đang kiểm tra và gửi yêu cầu cho ${name}. Chờ kết quả; không gửi lại thao tác.`);
      $('reset-dialog-cancel').disabled=true;
      await request('/api/quota/reset',{identity,expires_at:earliest.expires_at,operation});
    });
    cell.append(button);
  }
  return cell;
}

function quotaBar(window, label, compact) {
  const wrap = node('div', compact ? 'quota-block compact' : 'quota-block');
  const remaining = 100 - window.used;
  const line = node('div', 'quota-top');
  if (compact) line.append(node('span', 'quota-window-label', label || 'Quota'), node('strong', '', pct(remaining)));
  else line.append(node('strong', '', pct(remaining)), node('span', '', `đã dùng ${pct(window.used)}`));
  const bar = node('div', 'bar');
  const fill = node('div', `bar-fill${remaining <= 20 ? ' low' : remaining <= 40 ? ' warning' : ''}`);
  fill.style.width = `${Math.max(0, Math.min(100, remaining))}%`;
  bar.setAttribute('role', 'meter'); bar.setAttribute('aria-valuenow', String(remaining));
  bar.setAttribute('aria-valuemin', '0'); bar.setAttribute('aria-valuemax', '100');
  bar.setAttribute('aria-label', label ? `Quota còn lại · ${label}` : 'Quota còn lại'); bar.append(fill);
  wrap.append(line, bar);
  if (!compact || Number.isFinite(window.resets_at)) wrap.append(node('div', 'quota-reset', until(window.resets_at)));
  if (label && !compact) wrap.append(node('div', 'quota-reset', label));
  return wrap;
}
function quotaCell(window, error, groups, kind) {
  const cell = node('td');
  const rows = Array.isArray(groups) ? groups.filter(group => group && group[kind]) : [];
  if (rows.length) {
    for (const group of rows) cell.append(quotaBar(group[kind], group.name));
    return cell;
  }
  if (!window) { cell.append(node('span', 'quota-unknown', error ? '— Không đọc được quota' : '— Chưa được báo cáo')); return cell; }
  cell.append(quotaBar(window, window.group));
  return cell;
}
function groupWindows(group) {
  if (!group) return [];
  if (Array.isArray(group.windows) && group.windows.length) return group.windows.filter(Boolean);
  const rows = [];
  if (group.short) rows.push(group.short);
  if (group.weekly) rows.push(group.weekly);
  return rows;
}
function shortUsed(group) {
  const window = group.short || groupWindows(group).find(item => item.kind === '5h') || groupWindows(group)[0];
  return window ? Math.round(window.used) : null;
}
function nestQuotaGroups(groups) {
  const rows = Array.isArray(groups) ? groups.filter(group => group && groupWindows(group).length) : [];
  const pools = rows.filter(group => (group.kind || 'pool') !== 'model');
  const models = rows.filter(group => group.kind === 'model');
  if (!models.length) return (pools.length ? pools : rows).map(group => Object.assign({}, group, {models: []}));
  if (!pools.length) return [{name: 'Models', kind: 'bundle', windows: [], models}];
  const buckets = pools.map(pool => ({pool, models: []}));
  const leftover = [];
  for (const model of models) {
    const used = shortUsed(model);
    const hits = used == null ? [] : buckets.filter(bucket => shortUsed(bucket.pool) === used);
    const name = (model.name || '').toLowerCase();
    const byName = list => list.find(bucket => {
      const poolName = (bucket.pool.name || '').toLowerCase();
      if (/claude|gpt|oss/.test(name) && /claude|gpt/.test(poolName)) return true;
      if (/gemini/.test(name) && /gemini/.test(poolName)) return true;
      return false;
    });
    const target = hits.length === 1 ? hits[0] : (byName(hits) || byName(buckets) || (hits[0] || null));
    if (target) target.models.push(model);
    else leftover.push(model);
  }
  const nested = buckets.map(bucket => Object.assign({}, bucket.pool, {models: bucket.models}));
  if (leftover.length) nested.push({name: 'Model khác', kind: 'bundle', windows: [], models: leftover});
  return nested;
}
const antigravityQuotaOpen = new Set();
function quotaGroupsCell(groups, error) {
  const cell = node('td', 'quota-groups-cell');
  const rows = nestQuotaGroups(groups);
  if (!rows.length) {
    cell.append(node('span', 'quota-unknown', error ? '— Không đọc được quota' : '— Chưa được báo cáo'));
    return cell;
  }
  for (const group of rows) {
    const block = node('div', 'quota-group');
    const title = node('div', 'quota-group-name', group.name);
    if (group.tag) title.append(node('span', 'quota-tag', group.tag));
    block.append(title);
    for (const window of groupWindows(group)) {
      const label = window.label || (window.kind === 'weekly' ? 'tuần' : window.kind === '5h' ? '5 giờ' : window.kind);
      block.append(quotaBar(window, label, true));
    }
    if (group.models && group.models.length) {
      const details = node('details', 'quota-models');
      const key = group.name || 'models';
      details.open = antigravityQuotaOpen.has(key);
      details.addEventListener('toggle', () => {
        if (details.open) antigravityQuotaOpen.add(key);
        else antigravityQuotaOpen.delete(key);
      });
      details.append(node('summary', '', `${group.models.length} model`));
      for (const model of group.models) {
        const line = node('div', 'quota-model');
        line.append(node('span', 'quota-model-name', model.name || 'Model'));
        if (model.tag) line.append(node('span', 'quota-tag', model.tag));
        const window = groupWindows(model)[0];
        if (window) line.append(node('span', 'quota-model-pct', pct(100 - window.used)));
        details.append(line);
      }
      block.append(details);
    }
    cell.append(block);
  }
  return cell;
}
function render() {
  const active = account(view.active) || {name:"Chưa chọn", token_summary:null};
  const manage=$('manage-accounts');
  manage.setAttribute('aria-disabled',String(!view.account_management));
  manage.title=view.account_management?'Quản lý tài khoản, alias và backup':'Cần dashboard live để quản lý tài khoản';
  $('mode-title').textContent = view.live ? (view.loading ? 'Đang đọc quota Codex thật…' : view.stale ? 'Quota live đã cũ · cần cập nhật' : 'Đã kết nối Codex local · quota thật') : view.demo ? 'Bản xem trước · dữ liệu mô phỏng' : view.stale ? 'Snapshot đã cũ' : 'Snapshot quota · không phải kết nối trực tiếp';
  $('mode-text').textContent = view.live ? (view.live_error || 'Kiểm tra riêng từng account theo quota, mức hoạt động và trạng thái. Account đang dùng được xác định từ file đăng nhập local.') : view.demo ? '4 tài khoản mẫu. Không có tài khoản thật được kết nối hoặc chuyển đổi.' : view.stale ? 'Quota quá 60 giây sẽ không được dùng để đề xuất chuyển.' : 'Chỉ dùng metadata bạn cung cấp. Tài khoản trong app chưa được xác nhận.';
  const antigravityRows = (view.antigravity && view.antigravity.profiles) || [];
  const managedCount = view.accounts.length + antigravityRows.filter(profile => !profile.archived).length;
  $('count').textContent = managedCount;
  $('nav-count').textContent = managedCount;
  $('ready').textContent = view.accounts.filter(a => a.eligible && !a.active).length;
  $('ready-total').textContent = ` / ${view.accounts.length}`;
  $('active-name').textContent = active.name;
  $('active-name').title = active.name;
  $('active-note').textContent = view.live ? 'Theo đăng nhập trên máy · chưa xác nhận UI client' : view.demo ? 'Được đánh dấu trong dữ liệu demo' : 'Được đánh dấu trong snapshot';
  $('source').textContent = view.live ? 'Codex app-server · đọc quota thật' : view.demo ? 'Mô phỏng · không tiêu thụ quota' : 'Không có live collector';
  $('snapshot-badge').textContent = view.live ? (view.loading ? 'CONNECTING' : view.stale ? 'LIVE · STALE' : 'LIVE DATA') : view.demo ? 'DEMO DATA' : view.stale ? 'STALE SNAPSHOT' : 'METADATA SNAPSHOT';
  const tokens = active.token_summary?.lifetimeTokens;
  $('token-summary').textContent = Number.isFinite(tokens) ? `Hoạt động token tài khoản: ${fmt.format(tokens)} tokens` : 'Tokens / chi phí: chưa có dữ liệu';
  const body = $('accounts-body'); body.replaceChildren();
  for (const a of view.accounts) {
    const row = node('tr', a.active ? 'current' : '');
    const cell = node('td');
    const block = node('div', 'account-cell');
    const icon = node('div', 'account-icon', a.name.slice(0, 1).toUpperCase());
    const label = node('div'), top = node('div', 'account-top');
    top.append(node('strong', '', a.name));
    if (a.active) top.append(node('span', 'mini-active'));
    label.append(top, node('small', '', a.active ? (view.live ? `Đang dùng · ${a.plan || 'ChatGPT'}` : 'Đang dùng · theo snapshot') : (a.plan || a.id)));
    block.append(icon, label); cell.append(block);
    let state = 'Sẵn sàng', className = 'row-state';
    if (a.excluded) { state = 'Đã tạm loại'; className += ' amber'; }
    else if (view.stale || a.reason === 'Quota chưa được xác nhận' || a.reason.startsWith('Dữ liệu')) { state = 'Cần refresh'; className += ' amber'; }
    else if (a.active) { state = a.can_keep ? 'Đang sử dụng' : 'Quota thấp'; className += a.can_keep ? ' active' : ' amber'; }
    else if (!a.eligible) { state = 'Chưa đủ dư địa'; className += ' amber'; }
    const status = node('td'); const badge = node('span', className, `● ${state}`); badge.title = a.reason; status.append(badge);
    if(a.connection_error)status.append(node('small','',a.connection_error));
    if (a.polling) {
      const p=a.polling;
      const stamp=value=>Number.isFinite(value)?new Date(value*1000).toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'}):'—';
      const cadence=p.interval_seconds<60?`${p.interval_seconds} giây`:`${Math.round(p.interval_seconds/60)} phút`;
      const text=node('small','quota-countdown');
      text.dataset.nextCheck=String(p.next_check_at);
      text.dataset.refreshing=String(!!a.refreshing);
      text.title=`${cadence}/lần · lần đọc gần nhất ${stamp(p.last_checked_at)}`;
      text.style.display='block';status.append(text);
      const refresh=node('button','button secondary account-refresh',a.refreshing?'Đang kiểm tra…':'↻ Refresh');
      refresh.dataset.refreshing=String(!!a.refreshing);
      refresh.disabled=!!a.refreshing;
      refresh.setAttribute('aria-label',`Refresh quota ${a.name}`);
      refresh.addEventListener('click',()=>request('/api/refresh',{identity:a.id}));
      status.append(refresh);
      const changed=node('small','',`Mốc quota không đổi: ${stamp(p.last_changed_at)}${p.reason==='inactive'?' · suy đoán không hoạt động':''}`);
      changed.style.display='block';status.append(changed);
    }

    row.append(cell, quotaCell(a.short,a.connection_error), quotaCell(a.weekly,a.connection_error), resetCreditsCell(a.reset_credits,a.id), status, useAccountCell('codex', a.id, a.name, a.active)); body.append(row);
  }
  renderAntigravityAccounts();
  updateClock();
}
let antigravityQuotaTimer = null;
let antigravityRefreshing = new Set();
async function refreshAntigravityQuota(identity) {
  if (window.RELAY_LOCAL || busy || antigravityRefreshing.has(identity) || !view?.account_management) return;
  antigravityRefreshing.add(identity);
  renderAntigravityAccounts();
  try {
    await accountAPI('/api/providers/antigravity/quota', {identity});
    await request('/api/state');
  } catch (error) {
    $('error').hidden = false;
    $('error').textContent = error.message;
  } finally {
    antigravityRefreshing.delete(identity);
    if (view) renderAntigravityAccounts();
  }
}
function renderAntigravityAccounts() {
  const body = $('antigravity-accounts-body');
  const badge = $('antigravity-accounts-badge');
  const refreshBtn = $('antigravity-quota-refresh');
  if (!body) return;
  body.replaceChildren();
  const summary = view.antigravity || {enabled:false, profiles:[]};
  const profiles = summary.profiles || [];
  if (refreshBtn) refreshBtn.disabled = !summary.enabled || window.RELAY_LOCAL || busy;
  if (badge) badge.textContent = summary.enabled ? (summary.pending_recovery ? 'CẦN KHÔI PHỤC' : summary.live && summary.live.readable ? 'KEYCHAIN' : 'VAULT') : 'TẮT';
  if (!summary.enabled) {
    const row = node('tr');
    const cell = node('td','', 'Chưa bật quản lý Antigravity. Chạy dashboard với account mode để lưu snapshot Keychain.');
    cell.colSpan = 5;
    row.append(cell); body.append(row);
    return;
  }
  if (!profiles.length) {
    const row = node('tr');
    const cell = node('td','', 'Chưa có snapshot Antigravity. Đăng nhập trong IDE rồi lưu phiên trên trang Antigravity.');
    cell.colSpan = 5;
    row.append(cell); body.append(row);
    return;
  }
  let loading = false;
  for (const profile of profiles) {
    const row = node('tr', profile.selected ? 'current' : '');
    const cell = node('td');
    const block = node('div', 'account-cell');
    const name = profile.alias || profile.email || profile.name || (profile.id || '?').slice(0, 12);
    const icon = node('div', 'account-icon', String(name).slice(0, 1).toUpperCase());
    const label = node('div'), top = node('div', 'account-top');
    top.append(node('strong', '', name));
    if (profile.selected) top.append(node('span', 'mini-active'));
    label.append(top, node('small', '', profile.email || (profile.archived ? 'Đã lưu trữ trên Relay' : 'Antigravity IDE · snapshot Keychain')));
    block.append(icon, label); cell.append(block);
    const quota = profile.quota || {};
    const plan = node('td', '', quota.plan || '—');
    const failed = quota.status && quota.status !== 'ok' && quota.status !== 'loading';
    const quotaError = failed ? (quota.error || 'Không đọc được quota') : (quota.status === 'loading' ? 'Đang đọc quota' : null);
    loading = loading || quota.status === 'loading';
    let state = 'Đã lưu', className = 'row-state';
    if (profile.archived) { state = 'Đã lưu trữ'; className += ' amber'; }
    else if (quota.status === 'loading') { state = 'Đang đọc quota'; className += ' amber'; }
    else if (failed) { state = 'Cần refresh'; className += ' amber'; }
    else if (profile.selected) { state = 'Keychain hiện tại'; className += ' active'; }
    else { state = 'Snapshot đã lưu'; }
    const status = node('td');
    status.append(node('span', className, `● ${state}`));
    if (quota.stale) status.append(node('small','','Số liệu đã cũ'));
    const blocked = profile.archived ? 'Snapshot đã lưu trữ' : (summary.pending_recovery ? 'Cần khôi phục lần chuyển bị gián đoạn' : '');
    const actions = node('td', 'toggle-cell');
    const stack = node('div', 'account-actions');
    const refreshing = antigravityRefreshing.has(profile.id);
    const refresh = node('button', 'button secondary antigravity-refresh', refreshing ? 'Đang đọc…' : '↻ Quota');
    refresh.disabled = !!profile.archived || window.RELAY_LOCAL || busy || refreshing || !view?.account_management;
    refresh.dataset.refreshing = String(refreshing);
    refresh.setAttribute('aria-label', `Đọc quota ${name}`);
    refresh.addEventListener('click', () => refreshAntigravityQuota(profile.id));
    stack.append(refresh, useAccountButton('antigravity', profile.id, name, profile.selected, blocked));
    actions.append(stack);
    row.append(cell, plan, quotaGroupsCell(quota.groups, quotaError), status, actions);
    body.append(row);
  }
  if (loading && !antigravityQuotaTimer && !window.RELAY_LOCAL) {
    antigravityQuotaTimer = setTimeout(() => {
      antigravityQuotaTimer = null;
      if (!busy) request('/api/state');
    }, 1500);
  }
}
function updateClock() {
  for(const el of document.querySelectorAll('.quota-countdown')) {
    const seconds=Math.max(0,Math.ceil(Number(el.dataset.nextCheck)-Date.now()/1000));
    el.textContent=el.dataset.refreshing==='true'?'Đang kiểm tra dữ liệu…':seconds===0?'Đến lượt kiểm tra…':`Kiểm tra sau ${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
  }
  $('clock').textContent = new Intl.DateTimeFormat('vi-VN', {hour:'2-digit',minute:'2-digit',timeZone:'Asia/Tokyo'}).format(new Date()) + ' JST';
  if (view) {
    const age = Math.max(0, Math.floor(Date.now() / 1000 - view.received_at));
    $('freshness').textContent = age < 60 ? `${age}s` : `${Math.floor(age / 60)}p`;
    if (!view.live && !view.demo && age > 60 && !view.stale && !busy) request('/api/state');
  }
}
$('refresh').addEventListener('click', async () => {
  if (view?.live) { await request('/api/refresh', {}); setTimeout(() => request('/api/state'), 2000); }
  else request('/api/state');
});
$('demo').addEventListener('click', () => request('/api/demo', {}));
$('import').addEventListener('click', () => $('import-file').click());
$('import-file').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return;
  try {
    if (file.size > 4 * 1024 * 1024) throw new Error('File tối đa 4 MB.');
    await request('/api/import', JSON.parse(await file.text()));
  } catch (error) { $('error').hidden = false; $('error').textContent = `Không nhập được snapshot: ${error.message}`; }
  event.target.value = '';
});
setInterval(updateClock, 1000);
setInterval(()=>{if(!busy && !$('reset-dialog').open && view?.accounts.some(a=>a.refreshing))request('/api/state');},2000);
setInterval(() => { if (!busy && !$('reset-dialog').open && !$('use-account-dialog').open) request('/api/state'); }, 30000);
request('/api/state');

async function accountAPI(path, data) {
  const result = await fetch(path, data === undefined ? {cache:'no-store'} : {
    method:'POST', headers:{'Content-Type':'application/json','X-Relay-CSRF':view.csrf}, body:JSON.stringify(data)
  });
  const payload = await result.json();
  if (!result.ok) throw new Error(payload.error || 'Không hoàn tất thao tác.');
  return payload;
}
async function savedAccounts() {
  const result = await accountAPI('/api/accounts');
  const list = $('saved-accounts'); list.replaceChildren();
  if (!result.profiles.length) list.append(node('p','account-help','Chưa có tài khoản. Thêm qua đăng nhập OpenAI hoặc lưu đăng nhập local hiện tại.'));
  for (const profile of result.profiles) {
    const row = node('div','saved-account'), details = node('div','saved-details');
    details.append(node('strong','',profile.display_name || profile.name), node('small','',profile.email || profile.id.slice(0,12)));
    if (profile.selected_on_disk) details.append(node('span','chip mint','Đang được chọn trên máy'));
    const editor=node('div','alias-editor'), input=node('input');
    input.value=profile.alias || ''; input.maxLength=80;
    input.placeholder='Alias · để trống dùng email';
    input.setAttribute('aria-label',`Alias cho ${profile.email || profile.name}`);
    const save=node('button','button secondary','Lưu alias');
    save.addEventListener('click',()=>accountOperation('/api/accounts/update',{identity:profile.id,alias:input.value}));
    editor.append(input,save);
    const actions=node('div','account-actions');
    const button = node('button','button secondary',profile.selected_on_disk ? 'Đã chọn trên đĩa' : 'Sử dụng tài khoản');
    button.disabled = profile.selected_on_disk || result.pending_recovery;
    button.addEventListener('click', () => askUseAccount('codex', profile.id, profile.display_name || profile.name));
    const remove=node('button','button danger-outline','Gỡ profile');
    remove.disabled=!!result.pending_recovery;
    remove.addEventListener('click',()=>{
      pendingDelete=profile;
      $('delete-account-label').textContent=profile.display_name || profile.name;
      $('delete-account-dialog').showModal();
    });
    save.disabled=!!result.pending_recovery;
    actions.append(button,remove); row.append(details,editor,actions); list.append(row);
  }
  $('restore-account').disabled = !result.pending_recovery;
}
let accountBusy = false;
async function accountOperation(path, data) {
  if (accountBusy) return;
  accountBusy = true;
  const buttons = [...$('account-dialog').querySelectorAll('button')].map(button=>[button,button.disabled]);
  for (const [button] of buttons) button.disabled = true;
  try {
    const result = await accountAPI(path,data);
    $('account-message').textContent = result.removed ? 'Đã gỡ profile khỏi Relay; đăng nhập local không thay đổi.' : path.endsWith('/update') ? 'Đã lưu alias.' : result.forced ? `Đã SIGKILL ${result.killed || 0} process Codex rồi ghi auth.json. VS Code vẫn chạy; kiểm tra extension có nhận account mới không.` : result.restart_required ? 'Đã cập nhật file đăng nhập. Mở lại hai client và kiểm tra account hiển thị. Quota đang được đọc lại cho các profile thật.' : 'Đã lưu profile. Chưa đổi đăng nhập.';
    await savedAccounts();
    await request('/api/state');
  } catch(error) { $('account-message').textContent = error.message; }
  finally {
    accountBusy = false;
    for (const [button,disabled] of buttons) if (button.isConnected) button.disabled = disabled;
  }
}
$('close-accounts').addEventListener('click',()=>{location.hash='overview';});
$('refresh-accounts').addEventListener('click', () => savedAccounts().catch(error => $('account-message').textContent=error.message));
$('capture-account').addEventListener('click', () => accountOperation('/api/accounts/capture',{name:$('profile-name').value}));
$('restore-account').addEventListener('click', () => accountOperation('/api/accounts/restore',{}));

let pendingDelete=null;
$('cancel-delete-account').addEventListener('click',()=>{pendingDelete=null;$('delete-account-dialog').close();});
$('confirm-delete-account').addEventListener('click',async()=>{
  if(!pendingDelete) return;
  const identity=pendingDelete.id;pendingDelete=null;$('delete-account-dialog').close();
  await accountOperation('/api/accounts/remove',{identity});
});
let loginTimer=null;
async function pollLogin() {
  clearTimeout(loginTimer);
  const status=await accountAPI('/api/accounts/login');
  const active=['starting','waiting','cancelling'].includes(status.status);
  $('login-status').hidden=!active;
  $('login-account').disabled=active;
  $('login-link').hidden=!status.auth_url;
  if(status.auth_url) $('login-link').href=status.auth_url;
  else $('login-link').removeAttribute('href');
  $('login-status-text').textContent=status.status==='waiting' ? 'Mở trang OpenAI và chọn tài khoản cần thêm. Đăng nhập hiện tại không bị đổi.' : 'Đang chuẩn bị đăng nhập…';
  if(active) loginTimer=setTimeout(()=>pollLogin().catch(error=>{$('account-message').textContent=error.message;}),1500);
  else if(status.status==='complete') {
    $('account-message').textContent='Đã thêm tài khoản. Quota sẽ được đọc tự động.';
    await savedAccounts(); await request('/api/state');
  } else if(status.status==='error' || status.status==='expired') $('account-message').textContent=status.message || 'Phiên đăng nhập hết hạn. Hãy thử lại.';
}
$('login-account').addEventListener('click',async()=>{
  $('login-account').disabled=true;
  try {
    await accountAPI('/api/accounts/login/start',{alias:$('profile-name').value});
    await pollLogin();
  } catch(error) { $('account-message').textContent=error.message;$('login-account').disabled=false; }
});
$('cancel-login').addEventListener('click',async()=>{
  try { await accountAPI('/api/accounts/login/cancel',{});await pollLogin(); }
  catch(error) { $('account-message').textContent=error.message; }
});

function base64Bytes(bytes) {
  let value=''; for (const byte of bytes) value+=String.fromCharCode(byte);
  return btoa(value);
}
function decodeBackup(value) {
  if (typeof value!=='string' || value.length>6000000) throw new Error('File backup không hợp lệ.');
  return Uint8Array.from(atob(value),c=>c.charCodeAt(0));
}
async function backupKey(password,salt) {
  const material=await crypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveKey']);
  return crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:600000,hash:'SHA-256'},material,{name:'AES-GCM',length:256},false,['encrypt','decrypt']);
}
$('export-accounts').addEventListener('click',async()=>{
  if(accountBusy)return;
  accountBusy=true;
  try {
    const password=$('backup-password').value;
    if(password.length<12)throw new Error('Nhập mật khẩu backup ít nhất 12 ký tự.');
    const salt=crypto.getRandomValues(new Uint8Array(16)),iv=crypto.getRandomValues(new Uint8Array(12));
    const key=await backupKey(password,salt);
    const bundle=await accountAPI('/api/accounts/export',{});
    if(!bundle.profiles.length)throw new Error('Chưa có tài khoản để export.');
    const encrypted=await crypto.subtle.encrypt({name:'AES-GCM',iv},key,new TextEncoder().encode(JSON.stringify(bundle)));
    const file={format:'relay-encrypted-v1',salt:base64Bytes(salt),iv:base64Bytes(iv),data:base64Bytes(new Uint8Array(encrypted))};
    const url=URL.createObjectURL(new Blob([JSON.stringify(file)],{type:'application/json'}));
    const link=node('a');link.href=url;link.download='relay-accounts-encrypted.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    $('account-message').textContent='Đã export file mã hóa. Phiên đăng nhập hết hạn có thể cần đăng nhập lại sau khi import.';
  }catch(error){$('account-message').textContent=error.message;}
  finally{$('backup-password').value='';accountBusy=false;}
});
$('import-accounts').addEventListener('click',()=>{if(!accountBusy)$('backup-file').click();});
$('backup-file').addEventListener('change',async()=>{
  if(accountBusy)return;
  const file=$('backup-file').files[0];if(!file)return;
  accountBusy=true;
  try{
    if(file.size>6000000)throw new Error('File quá lớn (tối đa 6 MB).');
    const password=$('backup-password').value;
    if(password.length<12)throw new Error('Nhập mật khẩu của file backup trước khi import.');
    const backup=JSON.parse(await file.text());
    if(backup.format!=='relay-encrypted-v1')throw new Error('Chỉ nhận file export mã hóa của Relay.');
    const salt=decodeBackup(backup.salt),iv=decodeBackup(backup.iv);
    if(salt.length!==16||iv.length!==12)throw new Error('File backup không hợp lệ.');
    const key=await backupKey(password,salt);
    let plain;
    try{plain=await crypto.subtle.decrypt({name:'AES-GCM',iv},key,decodeBackup(backup.data));}
    catch{throw new Error('Sai mật khẩu hoặc file đã hỏng.');}
    const result=await accountAPI('/api/accounts/import',{bundle:JSON.parse(new TextDecoder().decode(plain))});
    $('account-message').textContent=`Đã nhập ${result.imported} tài khoản; giữ nguyên ${result.skipped} tài khoản trùng. Đăng nhập local không thay đổi.`;
    await savedAccounts();await request('/api/state');
  }catch(error){$('account-message').textContent=error.message;}
  finally{accountBusy=false;$('backup-password').value='';$('backup-file').value='';}
});


$('use-account-cancel').addEventListener('click', () => $('use-account-dialog').close());
$('use-account-confirm').addEventListener('click', async () => {
  if (!pendingUse || !canSwitchAccounts()) return;
  const button = $('use-account-confirm');
  button.disabled = true;
  $('use-account-error').textContent = '';
  try {
    if (pendingUse.kind === 'antigravity') {
      const relaunch = $('antigravity-relaunch') ? $('antigravity-relaunch').checked : true;
      await accountAPI('/api/providers/antigravity/switch', {identity: pendingUse.id, relaunch});
      await antigravityProfiles();
    } else {
      const force = $('codex-force-auth') ? $('codex-force-auth').checked : false;
      await accountAPI('/api/accounts/switch', {identity: pendingUse.id, force});
      if (location.hash === '#login' || location.hash === '#codex-provider') await savedAccounts();
    }
    $('use-account-dialog').close();
    pendingUse = null;
    await request('/api/state');
  } catch (error) {
    $('use-account-error').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

function routePage(){
  const hash=location.hash||'#overview';
  const antigravity=hash==='#antigravity-provider';
  const codex=hash==='#codex-provider' || hash==='#login';
  const login=codex || antigravity;
  $('dashboard-page').hidden=login;
  $('account-dialog').hidden=!login;
  if($('codex-provider'))$('codex-provider').hidden=!codex;
  if($('antigravity-provider'))$('antigravity-provider').hidden=!antigravity;
  const child=antigravity?'#antigravity-provider':codex?'#codex-provider':hash;
  for(const link of document.querySelectorAll('.sidebar nav a')){
    const href=link.getAttribute('href');
    const current=href===child;
    link.classList.toggle('selected',current);
    link.classList.toggle('open',href==='#login' && login);
    if(current)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  }
  const group=document.querySelector('.nav-group');
  if(group)group.classList.toggle('open',login);
  document.querySelector('.breadcrumb').textContent=antigravity?'Workspace / Quản lý đăng nhập / Antigravity':codex?'Workspace / Quản lý đăng nhập / Codex':hash==='#accounts'?'Workspace / Tài khoản':'Workspace / Tổng quan';
  if($('manage-title'))$('manage-title').textContent=antigravity?'Antigravity':codex?'Codex':'Quản lý đăng nhập';
  if($('manage-eyebrow'))$('manage-eyebrow').textContent=antigravity?'GOOGLE · KEYCHAIN':codex?'CODEX · FILE AUTH':'LOCAL FILE AUTH';
  if(login){
    if(antigravity)antigravityProfiles();
    if(!view?.account_management){$('account-message').textContent='Quản lý đăng nhập cần dashboard live.';if(codex)return;}
    if($('account-message') && $('account-message').textContent==='Quản lý đăng nhập cần dashboard live.')$('account-message').textContent='';
    if(codex)savedAccounts().then(pollLogin).catch(error=>$('account-message').textContent=error.message);
  }
}
window.addEventListener('hashchange',routePage);
$('manage-accounts').addEventListener('click',()=>{if(location.hash==='#login')routePage();});
routePage();
const themeMedia=window.matchMedia('(prefers-color-scheme: dark)');
let themeChoice='system';
try{themeChoice=localStorage.getItem('relay-theme')||'system';}catch{}
if(!['system','light','dark'].includes(themeChoice))themeChoice='system';
function applyTheme(){
  document.documentElement.dataset.theme=themeChoice==='system'?(themeMedia.matches?'dark':'light'):themeChoice;
  $('theme-select').value=themeChoice;
}
$('theme-select').addEventListener('change',()=>{
  themeChoice=$('theme-select').value;
  try{localStorage.setItem('relay-theme',themeChoice);}catch{}
  applyTheme();
});
themeMedia.addEventListener('change',()=>{if(themeChoice==='system')applyTheme();});
applyTheme();


async function antigravityProfiles(path='/api/providers/antigravity',data) {
  const status=$('antigravity-status'),capture=$('antigravity-capture'),restore=$('antigravity-restore');
  if(window.RELAY_LOCAL){
    status.textContent='Quản lý Antigravity cần dashboard server; bản HTML offline không đụng Keychain.';
    capture.disabled=true;restore.disabled=true;return;
  }
  capture.disabled=true;restore.disabled=true;
  try {
    const result=await accountAPI(path,data);
    if(result.changed){
      status.textContent=result.relaunched
        ? 'Đã ghi token vào Keychain và yêu cầu mở lại Antigravity IDE. Kiểm tra tài khoản hiển thị trong IDE.'
        : (result.message || 'Đã ghi token vào Keychain. Mở lại Antigravity IDE và kiểm tra tài khoản.');
    }else if(result.restored){
      status.textContent='Đã khôi phục token Keychain trước lần chuyển bị gián đoạn. Mở lại IDE và kiểm tra tài khoản.';
    }else if(!result.enabled){
      status.textContent='Bật quản lý account ở dashboard server để lưu profile Antigravity.';
    }else if(result.pending_recovery){
      status.textContent='Có lần chuyển bị gián đoạn. Đóng IDE rồi bấm khôi phục trước khi đổi tiếp.';
    }else if(result.live && result.live.readable){
      status.textContent=`Keychain hiện tại: ${result.live.email || 'đã đăng nhập'}. Token đã lưu tương đương phiên đăng nhập sẵn.`;
    }else{
      status.textContent='Chưa đọc được token gemini/antigravity trong Keychain. Đăng nhập trong Antigravity IDE rồi bấm lưu phiên.';
    }
    const list=$('antigravity-profiles');list.replaceChildren();
    if(result.enabled && !(result.profiles||[]).length){
      list.append(node('p','account-help','Chưa có snapshot nào. Đăng nhập một tài khoản trong Antigravity IDE, rồi bấm “Lưu phiên Keychain hiện tại”.'));
    }
    for(const profile of result.profiles||[]){
      const row=node('div','saved-account'),details=node('div','saved-details');
      const header=node('div','gemini-header'),titleBox=node('div');
      const quota=profile.quota||{};
      const remain=window=>window?`${pct(100-window.used)} còn`:'—';
      const groupLine=nestQuotaGroups(quota.groups).map(group => {
        const parts=groupWindows(group).map(window => `${window.label || ''} ${remain(window)}`.trim());
        const extra=group.models && group.models.length ? ` · ${group.models.length} model` : '';
        return (parts.length || extra) ? `${group.name} ${parts.join(' / ')}${extra}`.trim() : '';
      }).filter(Boolean).join(' · ');
      const quotaLine=quota.status==='ok'
        ? `${quota.plan || 'Antigravity'} · ${groupLine || `5 giờ ${remain(quota.short)} · tuần ${remain(quota.weekly)}`}`
        : quota.status==='loading' ? 'Đang đọc quota 5 giờ và tuần'
        : (quota.error || (profile.archived ? 'Đã lưu trữ · snapshot token vẫn được giữ' : profile.selected ? 'Đang là phiên Keychain hiện tại' : (profile.email || 'Snapshot đã lưu · chưa được chọn')));
      titleBox.append(
        node('strong','',profile.alias || profile.email || profile.name),
        node('small','',quotaLine)
      );
      if(profile.selected)titleBox.append(node('span','chip mint','Keychain hiện tại'));
      header.append(titleBox);details.append(header);
      const editor=node('div','alias-editor'),alias=node('input');
      alias.value=profile.alias || '';alias.maxLength=80;alias.setAttribute('aria-label','Tên profile Antigravity');
      const save=node('button','button secondary','Lưu tên');
      save.addEventListener('click',()=>antigravityProfiles('/api/providers/antigravity/update',{identity:profile.id,alias:alias.value}));
      const archive=node('button','button secondary',profile.archived ? 'Khôi phục profile' : 'Lưu trữ profile');
      archive.addEventListener('click',()=>antigravityProfiles('/api/providers/antigravity/update',{identity:profile.id,archived:!profile.archived}));
      editor.append(alias,save,archive);details.append(editor);
      const actions=node('div','account-actions');
      const use=node('button','button primary',profile.selected ? 'Đang dùng' : 'Đổi sang tài khoản này');
      use.disabled=profile.selected || profile.archived || result.pending_recovery;
      use.addEventListener('click',()=>{
        const relaunch=$('antigravity-relaunch').checked;
        const confirmText=relaunch
          ? `Đóng Antigravity IDE, ghi token của ${profile.alias || profile.email || 'profile này'} vào Keychain, rồi mở lại? Token đã lưu tương đương đăng nhập sẵn.`
          : `Ghi token của ${profile.alias || profile.email || 'profile này'} vào Keychain? Hãy đóng Antigravity IDE trước. Token đã lưu tương đương đăng nhập sẵn.`;
        if(!confirm(confirmText))return;
        antigravityProfiles('/api/providers/antigravity/switch',{identity:profile.id,relaunch});
      });
      const remove=node('button','button secondary danger-outline','Gỡ snapshot');
      remove.addEventListener('click',()=>{
        if(!confirm('Xóa snapshot token trên máy? Không đăng xuất tài khoản Google và không đổi Keychain đang dùng.'))return;
        antigravityProfiles('/api/providers/antigravity/remove',{identity:profile.id});
      });
      actions.append(use,remove);details.append(actions);
      row.append(details);list.append(row);
    }
    if(path.endsWith('/capture'))$('antigravity-alias').value='';
    capture.disabled=!result.enabled;
    restore.disabled=!result.enabled || !result.pending_recovery;
  } catch(error){
    status.textContent=error.message;
    capture.disabled=false;restore.disabled=false;
  }
}
$('antigravity-quota-refresh').addEventListener('click', async () => {
  if (window.RELAY_LOCAL || busy) return;
  try {
    await accountAPI('/api/providers/antigravity/quota', {});
    await request('/api/state');
  } catch (error) {
    $('error').hidden = false;
    $('error').textContent = error.message;
  }
});
$('antigravity-capture').addEventListener('click',()=>antigravityProfiles('/api/providers/antigravity/capture',{alias:$('antigravity-alias').value}));
$('antigravity-restore').addEventListener('click',()=>antigravityProfiles('/api/providers/antigravity/restore',{}));
$('antigravity-refresh').addEventListener('click',()=>antigravityProfiles());
antigravityProfiles();
