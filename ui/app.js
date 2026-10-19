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
  for (const el of document.querySelectorAll('#refresh,#import,#demo,#reserve,#margin,.toggle,.account-refresh,[data-strategy]')) el.disabled = value || el.dataset.refreshing === 'true' || (!view && el.id !== 'refresh') || (view?.live && ['import','demo'].includes(el.id));
}
async function request(path, data) {
  if (busy) return;
  setBusy(true);
  try {
    const response = window.RELAY_LOCAL ? await window.RELAY_LOCAL.request(path, data) : await fetch(path, data === undefined ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Relay-CSRF': view?.csrf || ''},
      body: JSON.stringify(data)
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Không đọc được dữ liệu.');
    view = payload;
    $('error').hidden = true;
    render();
    if(location.hash==='#login' && !$('saved-accounts').children.length)routePage();
  } catch (error) {
    $('error').hidden = false;
    $('error').textContent = `Dừng thao tác: ${error.message} Kiểm tra trạng thái trước khi thử lại.`;
    $('snapshot-badge').textContent = 'KHÔNG XÁC NHẬN';
    $('preview').disabled = true;
  } finally { setBusy(false); }
}
function quotaCell(window, error) {
  const cell = node('td');
  if (!window) { cell.append(node('span', 'quota-unknown', error ? '— Không đọc được quota' : '— Chưa được báo cáo')); return cell; }
  const remaining = 100 - window.used;
  const line = node('div', 'quota-top');
  line.append(node('strong', '', pct(remaining)), node('span', '', `đã dùng ${pct(window.used)}`));
  const bar = node('div', 'bar');
  const fill = node('div', `bar-fill${remaining <= 20 ? ' low' : remaining <= 40 ? ' warning' : ''}`);
  fill.style.width = `${Math.max(0, Math.min(100, remaining))}%`;
  bar.setAttribute('role', 'meter'); bar.setAttribute('aria-valuenow', String(remaining));
  bar.setAttribute('aria-valuemin', '0'); bar.setAttribute('aria-valuemax', '100');
  bar.setAttribute('aria-label', 'Quota còn lại'); bar.append(fill);
  cell.append(line, bar, node('div', 'quota-reset', until(window.resets_at)));
  return cell;
}
function render() {
  const active = account(view.active) || {name:"Chưa chọn", token_summary:null}, target = account(view.target);
  const manage=$('manage-accounts');
  manage.setAttribute('aria-disabled',String(!view.account_management));
  manage.title=view.account_management?'Quản lý tài khoản, alias và backup':'Cần dashboard live để quản lý tài khoản';
  $('mode-title').textContent = view.live ? (view.loading ? 'Đang đọc quota Codex thật…' : view.stale ? 'Quota live đã cũ · cần cập nhật' : 'Đã kết nối Codex local · quota thật') : view.demo ? 'Bản xem trước · dữ liệu mô phỏng' : view.stale ? 'Snapshot đã cũ' : 'Snapshot quota · không phải kết nối trực tiếp';
  $('mode-text').textContent = view.live ? (view.live_error || 'Kiểm tra riêng từng account theo quota, mức hoạt động và trạng thái. Account đang dùng được xác định từ file đăng nhập local.') : view.demo ? '4 tài khoản mẫu. Không có tài khoản thật được kết nối hoặc chuyển đổi.' : view.stale ? 'Quota quá 60 giây sẽ không được dùng để đề xuất chuyển.' : 'Chỉ dùng metadata bạn cung cấp. Tài khoản trong app chưa được xác nhận.';
  $('count').textContent = view.accounts.length;
  $('nav-count').textContent = view.accounts.length;
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
    else if (a.active) { state = a.can_keep ? 'Đang sử dụng' : 'Cần bàn giao'; className += a.can_keep ? ' active' : ' amber'; }
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

    const toggleCell = node('td', 'toggle-cell'), toggle = node('input', 'toggle');
    toggle.type = 'checkbox'; toggle.checked = !a.excluded; toggle.setAttribute('aria-label', `Cho ${a.name} tham gia rotate`);
    toggle.addEventListener('change', () => {
      const disabled = new Set(view.settings.disabled);
      if (toggle.checked) disabled.delete(a.id); else disabled.add(a.id);
      request('/api/settings', {...view.settings, disabled: [...disabled]});
    });
    toggleCell.append(toggle);
    row.append(cell, quotaCell(a.short,a.connection_error), quotaCell(a.weekly,a.connection_error), status, toggleCell); body.append(row);
  }
  $('rotation-runner').hidden=!view.live;
  $('reserve').value = view.settings.reserve;
  $('reserve-value').textContent = view.settings.reserve;
  $('margin').value = view.settings.margin;
  $('margin-value').textContent = view.settings.margin;
  for (const button of document.querySelectorAll('[data-strategy]')) {
    const selected = button.dataset.strategy === view.settings.strategy;
    button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected));
  }
  $('strategy-description').textContent = view.settings.strategy === 'balanced' ? 'Giữ account hiện tại khi còn đủ quota. Khi cần chuyển, chọn account có mức dư thấp nhất giữa các cửa sổ cao nhất.' : 'Ưu tiên quota tuần còn nhiều và gần reset, sau đó xét quota 5 giờ và dư địa của phiên. Account mới phải còn trên 30% ở cả hai cửa sổ, hoặc cao hơn theo cấu hình dự phòng.';
  $('policy-summary').textContent = `Bàn giao khi dùng từ ${view.threshold}% ở bất kỳ cửa sổ nào. Account nhận phiên mới phải dùng dưới ${view.entry_threshold}% ở tất cả cửa sổ. Nếu cửa sổ đang cạn sắp reset trong 5 phút, ưu tiên chờ và đọc lại quota. Cài đặt chỉ lưu trong phiên dashboard này.`;
  $('decision-title').textContent = view.action==='wait_reset'?`Chờ reset trên ${active.name}.`:view.action === 'keep' ? `Tiếp tục với ${active.name}.` : view.action === 'handoff' ? `Chuẩn bị chuyển sang ${target.name}.` : 'Tạm dừng và đọc lại quota.';
  $('decision-copy').textContent = view.action==='wait_reset'?'Quota đang hạn chế sẽ reset trong vòng 5 phút. Chưa compact hoặc chuyển account; đọc lại quota sau reset để xác nhận trước khi tiếp tục.':view.action === 'keep' ? 'Chưa cần chuyển: quota vẫn đủ phần dự phòng. Giữ phiên hiện tại giúp giảm chi phí bàn giao và mất context.' : view.action === 'handoff' ? `${active.name} đã chạm ngưỡng hoặc bị loại. ${target.name} có ${pct(target.bottleneck_remaining)} dư địa ở cửa sổ hạn chế nhất.` : 'Chưa có account nhận phiên mới đủ điều kiện. Không đoán quota đã hồi chỉ dựa trên giờ reset.';
  $('route-from').textContent = active.name;
  $('route-to').textContent = target?.name || (view.action==='wait_reset'?'Chờ reset':view.action === 'keep' ? 'Giữ phiên' : 'Chờ dữ liệu');
  const ranking = $('ranking'); ranking.replaceChildren();
  view.ranking.forEach((id, index) => {
    const a = account(id), row = node('div', 'rank');
    row.append(node('span', 'rank-number', `0${index + 1}`), node('span', '', a.name),
      node('span', 'rank-score', `${pct(a.bottleneck_remaining)} dư địa · ${a.score}/100 điểm${a.reset_bonus ? ` · lợi thế reset ${a.reset_bonus}` : ''}`));
    ranking.append(row);
  });
  if (!view.ranking.length) ranking.append(node('p', 'method-note', 'Chưa có ứng viên đã xác nhận.'));
  $('preview').disabled = false;
  updateClock();
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
function preview() {
  if (!view) return;
  const box = $('plan-content'); box.replaceChildren();
  box.append(node('p', '', view.demo ? 'Đây là kế hoạch mô phỏng. Không đổi account, gửi prompt hoặc thực hiện tác vụ thật.' : 'Đây chỉ là kế hoạch. Adapter desktop và VS Code chưa được xác nhận; không đổi tài khoản thật.'));
  const steps = view.action==='wait_reset'?['Giữ account hiện tại; chưa compact hay handoff.','Chờ cửa sổ quota đang hạn chế reset trong vòng 5 phút.','Đọc quota mới để xác nhận. Nếu quota chưa hồi, đánh giá lại điều kiện chuyển.']:view.action === 'keep' ? ['Giữ phiên hiện tại.', 'Theo dõi quota ở cả hai cửa sổ.', 'Tạo checkpoint ở ranh giới tác vụ khi tích hợp được kết nối.'] : view.action === 'pause' ? ['Dừng lập lịch tác vụ mới.', 'Đọc lại quota đã xác nhận hoặc đợi reset.', 'Không tự thử qua lại giữa các tài khoản.'] : [
    `Ngừng nhận tác vụ mới trên desktop và VS Code của ${account(view.active).name}.`,
    'Đợi công cụ đang chạy kết thúc; kiểm tra các tác động chưa rõ.',
    'Tạo bản compact và lưu file handoff cho từng chat. Nếu thất bại, dừng chuyển.',
    `Đọc lại quota của ${account(view.target).name}; chỉ chuyển nếu vẫn đủ dư địa.`,
    'Đổi tài khoản và xác nhận danh tính trên cả desktop lẫn VS Code.',
    'Nạp handoff; kiểm tra trạng thái file và hành động trước khi tiếp tục.'
  ];
  const list = node('ol'); steps.forEach(text => list.append(node('li', '', text))); box.append(list);
  $('plan-dialog').showModal();
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
for (const id of ['reserve','margin']) {
  $(id).addEventListener('input', event => $(`${id}-value`).textContent = event.target.value);
  $(id).addEventListener('change', event => request('/api/settings', {...view.settings, [id]: Number(event.target.value)}));
}
for (const button of document.querySelectorAll('[data-strategy]')) button.addEventListener('click', () => request('/api/settings', {...view.settings, strategy: button.dataset.strategy}));
$('preview').addEventListener('click', preview);
$('close-dialog').addEventListener('click', () => $('plan-dialog').close());
$('done').addEventListener('click', () => $('plan-dialog').close());
$('download').addEventListener('click', () => {
  const {csrf, ...safe} = view;
  const blob = new Blob([JSON.stringify({...safe, executed:false}, null, 2)], {type:'application/json'});
  const url = URL.createObjectURL(blob), link = node('a'); link.href = url; link.download = 'relay-rotation-plan.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
setInterval(updateClock, 1000);
setInterval(()=>{if(!busy && view?.accounts.some(a=>a.refreshing))request('/api/state');},2000);
setInterval(() => { if (!busy && !$('plan-dialog').open) request('/api/state'); }, 30000);
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
    button.addEventListener('click', () => accountOperation('/api/accounts/switch',{identity:profile.id}));
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
    $('account-message').textContent = result.removed ? 'Đã gỡ profile khỏi Relay; đăng nhập local không thay đổi.' : path.endsWith('/update') ? 'Đã lưu alias.' : result.restart_required ? 'Đã cập nhật file đăng nhập. Mở lại hai client và kiểm tra account hiển thị. Quota đang được đọc lại cho các profile thật.' : 'Đã lưu profile. Chưa đổi đăng nhập.';
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

let rotationState=null, rotationBusy=false;
const rotationLabels={idle:'Sẵn sàng',waiting_reset:'Chờ quota reset',queued:'Đã xếp lịch',waiting_clients_closed:'Chờ đóng client',preflight:'Xác minh',compacting:'Đang compact',exporting:'Đang tạo handoff',exported:'Đã export',switching:'Đang đổi account',importing:'Đang import',imported:'Đã import',continuing:'Đang tiếp tục',complete:'Hoàn tất',checkpoint_complete:'Đã lưu checkpoint',failed:'Đã dừng do lỗi',cancelled:'Đã hủy',recovery_required:'Cần phục hồi'};
function renderRotation(state){
  rotationState=state;
  const job=state.job||{},config=state.config||{};
  $('rotation-phase').textContent=rotationLabels[job.phase]||job.phase;
  const stopped=['failed','recovery_required','cancelled'].includes(job.phase);
  $('rotation-message').textContent=(stopped?'Trạng thái lịch sử (không phải kiểm tra hiện tại): ':'')+(job.message||'Chưa có phiên rotate.');
  let clientStatus=$('rotation-client-status');
  if(!clientStatus){clientStatus=node('p','account-help');clientStatus.id='rotation-client-status';$('rotation-message').after(clientStatus);}
  clientStatus.hidden=!state.client_check;
  clientStatus.textContent=state.client_check?`Kiểm tra hiện tại: ${state.client_check.message}`:'';
  $('rotation-recovery').disabled=rotationBusy||state.busy||state.client_check?.clear===false;
  $('rotation-auto-toggle').checked=!!config.enabled;
  $('rotation-auto-toggle').disabled=rotationBusy||(!config.enabled&&(state.busy||['failed','recovery_required'].includes(job.phase)));
  $('rotation-cancel').disabled=rotationBusy||!state.busy;
  $('rotation-config-summary').textContent=`${config.workflow_ids?.length||config.threads?.length||0} luồng · Tự động: ${config.enabled?'bật':'tắt'} · Tiếp tục bằng Relay: ${config.continue_work?'bật':'tắt'}`+(state.file_auth_ready?'':` · Cần thiết lập cli_auth_credentials_store = \"file\" ở đầu ${state.codex_home}/config.toml và đăng nhập lại trước khi chạy.`);
  for(const id of ['rotation-start','rotation-checkpoint','rotation-settings'])$(id).disabled=state.busy||rotationBusy;
  const missing=(state.managed_threads||[]).filter(flow=>(config.workflow_ids||[]).includes(flow.id)&&!flow.bindings[state.active_account]);
  let mappingStatus=$('rotation-mapping-status');
  if(!mappingStatus){mappingStatus=node('p','account-help');mappingStatus.id='rotation-mapping-status';$('rotation-config-summary').after(mappingStatus);}
  mappingStatus.hidden=!missing.length;
  mappingStatus.textContent=missing.length?`Chưa thể rotate: ${missing.map(flow=>flow.label).join(', ')} chưa có thread trên account hiện tại. ${['failed','recovery_required'].includes(job.phase)?'Đóng client và chọn “Xác nhận sau phục hồi”, sau đó ':''}mở luồng bên dưới và chọn “Nạp handoff còn thiếu”.`:'';
  if(missing.length)for(const id of ['rotation-start','rotation-checkpoint'])$(id).disabled=true;
  $('rotation-recovery').hidden=!['failed','cancelled','recovery_required'].includes(job.phase);
  const artifacts=$('rotation-artifacts');artifacts.replaceChildren();
  if(job.failed_phase)artifacts.append(node('p','account-help',`Dừng tại bước: ${rotationLabels[job.failed_phase]||job.failed_phase}. ${job.rolled_back?'Đã xác minh hoàn nguyên account.':'Kiểm tra account trước khi tiếp tục.'}`));
  if(job.directory)artifacts.append(node('p','account-help',`Thư mục handoff / journal: ${job.directory}`));
  for(const h of job.handoffs||[]){
    const link=node('a','button secondary handoff-artifact',`↓ ${h.file}`);
    link.href='/api/rotation/handoff?name='+encodeURIComponent(h.file);link.download=h.file;artifacts.append(link);
  }
  const logBox=$('relay-log-output');
  const logText=(state.logs||[]).map(row=>`${new Date(row.time*1000).toLocaleString('vi-VN')} [${row.level.toUpperCase()}] [${row.job_id?.slice(0,8)||'server'}] ${rotationLabels[row.phase]||row.phase}${row.context?` · Account: ${row.context.account_name||row.context.account_id||'—'} · Thread: ${row.context.alias||'—'} (${row.context.thread_id||'đang tạo'})`:(row.source_account?.id||row.target_account?.id)?` · ${row.source_account?.name||'—'} → ${row.target_account?.name||'—'}`:''}: ${row.message}`).join('\n');
  if(logBox.textContent!==logText){logBox.textContent=logText||'Chưa có log.';if($('relay-log-follow').checked)logBox.scrollTop=logBox.scrollHeight;}
  $('relay-log-status').textContent=state.busy?`Đang chạy: ${rotationLabels[job.phase]||job.phase}`:`Hiện tại: ${rotationLabels[job.phase]||job.phase} · ${(state.logs||[]).length} mốc đã lưu`;
  renderManagedWorkflows(state);
  for(const imported of job.imports||[]){
    const line=node('p','handoff-artifact',`${imported.imported?'Đã import':'Chưa xác nhận import'} · ${imported.label||'Luồng công việc'} · ${imported.reused?'Dùng lại thread':'Tạo thread'}: `);
    line.append(node('code','',imported.id));line.append(node('small','',` · ${imported.cwd}`));artifacts.append(line);
  }
}
async function pollRotation(){
  if(!view?.live||rotationBusy)return;
  try{renderRotation(await accountAPI('/api/rotation'));}
  catch(error){$('rotation-message').textContent=error.message;}
}
async function rotationAction(path,data={}){
  if(rotationBusy)return;
  rotationBusy=true;
  let failure=null;
  try{renderRotation(await accountAPI(path,data));}
  catch(error){failure=error.message;}
  finally{rotationBusy=false;if(rotationState)renderRotation(rotationState);if(failure)$('rotation-message').textContent=failure;}
}
$('rotation-settings').addEventListener('click',async()=>{
  if(!view?.live)return;
  try{
    const [state,result]=await Promise.all([accountAPI('/api/rotation'),accountAPI('/api/rotation/sessions')]);
    renderRotation(state);
    const selectedFlows=new Set(state.config.workflow_ids||[]),selected=new Set(state.config.threads);
    const known=new Set(),workflows=$('rotation-workflows');workflows.replaceChildren();
    for(const flow of state.managed_threads||[]){
      for(const binding of Object.values(flow.bindings))known.add(binding.thread_id);
      const binding=flow.bindings[state.active_account];
      const label=node('label','rotation-session'),checkbox=node('input');checkbox.type='checkbox';checkbox.value=flow.id;
      checkbox.checked=selectedFlows.has(flow.id);checkbox.disabled=!binding;
      const text=node('span','',flow.label);text.append(node('small','',binding?`Account hiện tại · ${binding.thread_id}`:'Chưa liên kết thread trên account hiện tại'));
      label.append(checkbox,text);workflows.append(label);
    }
    if(!(state.managed_threads||[]).length)workflows.append(node('p','','Chưa có luồng được quản lý. Chọn chat bên dưới để đăng ký.'));
    const list=$('rotation-sessions');list.replaceChildren();let available=0;
    for(const session of result.sessions){
      if(known.has(session.id))continue;
      available++;
      const label=node('label','rotation-session'),checkbox=node('input');checkbox.type='checkbox';checkbox.value=session.id;checkbox.checked=selected.has(session.id);
      const text=node('span','',session.title);text.append(node('small','',`${session.frontend} · ${session.cwd} · ${session.id}`));label.append(checkbox,text);list.append(label);
    }
    if(!available)list.append(node('p','','Không có chat local chưa đăng ký.'));
    $('rotation-auto').checked=state.config.enabled;$('rotation-continue').checked=state.config.continue_work;
    $('rotation-dialog-error').textContent='';$('rotation-dialog').showModal();
  }catch(error){$('rotation-message').textContent=error.message;}
});
$('rotation-close').addEventListener('click',()=>$('rotation-dialog').close());
$('rotation-save').addEventListener('click',async()=>{
  const threads=[...$('rotation-sessions').querySelectorAll('input:checked')].map(el=>el.value);
  const workflow_ids=[...$('rotation-workflows').querySelectorAll('input:checked')].map(el=>el.value);
  try{
    renderRotation(await accountAPI('/api/rotation/settings',{threads,workflow_ids,enabled:$('rotation-auto').checked,continue_work:$('rotation-continue').checked}));
    $('rotation-dialog').close();
  }catch(error){$('rotation-dialog-error').textContent=error.message;}
});
let confirmedRotationMode=null;
async function confirmRotation(mode){
  try{
    const state=await accountAPI('/api/rotation');renderRotation(state);
    if(state.busy)throw new Error('Đang có phiên chạy.');
    const flows=(state.managed_threads||[]).filter(flow=>(state.config.workflow_ids||[]).includes(flow.id));
    if(!flows.length)throw new Error('Chọn chat và lưu cấu hình trước khi chạy.');
    if(flows.some(flow=>!flow.bindings[state.active_account]))throw new Error('Có workflow chưa có thread nguồn. Nạp handoff còn thiếu trong Luồng công việc được quản lý trước khi chạy.');
    confirmedRotationMode=mode;
    $('rotation-restart-desktop').checked=false;
    $('rotation-confirm-title').textContent=mode==='rotate'?'Xác nhận compact, tạo handoff và chuyển account':'Xác nhận compact thật và tạo handoff mới';
    const box=$('rotation-confirm-content');box.replaceChildren();
    const name=state.accounts?.find(account=>account.id===state.active_account)?.name||state.active_account||'Chưa xác định';
    box.append(node('p','',`Account nguồn: ${name}`));
    const list=node('ul');
    for(const flow of flows)list.append(node('li','',`${flow.label} · ${flow.bindings[state.active_account]?.thread_id||'Chưa có thread nguồn'} · ${flow.cwd}`));
    box.append(list);
    const steps=node('ol');
    const descriptions=['Chờ client đóng; xác minh account và quota.','Sao lưu dữ liệu, compact từng thread và xuất handoff Markdown.'];
    if(mode==='rotate')descriptions.push('Chọn account đích đủ quota theo chính sách hiện tại; xác minh lại trước khi đổi.','Đổi đăng nhập, xác minh account đích; dùng lại thread liên kết hoặc tạo mới, đồng bộ alias và import toàn bộ context.',state.config.continue_work?'Sau import, Relay chạy tiếp một lượt: có thể sửa file trong project, tắt mạng.':'Sau import, mở client để tiếp tục thủ công.');
    else descriptions.push('Kết thúc tại checkpoint; giữ nguyên account, không import hoặc chạy tiếp.');
    for(const text of descriptions)steps.append(node('li','',text));box.append(steps);
    $('rotation-confirm-dialog').showModal();
  }catch(error){$('rotation-message').textContent=error.message;}
}
$('rotation-start').addEventListener('click',()=>confirmRotation('rotate'));
$('rotation-checkpoint').addEventListener('click',()=>confirmRotation('checkpoint'));
$('rotation-confirm-cancel').addEventListener('click',()=>{confirmedRotationMode=null;$('rotation-confirm-dialog').close();});
$('rotation-confirm-run').addEventListener('click',()=>{
  const mode=confirmedRotationMode;confirmedRotationMode=null;$('rotation-confirm-dialog').close();
  if(mode)rotationAction('/api/rotation/start',{mode,restart_desktop:$('rotation-restart-desktop').checked});
});
$('force-switch').addEventListener('click',async()=>{
  try{
    const state=await accountAPI('/api/rotation');renderRotation(state);
    const select=$('force-switch-target');select.replaceChildren();
    for(const account of state.accounts||[]){if(account.id===state.active_account)continue;const option=node('option','',account.name);option.value=account.id;select.append(option);}
    if(!select.options.length)throw new Error('Chưa có account khác.');
    if(state.busy)throw new Error('Đợi phiên hiện tại kết thúc.');
    $('force-switch-error').textContent='';$('force-switch-dialog').showModal();
  }catch(error){$('rotation-message').textContent=error.message;}
});
$('force-switch-cancel').addEventListener('click',()=>$('force-switch-dialog').close());
$('force-switch-confirm').addEventListener('click',async()=>{
  const button=$('force-switch-confirm');button.disabled=true;
  try{
    await accountAPI('/api/accounts/switch',{identity:$('force-switch-target').value});
    $('force-switch-dialog').close();await request('/api/state');await pollRotation();
  }catch(error){$('force-switch-error').textContent=error.message;}
  finally{button.disabled=false;}
});
$('rotation-auto-toggle').addEventListener('change',()=>rotationAction('/api/rotation/automatic',{enabled:$('rotation-auto-toggle').checked}));
$('rotation-cancel').addEventListener('click',()=>{if(confirm('Dừng phiên hiện tại và tắt tự động? Các bước đã hoàn tất và handoff được giữ; không hoàn tác sửa file project.'))rotationAction('/api/rotation/cancel');});
$('rotation-recovery').addEventListener('click',()=>rotationAction('/api/rotation/recovery'));
setInterval(pollRotation,3000);
pollRotation();

function renderManagedWorkflows(state){
  const box=$('managed-workflows');
  if(box.contains(document.activeElement)&&['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName))return;
  const opened=new Set([...box.querySelectorAll('details[open]')].map(el=>el.dataset.workflow));
  box.replaceChildren();
  box.append(node('h3','','Luồng công việc được quản lý'));
  if(!(state.managed_threads||[]).length){box.append(node('p','account-help','Chọn chat và lưu cấu hình để tạo luồng có nhãn, theo dõi account–thread và lịch sử rotate.'));return;}
  const accountName=id=>state.accounts?.find(a=>a.id===id)?.name||id||'Chưa chọn';
  for(const flow of state.managed_threads){
    const details=node('details','managed-workflow');details.dataset.workflow=flow.id;details.open=opened.has(flow.id)||((state.config.workflow_ids||[]).includes(flow.id)&&!flow.bindings[state.active_account]);
    details.append(node('summary','',`${flow.label} · ${Object.keys(flow.bindings).length} account · ${flow.history.length} lần bàn giao gần nhất`));
    details.append(node('p','account-help',`Workflow ID: ${flow.id} · ${flow.cwd}`));
    for(const [account,binding] of Object.entries(flow.bindings)){
      const line=node('p','binding-row',`${accountName(account)}${account===state.active_account?' · đang chọn':''} · ${binding.state==='ready'?'Sẵn sàng':'Chờ xác nhận import'}`);
      const expectedName=`${flow.label} · ${accountName(account)}`;
      line.append(node('small','',`Tên Codex: ${expectedName} · ${binding.name_status==='synced'&&binding.synced_name===expectedName?'Đã đồng bộ':binding.name_status==='error'?'Lỗi đồng bộ tên':'Chờ đồng bộ khi client đóng'}`));line.append(node('small','',`Account ID: ${account}`));line.append(node('code','',binding.thread_id));details.append(line);
    }
    if(!flow.bindings[state.active_account]){
      const repair=node('button','button secondary','Nạp handoff còn thiếu');repair.disabled=state.busy||rotationBusy||['failed','recovery_required'].includes(state.job?.phase);
      repair.addEventListener('click',()=>{
        if(confirm(`Nạp handoff mới nhất đã lưu của “${flow.label}” vào ${accountName(state.active_account)}? Tạo thread và đồng bộ alias; không compact, đổi account hoặc chạy tiếp công việc.`))rotationAction('/api/rotation/start',{mode:'repair',workflow_id:flow.id});
      });details.append(repair);
    }
    const editor=node('div','runner-actions'),label=node('input');label.value=flow.label;label.maxLength=80;label.setAttribute('aria-label',`Alias chung ${flow.label}`);
    const rename=node('button','button secondary','Lưu alias chung');rename.disabled=state.busy;
    rename.addEventListener('click',()=>rotationAction('/api/rotation/workflow',{workflow_id:flow.id,label:label.value}));editor.append(label,rename);details.append(editor);
    const binder=node('div','runner-actions'),account=node('select');account.setAttribute('aria-label',`Account liên kết ${flow.label}`);
    for(const item of state.accounts||[]){const option=node('option','',item.name+(flow.bindings[item.id]?' · đã liên kết':''));option.value=item.id;account.append(option);}
    const thread=node('input');thread.placeholder='Thread ID local đã có';thread.setAttribute('aria-label',`Thread liên kết ${flow.label}`);
    const bind=node('button','button secondary','Liên kết thread có sẵn');bind.disabled=state.busy;
    bind.addEventListener('click',()=>rotationAction('/api/rotation/workflow',{workflow_id:flow.id,account_id:account.value,thread_id:thread.value.trim()}));binder.append(account,thread,bind);details.append(binder);
    if(flow.history.length){
      const history=node('ul','managed-history');
      for(const event of [...flow.history].reverse().slice(0,10)){
        const date=new Date(event.updated_at*1000).toLocaleString('vi-VN',{timeZone:'Asia/Tokyo'});
        const item=node('li','',`${date} · ${accountName(event.from_account)} → ${accountName(event.to_account)} · ${event.to_thread?(event.reused?'dùng lại thread':'tạo thread'):'chưa nạp context'} · ${event.status}`);
        history.append(item);
      }details.append(history);
    }
    const sync=node('button','button secondary','Đồng bộ tên Codex');sync.disabled=state.busy;sync.addEventListener('click',()=>rotationAction('/api/rotation/names'));details.append(sync);
    box.append(details);
  }
}

function routePage(){
  const login=location.hash==='#login';
  $('dashboard-page').hidden=login;
  $('account-dialog').hidden=!login;
  const active=login?'#login':location.hash||'#overview';
  for(const link of document.querySelectorAll('.sidebar nav a')){
    const selected=link.getAttribute('href')===active;
    link.classList.toggle('selected',selected);
    if(selected)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  }
  document.querySelector('.breadcrumb').textContent=login?'Workspace / Quản lý đăng nhập':'Workspace / Tổng quan';
  if(login){
    if(!view?.account_management){$('account-message').textContent='Quản lý đăng nhập cần dashboard live.';return;}
    if($('account-message').textContent==='Quản lý đăng nhập cần dashboard live.')$('account-message').textContent='';
    savedAccounts().then(pollLogin).catch(error=>$('account-message').textContent=error.message);
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
