/* Standalone browser adapter. No network, filesystem, credential or app access. */
'use strict';
(() => {
  const number = n => typeof n === 'number' && Number.isFinite(n);
  const forbidden = new Set(['access_token','refresh_token','id_token','openai_api_key','authorization','password','client_secret','tokens','auth.json']);
  function metadata(value, depth = 0) {
    if (depth > 24) throw new Error('Snapshot quá sâu.');
    if (value && typeof value === 'object') {
      for (const [key, child] of Object.entries(value)) {
        if (forbidden.has(key.toLowerCase())) throw new Error('Chỉ nhận metadata quota; không nhập credentials.');
        metadata(child, depth + 1);
      }
    }
  }
  function options(raw = {}) {
    const result = {reserve:raw.reserve ?? 15, margin:raw.margin ?? 10, strategy:raw.strategy ?? 'reset_first', disabled:raw.disabled ?? []};
    if (!number(result.reserve) || result.reserve < 1 || result.reserve > 40 || !number(result.margin) || result.margin < 0 || result.margin > 20 || !['balanced','reset_first'].includes(result.strategy) || !Array.isArray(result.disabled) || result.disabled.length > 100 || result.disabled.some(k => typeof k !== 'string')) throw new Error('Cài đặt không hợp lệ.');
    return result;
  }
  function demo(now) {
    return {received_at:now, result:{schema_version:1,command:'list',active_account_key:'demo-1',accounts:[
      ['Studio',93,38,62],['Build',22,31,210],['Research',48,67,95],['Reserve',12,18,285]
    ].map(([name,short,week,reset],i) => ({account_key:`demo-${i+1}`,alias:name,auth_mode:'chatgpt',active:i===0,
      usage:{source:'api',refresh:{requested:true,method:'api',status:'ok'},
        primary:{used_percent:short,window_minutes:300,resets_at:now+reset*60},
        secondary:{used_percent:week,window_minutes:10080,resets_at:now+(3+i)*86400}}}))}};
  }
  function build(packet, raw = {}, now = Date.now()/1000, isDemo = false) {
    metadata(packet);
    const opts = options(raw), received = packet?.received_at, doc = packet?.result;
    if (!number(received) || received > now || now-received > 1e12 || doc?.schema_version !== 1 || doc.command !== 'list' || doc.error || !Array.isArray(doc.accounts) || (doc.active_account_key !== null && (typeof doc.active_account_key !== 'string' || !doc.active_account_key))) throw new Error('Snapshot không hợp lệ hoặc không đúng schema.');
    const seen = new Set(), threshold = 100-opts.reserve, entry = opts.strategy==='reset_first'?Math.min(70,Math.max(1,threshold-opts.margin)):Math.max(1,threshold-opts.margin);
    const accounts = doc.accounts.map(a => {
      if (!a || typeof a.account_key !== 'string' || !a.account_key || seen.has(a.account_key) || typeof a.active !== 'boolean' || a.active !== (a.account_key === doc.active_account_key)) throw new Error('Định danh hoặc account đang dùng không nhất quán.');
      seen.add(a.account_key);
      const usage = a.usage || {}, refresh = usage.refresh || {}, windows = [];
      let reason = '';
      if (typeof usage !== 'object' || Array.isArray(usage) || typeof refresh !== 'object' || Array.isArray(refresh)) throw new Error('Usage không hợp lệ.');
      if (a.auth_mode !== 'chatgpt') reason = 'Unsupported auth mode';
      else if (usage.source !== 'api' || refresh.requested !== true || refresh.method !== 'api' || refresh.status !== 'ok') reason = 'Refresh unavailable';
      for (const field of ['primary','secondary']) {
        const w = usage[field]; if (w == null) continue;
        if (!number(w.used_percent) || w.used_percent < 0 || w.used_percent > 100) { reason = 'Invalid window'; continue; }
        if (w.resets_at != null && (!number(w.resets_at) || w.resets_at <= now)) reason = 'Expired window';
        windows.push({used:w.used_percent,minutes:w.window_minutes,resets_at:w.resets_at});
      }
      if (!windows.length) reason = reason || 'Windows unavailable';
      const observed=usage.observed_at ?? received;
      if (!number(observed) || observed>now) reason='Invalid measurement timestamp';
      const excluded = opts.disabled.includes(a.account_key), stale = now-observed>60;
      const usable = !reason && !excluded && !stale;
      const eligible = usable && windows.every(w => w.used<entry);
      const keep = usable && windows.every(w => w.used<threshold);
      const bottleneck = windows.length ? 100-Math.max(...windows.map(w=>w.used)) : null;
      const short = windows.find(w=>w.minutes===300) || null, weekly=windows.find(w=>w.minutes===10080) || null;
      let bonus=0,score=eligible?bottleneck:null;
      if(eligible && opts.strategy==='reset_first' && short && weekly){
        const urgency=w=>number(w.resets_at)&&w.resets_at>now?Math.max(0,1-(w.resets_at-now)/(w.minutes*60)):0;
        const sr=100-short.used,wr=100-weekly.used;
        const rawBonus=wr*.20*urgency(weekly)+sr*.05*urgency(short);
        bonus=Math.round(rawBonus*100)/100;
        score=Math.round((wr*.45+sr*.20+bottleneck*.10+rawBonus)*100)/100;
      }
      return {id:a.account_key,name:typeof a.alias==='string' && a.alias ? a.alias:typeof a.email==='string' && a.email ? a.email:a.account_key,active:a.active,short,weekly,windows,eligible,excluded,bottleneck_remaining:bottleneck,score,reset_bonus:bonus,can_keep:keep,
        reason:excluded?'Đã tạm loại khỏi vòng rotate':stale?'Dữ liệu đã cũ; cần đọc quota mới':reason?'Quota chưa được xác nhận':!eligible?'Chưa đủ dư địa cho một phiên mới':'Đủ quota ở tất cả cửa sổ được báo cáo'};
    });
    const active=accounts.find(a=>a.active); if (!active && doc.active_account_key !== null) throw new Error('Không có account đang dùng.');
    const ranked=accounts.filter(a=>!a.active && a.eligible).sort((a,b)=>b.score-a.score || a.id.localeCompare(b.id));
    let waitUntil=null;
    const activeUsage=doc.accounts.find(a=>a.active)?.usage || {};
    if(active && !active.can_keep && !active.excluded && activeUsage.refresh?.status==='ok' && now-(activeUsage.observed_at ?? received)<=60){
      const limiting=active.windows.filter(w=>w.used>=threshold);
      if(limiting.length && limiting.every(w=>number(w.resets_at)&&w.resets_at>now&&w.resets_at-now<=300))waitUntil=Math.max(...limiting.map(w=>w.resets_at))+1;
    }
    const action=!active?'pause':active.can_keep?'keep':waitUntil?'wait_reset':ranked.length?'handoff':'pause';
    return {demo:isDemo,received_at:received,now,stale:now-received>60,accounts,active:active?.id || null,target:action==='handoff'?ranked[0].id:null,ranking:ranked.map(a=>a.id),action,reset_wait_until:waitUntil,settings:opts,threshold,entry_threshold:entry,execution_enabled:false,integration:{desktop:'unverified',vscode:'unverified'},csrf:'offline'};
  }
  let imported=null, currentOptions=options(), demoEnabled=false;
  globalThis.RELAY_LOCAL = {
    build, demo,
    async request(path,data) {
      try {
        const now=Date.now()/1000;
        if (path==='/api/settings') currentOptions=options(data);
        else if (path==='/api/import') { build(data,currentOptions,now); imported=data; }
        else if (path==='/api/demo') { imported=null; demoEnabled=true; }
        else if (path!=='/api/state') throw new Error('Unknown operation');
        const empty={received_at:now,result:{schema_version:1,command:'list',active_account_key:null,accounts:[]}};
        const result=build(imported || (demoEnabled ? demo(now) : empty),currentOptions,now,demoEnabled && imported===null);
        return {ok:true,async json(){return result;}};
      } catch(error) { return {ok:false,async json(){return {error:error.message};}}; }
    }
  };
})();
