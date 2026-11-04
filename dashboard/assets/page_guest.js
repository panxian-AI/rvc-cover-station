/* RVC 控制台 · guest —— 由 console_page.html 拆出（v2.0.0 多页面） */

const $ = s => document.querySelector(s);

function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('on');
  clearTimeout(window._t);window._t=setTimeout(()=>t.classList.remove('on'),2400);}

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

const SVC_NAME = {api:'后端 3333', webui:'WebUI 7865', ncm:'网易云 3002', panel:'面板 8899'};

const ST = {pending:'等批准', queued:'排队中', running:'制作中', done:'已完成', rejected:'已拒绝', error:'失败'};

const SC = {pending:'b-wait', queued:'b-q', running:'b-run', done:'b-done', rejected:'b-no', error:'b-no'};

/* ---------- 白名单 ---------- */
async function loadWl(){
  let d;
  try{ d = await (await fetch('/api/admin/wl', {cache:'no-store'})).json(); }
  catch(e){ $('#wlApps').innerHTML = '<div class="empty">读取失败</div>'; return; }
  if(!d.ok){ $('#wlApps').innerHTML = '<div class="empty">只能在本机管理</div>'; return; }
  const alerts = d.alerts || [];
  if(alerts.length){
    $('#wlApps').innerHTML = `<div style="background:#3a1d24;border:1px solid #5c2b36;border-radius:10px;
      padding:9px 11px;margin-bottom:8px;font-size:12.5px;color:#ff9aa8">
      ⚠ 有 ${alerts.length} 个白名单身份因异常被自动暂停（多设备/多网络/反复对不上）：<br>` +
      alerts.map(a => `· IP ${esc(a.ip)} — ${esc(a.reason)} <a class="link" href="#" onclick="wlState('${esc(a.gid)}','resume');return false">恢复</a>`).join('<br>') +
      `</div>`;
  }else{ $('#wlApps').innerHTML = ''; }
  const apps = (d.apps||[]).filter(a => a.state === 'pending');
  const done = (d.apps||[]).filter(a => a.state !== 'pending');
  $('#wlN').textContent = apps.length ? ('· ' + apps.length + ' 条待审') : '';
  if(!apps.length){
    $('#wlApps').innerHTML += '<div class="empty">暂时没有待审的白名单申请' +
      (done.length ? ('（已处理 ' + done.length + ' 条）') : '') + '</div>';
  }else{
    $('#wlApps').innerHTML += apps.map(a => `<div class="qrow">
      <div class="t" style="min-width:0;flex:1">
        <b style="font-size:13px">${esc(a.reason)}</b>
        <span>${esc(a.ua)||'-'}${a.country?' · '+esc(a.country):''} · IP ${esc(a.ip)} · 指纹 ${esc(a.fp_short)}</span>
        <span>历史：申请 ${a.gid_reqs} 次 · 被拒 ${a.gid_rejected} 次 · 提交于 ${esc(a.created)}${a.already_wl?' · <b style="color:#f0c674">该设备已在名单（换网络重新批）</b>':''}</span>
      </div>
      <button class="green" onclick="wlDecide('approve','${esc(a.id)}')">✓ 批准</button>
      <button class="red" onclick="wlDecide('reject','${esc(a.id)}')">拒绝</button>
    </div>`).join('');
  }
  const list = d.entries || [];
  $('#wlCount').textContent = list.length ? ('· ' + list.length + ' 人') : '';
  if(!list.length){
    $('#wlList').innerHTML = '<div class="empty">还没有人被加入白名单</div>';
  }else{
    $('#wlList').innerHTML = `<table><tr><th>批准时间</th><th>网络（IP）</th><th>设备</th><th>设备密钥</th><th>今日/上限</th><th>状态</th><th>备注</th><th>直通</th><th>最近活跃</th><th>操作</th></tr>` +
      list.map(w => `<tr>
        <td style="white-space:nowrap;color:#75869f">${esc(w.approved)}</td>
        <td>${esc(w.ip)}${w.country?'<div style="color:#75869f;font-size:11px">'+esc(w.country)+'</div>':''}</td>
        <td style="color:#75869f">${esc(w.ua)||'-'}</td>
        <td>${w.device_bound?'<span class="tag t-on">已绑定</span>':'<span class="tag t-off">仅指纹</span>'}</td>
        <td>${w.day_used}/${w.day_quota}</td>
        <td>${w.suspended?'<span class="tag t-off" style="color:#ff9aa8">已暂停</span>':'<span class="tag t-on">生效中</span>'}</td>
        <td style="color:#75869f;max-width:170px;word-break:break-all">${esc(w.note)||'-'}</td>
        <td>${w.hits}${w.active?' <span class="tag t-on">活跃</span>':''}</td>
        <td style="white-space:nowrap;color:#75869f">${esc(w.last_seen)||'—'}</td>
        <td style="white-space:nowrap">${w.suspended
          ? `<a class="link" href="#" onclick="wlState('${esc(w.gid)}','resume');return false">恢复</a> `
          : `<a class="link" href="#" onclick="wlState('${esc(w.gid)}','suspend');return false">暂停</a> `}<a class="link" href="#" onclick="wlRevoke('${esc(w.gid)}');return false">移除</a></td>
      </tr>`).join('') + `</table>`;
  }
}

