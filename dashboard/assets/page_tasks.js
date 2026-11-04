/* RVC 控制台 · tasks —— 由 console_page.html 拆出（v2.0.0 多页面） */

/* $ / toast / esc / jsq 在 assets/common.js（页面里的 <script> 顺序保证先加载） */

const SVC_NAME = {api:'后端 3333', webui:'WebUI 7865', ncm:'网易云 3002', panel:'面板 8899'};

const ST = {pending:'等批准', queued:'排队中', running:'制作中', done:'已完成', rejected:'已拒绝', error:'失败'};

const SC = {pending:'b-wait', queued:'b-q', running:'b-run', done:'b-done', rejected:'b-no', error:'b-no'};

let ALL = [];

/* ---------- 队列 ---------- */
async function loadQueue(){
  let d;
  try{ d = await (await fetch('/api/admin/queue', {cache:'no-store'})).json(); }
  catch(e){ $('#queue').innerHTML = '<div class="empty">读取失败</div>'; return; }
  if(!d.ok){ $('#queue').innerHTML = '<div class="empty">只能在本机管理</div>'; return; }
  const n = d.total || 0;
  const runTitles = (d.running_all && d.running_all.length) ? d.running_all
                                                             : (d.running ? [d.running] : []);
  const runs = d.running_detail || [];
  const cap = d.cap || 1;
  const avgTxt = d.avg_job_text ? (' · 单均约 ' + d.avg_job_text) : '';
  $('#qSum').textContent = (n ? ('· 排队 ' + n + ' 单') : '')
    + (runTitles.length ? (' · 正在做 ' + runTitles.length + '/' + cap + ' 单') : '');
  let html = `<div class="qbar">正在做：<b>${runTitles.length ? runTitles.map(esc).join(' / ') : '（空闲）'}</b>
    · 排队 ${n} 单 · 同时上限 ${cap} 单${avgTxt}${d.cooldown ? `　<span style="color:#f0c674">冷却 ${d.cooldown}s</span>` : ''}
    <button class="gray mini" style="margin-left:auto" onclick="pumpNow()">▶ 立即开始下一单</button></div>`;
  // 正在做的每一单单独一行：阶段 + 已用 + 预计剩余，比只列标题有用得多
  if(runs.length){
    html += runs.map(r => `<div class="qrow qrun">
      <span class="pos">▶</span>
      <div class="t"><b>${r.type==='tts'?'🗣':'🎤'} ${esc(r.title)||'(未命名)'}</b>
        <span>${esc(r.stage_label || '制作中')}${r.acapella?' · 清唱':''}${r.model?(' · '+esc(r.model)):''}
          · 已用 ${esc(r.elapsed_text || '0 秒')}${!r.left_text?'' : (r.left_text==='快好了' ? ' · 快好了' : ' · 预计还剩约 '+esc(r.left_text))}</span></div>
      ${r.pct ? `<div class="pbar" title="按最近成品耗时估算"><span style="width:${Math.max(3,Math.min(92,r.pct))}%"></span></div>` : ''}
    </div>`).join('');
  }
  if(!n){
    html += '<div class="empty">队列是空的 · 批准后会自动排到这里</div>';
  }else{
    html += d.queue.map(q => `<div class="qrow ${q.pos===1?'qrun':''}">
      <span class="pos">#${q.pos}</span>
      <div class="t"><b>${q.type==='tts'?'🗣':'🎤'} ${esc(q.title)}</b>
        <span>${esc(q.artist)||'-'}${q.acapella?' · 清唱':''} · 提交 ${esc(q.created)}
          · <b style="color:#c4b5fd">${q.q_ahead ? ('前面 '+q.q_ahead+' 单') : '下一个就做'}</b>${q.eta_text?` · <span class="eta">预计约 ${esc(q.eta_text)}开始</span>`:''}</span></div>
      <button class="gray" onclick="moveQ('${jsq(q.id)}','top')" ${q.pos===1?'disabled':''}>⤒</button>
      <button class="gray" onclick="moveQ('${jsq(q.id)}','up')" ${q.pos===1?'disabled':''}>↑</button>
      <button class="gray" onclick="moveQ('${jsq(q.id)}','down')" ${q.pos===d.total?'disabled':''}>↓</button>
      <button class="red" onclick="decide('reject','${jsq(q.id)}')">移出</button>
    </div>`).join('');
  }
  $('#queue').innerHTML = html;
}

async function moveQ(id, dir){
  const r = await fetch('/api/admin/move', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id, dir})});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已调整' : '调整失败'));
  refresh();
}

async function pumpNow(){
  const r = await fetch('/api/admin/pump', {method:'POST', headers:{'Content-Type':'application/json'},
    body: '{}'});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已推进队列' : '推进失败'));
  refresh();
}