async function wlState(gid, action){
  if(action === 'suspend' && !confirm('暂停这个身份的白名单权限？对方提交会回到人工审批。')) return;
  const r = await fetch('/api/admin/wl_state', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({gid, action})});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已处理' : '失败'));
  refresh();
}

/* ---------- 验证码解锁请求 ---------- */
async function loadUnlock(){
  let d;
  try{ d = await (await fetch('/api/admin/unlock', {cache:'no-store'})).json(); }
  catch(e){ return; }
  if(!d || !d.ok) return;
  const list = d.pending || [];
  $('#ulN').textContent = list.length ? ('· ' + list.length + ' 条待处理') : '';
  $('#ulBox').innerHTML = list.length
    ? list.map(u => `<div class="qrow">
        <div class="t" style="min-width:0;flex:1">
          <b style="font-size:13px">IP ${esc(u.ip)}</b>
          <span>已连错 ${u.streak} 次 · 请求于 ${Math.floor(u.age/60)} 分 ${u.age%60} 秒前</span>
          <span>同意 = 只解除当前这次锁定（错误计数保留，再错照常锁）</span>
        </div>
        <button class="green" onclick="unlockDecide('${esc(u.ip)}',1)">✓ 同意解锁</button>
        <button class="red" onclick="unlockDecide('${esc(u.ip)}',0)">拒绝</button>
      </div>`).join('')
    : '<div class="empty">暂时没有解锁请求。</div>';
}

async function unlockDecide(ip, allow){
  if(!allow && !confirm('拒绝这条解锁请求？对方 10 分钟内不能再发。')) return;
  const r = await fetch('/api/admin/unlock', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({ip, allow: !!allow})});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已处理' : '失败'));
  loadUnlock();
}

async function loadSec(){  let d;
  try{ d = await (await fetch('/api/admin/security', {cache:'no-store'})).json(); }
  catch(e){ return; }
  if(!d || !d.ok) return;
  const on = !d.paused;
  $('#secState').textContent = on ? '正常' : '公网已暂停';
  $('#secState').className = 'tag ' + (on ? 't-on' : 't-off');
  $('#btnPause').textContent = on ? '⏸ 暂停公网访问' : '▶ 恢复公网访问';
  const bans = d.banned || [];
  $('#secBan').innerHTML = bans.length
    ? `<div style="color:#ff9aa8;font-size:12.5px">已封禁 ${bans.length} 个来源：</div>` + bans.map(b =>
        `<div class="row" style="padding:5px 0;border:0"><span class="tag">${esc(b.ip)}</span>
         <span style="color:#75869f;font-size:12px">剩余 ${Math.ceil(b.left/60)} 分钟</span>
         <span class="sp"></span><a class="link" href="#" onclick="secUnban('${esc(b.ip)}');return false">解除</a></div>`).join('')
    : '<div style="color:#75869f;font-size:12.5px">没有被封禁的来源。</div>';
  const rec = d.recent || [];
  $('#secRecent').innerHTML = rec.length
    ? `累计拦下 ${d.probe_total} 次探测 · 最近：` + rec.slice(0,4).map(r =>
        `<code>${esc(r.t)} ${esc(r.ip)} → ${esc(r.path)}</code>`).join('　')
    : '还没有探测记录（公网只会看到访客页，其它路径一律当作不存在）。';
}

async function secPause(){
  const paused = $('#secState').textContent !== '正常';
  const act = paused ? 'resume' : 'pause';
  if(act === 'pause' && !confirm('暂停后公网访客会看到维护页（本机控制台不受影响）。继续？')) return;
  const r = await fetch('/api/admin/security', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({act})});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已处理' : '失败'));
  loadSec();
}

async function secUnban(ip){
  const r = await fetch('/api/admin/security', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({act:'unban', ip})});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已解除' : '失败'));
  loadSec();
}

async function wlDecide(action, id){
  if(action === 'reject' && !confirm('拒绝这条白名单申请？（对方 24 小时内不能再申请）')) return;
  const note = action === 'approve' ? (prompt('备注（可选，会显示在你的名单里）：') || '') : '';
  const r = await fetch('/api/admin/wl_decide', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({id, action, note})});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已处理' : '失败'));
  refresh();
}

async function wlRevoke(gid){
  if(!confirm('移出白名单？对方立即失去免审批资格。')) return;
  const r = await fetch('/api/admin/wl_revoke', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({gid})});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已移除' : '失败'));
  refresh();
}

function refresh(){ loadWl(); loadUnlock(); loadSec(); }
refresh(); setInterval(refresh, 8000);