/* ---------- 待审批 ---------- */
/* ---------- 自助翻唱任务（自己用，不走访客队列） ---------- */
async function loadCoverJobs(){
  let d;
  try{ d = await (await fetch('/api/cover/jobs', {cache:'no-store'})).json(); }
  catch(e){ $('#cjobs').innerHTML = '<div class="empty">读取失败</div>'; return; }
  if(!d.ok){ $('#cjobs').innerHTML = '<div class="empty">只能在本机查看</div>'; return; }
  const run = d.running || [], qs = d.queued || [], rec = d.recent || [];
  $('#cjSum').textContent = (run.length ? (' · 正在做 ' + run.length + '/' + (d.cap||1)) : '')
    + (qs.length ? (' · 排队 ' + qs.length) : '')
    + ((run.length || qs.length) ? '' : ' · 空闲');
  const sub = j => esc(j.model || '-') + (j.key_shift ? (' · 升调 ' + j.key_shift) : '')
    + (j.acapella ? ' · 清唱' : '');
  let html = '';
  if(run.length){
    html += run.map(j => `<div class="qrow qrun">
      <span class="pos">▶</span>
      <div class="t"><b>🎤 ${esc(j.title)}</b>
        <span>${sub(j)} · ${esc(j.stage || '制作中')} · 已用 ${Math.round(j.elapsed)}s${j.left_text?((j.left_text==='快好了'?' · 快好了':' · 预计还剩约 '+esc(j.left_text))):''}</span></div>
      ${j.pct ? `<div class="pbar" title="按最近成品耗时估算"><span style="width:${Math.max(3,Math.min(92,j.pct))}%"></span></div>` : ''}
    </div>`).join('');
  }else{
    html += '<div class="empty">现在没有在做的自助翻唱</div>';
  }
  if(qs.length){
    html += qs.map((j, i) => `<div class="qrow">
      <span class="pos">#${i+1}</span>
      <div class="t"><b>🎤 ${esc(j.title)}</b><span>${sub(j)} · 排队等 GPU</span></div>
    </div>`).join('');
  }
  if(rec.length){
    html += '<div class="hint" style="margin:10px 0 4px">最近完成</div>' + rec.map(j => {
      const ok = j.state === 'done';
      const tail = ok
        ? `<a class="link" href="/api/cover/file?name=${encodeURIComponent(j.file||'')}" target="_blank">▶ 试听</a>`
        : `<span style="color:#f87171;font-size:12px">${esc(j.error || '失败')}</span>`;
      return `<div class="qrow" style="opacity:.85">
        <span class="pos">${ok ? '✓' : '!'}</span>
        <div class="t"><b>🎤 ${esc(j.title)}</b><span>${sub(j)} · 用时 ${Math.round(j.elapsed)}s</span></div>
        ${tail}
      </div>`;
    }).join('');
  }
  $('#cjobs').innerHTML = html;
}

async function loadPend(){
  let d;
  try{ d = await (await fetch('/api/admin/requests?state=pending', {cache:'no-store'})).json(); }
  catch(e){ $('#pend').innerHTML = '<div class="empty">读取失败</div>'; return; }
  if(!d.ok){ $('#pend').innerHTML = '<div class="empty">只能在本机管理</div>'; return; }
  const list = d.requests || [];
  $('#pendN').textContent = list.length ? ('· ' + list.length) : '';
  if(!list.length){ $('#pend').innerHTML = '<div class="empty">暂时没有待批的单子</div>'; return; }
  $('#pend').innerHTML = list.slice(0,8).map(r => `<div class="req">
      <div class="t">
        <div class="n">${r.type==='tts'?'🗣':'🎤'} ${esc(r.title)}</div>
        <div class="m">${esc(r.artist||'-')} · ${r.type==='tts'?esc(r.voice_label || (r.voice||'').replace(/Neural$/,'')):esc((r.model||'').replace(/\.pth$/i,''))}${r.acapella?' · 清唱':''}${r.reuse?' · 复用成品':''} · ${esc(r.ip||'')}${r.country?'·'+esc(r.country):''} · ${esc(r.created)}</div>
      </div>
      <button class="green mini" onclick="decide('approve','${jsq(r.id)}')">✓ 批准入队</button>
      <button class="red mini" onclick="decide('reject','${jsq(r.id)}')">拒绝</button>
    </div>`).join('');
}

/* ---------- 申请记录 ---------- */
async function loadReqs(){
  let d;
  try{ d = await (await fetch('/api/admin/requests?state=all', {cache:'no-store'})).json(); }
  catch(e){ $('#reqs').innerHTML = '<div class="empty">读取失败</div>'; return; }
  if(!d.ok){ $('#reqs').innerHTML = '<div class="empty">只能在本机管理</div>'; return; }
  ALL = d.requests || [];
  const c = d.counts || {};
  $('#reqN').textContent = ALL.length ? ('· 共 ' + ALL.length + ' 条 · 待批 ' + (c.pending||0)) : '';
  if(!ALL.length){ $('#reqs').innerHTML = '<div class="empty">还没有人提交过申请</div>'; return; }
  $('#reqs').innerHTML = `<table><tr><th>时间</th><th>内容</th><th>声线 / 音色</th><th>状态</th><th>操作</th></tr>` +
    ALL.slice(0, 20).map(r => {
      const isTts = r.type === 'tts';
      const cls = SC[r.state] || 'b-wait', st = ST[r.state] || r.state;
      const sub = isTts
        ? esc(r.voice_label || (r.voice||'').replace(/Neural$/,''))
        : (r.acapella?'清唱 · ':'') + esc((r.model||'').replace(/\.pth$/i,''));
      let op = '';
      if(r.state === 'done' && r.file)
        op = `<a class="link" href="/api/cover/file?name=${encodeURIComponent(r.file)}" target="_blank">▶ 试听</a>`;
      else if(r.state === 'error' || r.state === 'rejected')
        op = `<a class="link" href="#" onclick="decide('approve','${jsq(r.id)}');return false">重试</a>`;
      else if(r.state === 'pending')
        op = `<a class="link" href="#" onclick="decide('approve','${jsq(r.id)}');return false">批准</a>`;
      // 状态列不只写「排队中/制作中」，把位次和阶段也带上，一眼知道卡在哪
      let stHtml = `<span class="badge ${cls}">${esc(st)}</span>`;
      if(r.state === 'queued' && r.q_pos)
        stHtml += `<div style="color:#c4b5fd;font-size:11.5px;margin-top:4px">第 ${r.q_pos} 位${r.q_ahead?(' · 前 '+r.q_ahead+' 单'):' · 下一个就做'}</div>`;
      else if(r.state === 'running')
        stHtml += `<div style="color:#7fb4ff;font-size:11.5px;margin-top:4px">${esc(r.stage_label||'制作中')}${r.elapsed_text?(' · 已用 '+esc(r.elapsed_text)):''}</div>`;
      return `<tr>
        <td style="color:#75869f;white-space:nowrap">${esc(r.created)}</td>
        <td><b>${isTts?'🗣':'🎤'} ${esc(String(r.title||'').replace(/^朗读：/,''))}</b>
          ${r.artist?'<div style="color:#75869f;font-size:11.5px">'+esc(r.artist)+'</div>':''}</td>
        <td>${sub}${r.reuse?'<div style="color:#22c55e;font-size:11.5px">复用成品</div>':''}</td>
        <td>${stHtml}</td>
        <td>${op}</td></tr>`;
    }).join('') + `</table>`;
}

async function loadVisits(){
  let d;
  try{ d = await (await fetch('/api/admin/visits', {cache:'no-store'})).json(); }
  catch(e){ $('#visits').innerHTML = '<div class="empty">读取失败</div>'; return; }
  if(!d.ok){ $('#visits').innerHTML = '<div class="empty">只能在本机查看</div>'; return; }
  $('#visitN').textContent = d.total ? ('· ' + d.total + ' 位') : '';
  if(!d.visits.length){ $('#visits').innerHTML = '<div class="empty">还没有人来过</div>'; return; }
  $('#visits').innerHTML = `<table><tr><th>最近来访</th><th>来源 IP</th><th>地区</th>
    <th>设备</th><th>访问</th><th>搜索</th><th>申请</th></tr>` + d.visits.slice(0, 40).map(v => `<tr>
      <td style="white-space:nowrap;color:#75869f">${esc(v.last)}</td><td>${esc(v.ip)}</td>
      <td>${esc(v.country)||'-'}</td><td style="color:#75869f">${esc(v.ua)||'-'}</td>
      <td>${v.count}</td><td>${v.searches||0}</td><td>${v.requests}</td></tr>`).join('') + `</table>`;
}

/* ---------- 操作 ---------- */
async function decide(kind, id){
  if(kind==='reject' && !confirm('确定拒绝这条申请？')) return;
  const r = await fetch('/api/admin/' + kind, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
  const d = await r.json();
  toast(d.msg || (d.ok ? '已处理' : '失败'));
  refresh();
}

function refresh(){ loadQueue(); loadCoverJobs(); loadPend(); loadReqs(); loadVisits(); }
refresh(); setInterval(refresh, 5000);
