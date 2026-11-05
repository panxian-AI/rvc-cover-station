/* RVC 控制台 · overview —— 由 console_page.html 拆出（v2.0.0 多页面） */

/* $ / toast / esc / jsq 在 assets/common.js（页面里的 <script> 顺序保证先加载） */

const SVC_NAME = {api:'后端 3333', webui:'WebUI 7865', ncm:'网易云 3002', panel:'面板 8899'};

const ST = {pending:'等批准', queued:'排队中', running:'制作中', done:'已完成', rejected:'已拒绝', error:'失败'};

const SC = {pending:'b-wait', queued:'b-q', running:'b-run', done:'b-done', rejected:'b-no', error:'b-no'};

let PUB = '';

async function loadStatus(){
  try{
    const d = await (await fetch('/api/status', {cache:'no-store'})).json();
    $('#svc').innerHTML = (d.ports||[]).map(p => {
      let warn = '';
      if(p.key === 'ncm' && d.ncm_preflight && !d.ncm_preflight.cookie)
        warn = ' <span class="tag t-off" style="font-size:11px" title="双击 RVCSVC\\ncmapi\\设置网易云Cookie.bat 扫码登录，保存后自动生效">⚠ 未登录</span>';
      return `<div class="s"><span class="dot ${p.open?'on':'off'}"></span>${esc(SVC_NAME[p.key]||p.key)}${warn}</div>`;
    }).join('');
    const all = (d.ports||[]).filter(p=>p.key!=='panel').every(p=>p.open);
    $('#btnStart').disabled = all;
    const pf = d.ncm_preflight;
    const el = $('#ncmCfg');
    if(el) el.innerHTML = !pf || !pf.installed
      ? '<span style="color:#ff9aa8">网易云代理缺失（重装可修复）</span>'
      : (pf.cookie ? '✓ 已登录网易云，点歌搜索可用'
                   : '<span style="color:#f0c674">⚠ 网易云未登录：服务能起，但 VIP / 独家曲目搜不到。'
                     + '双击 <b>RVCSVC\\ncmapi\\设置网易云Cookie.bat</b>，用手机 App 扫码登录即可（保存后自动生效，不用重启）。</span>');
    const pl = $('#perfLine');
    if(pl && d.perf_text) pl.textContent = '性能档位：' + d.perf_text;
    const sl = $('#silentLine');
    if(sl){
      if(d.silent || d.tray){
        sl.style.display = '';
        sl.innerHTML = (d.silent ? '启动方式：<b>静默</b>（没有终端窗口，浏览器已自动打开）' : '启动方式：带窗口（排查模式）')
          + (d.tray
              ? ' · 任务栏图标已就绪：<b>左键单击呼出菜单</b>（可停服务、开目录），左键双击打开本页'
              : ' · <span style="color:#f0c674">任务栏图标没挂上</span>，停服务请双击根目录的 停止RVC.bat');
      }else{
        sl.style.display = 'none';
      }
    }
    const kl = $('#saveLine');
    if(kl){
      if(d.save_error){
        kl.style.display = '';
        kl.innerHTML = '<span style="color:#ff9aa8">⚠ 访客库写盘失败：</span>' + esc(d.save_error)
          + '<br>多半是磁盘满了或文件被别的程序占用。数据还在内存里，腾出空间后下一次写盘会自动重试。';
      }else{
        kl.style.display = 'none';
      }
    }
  }catch(e){ $('#svc').innerHTML = '<div class="s">状态读取失败</div>'; }}

/* ---------- 启动日志（静默模式下原本在黑窗口里的那些输出） ---------- */
let LOG_OPEN = false;

async function logToggle(){
  const pre = $('#logPre'), btn = $('#btnLog');
  LOG_OPEN = !LOG_OPEN;
  if(!LOG_OPEN){ pre.style.display='none'; btn.textContent='📜 启动日志'; return; }
  pre.style.display = '';
  btn.textContent = '📜 收起日志';
  await logLoad();
}

async function logLoad(){
  const pre = $('#logPre');
  if(!pre || !LOG_OPEN) return;
  try{
    const d = await (await fetch('/api/panel_log', {cache:'no-store'})).json();
    const body = (d.lines||[]).join('\n');
    pre.textContent = body || '（还没有日志：带窗口启动时输出在终端里，不在这个文件）';
    pre.scrollTop = pre.scrollHeight;
  }catch(e){ pre.textContent = '读取失败：' + e; }
}

async function loadPublic(){
  try{
    const d = await (await fetch('/api/public', {cache:'no-store'})).json();
    PUB = d.url || '';
    $('#pubUrl').textContent = PUB || '—（还没开隧道）';
    const on = !!PUB && d.alive;
    const el = $('#pubState');
    el.className = 'tag ' + (on ? 't-on' : 't-off');
    el.textContent = on ? '隧道运行中' : (PUB ? '上次的地址（隧道已断）' : '未开启');
    const bOn = $('#btnTunnelOn'), bOff = $('#btnTunnelOff');
    if(bOn) bOn.style.display = on ? 'none' : '';
    if(bOff) bOff.style.display = on ? '' : 'none';
  }catch(e){}
}

/* ---------- 公网隧道开关 ---------- */
async function tunnelAct(what){
  const bOn = $('#btnTunnelOn'), bOff = $('#btnTunnelOff');
  if(what === 'stop'){
    if(!confirm('关闭后公网地址立即失效（本机/局域网不受影响）。继续？')) return;
    bOff.disabled = true; bOff.textContent = '⏹ 关闭中…';
  }else{
    bOn.disabled = true; bOn.textContent = '🌐 开启中…（约 10 秒）';
  }
  try{
    const d = await (await fetch('/api/action', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action: 'tunnel_' + what})})).json();
    toast(d.msg || (d.ok ? '完成' : '失败'));
  }catch(e){ toast('请求失败：' + e.message); }
  if(bOn){ bOn.disabled = false; bOn.textContent = '🌐 开启公网'; }
  if(bOff){ bOff.disabled = false; bOff.textContent = '⏹ 关闭公网'; }
  loadPublic();
}

async function act(action){
  if(action==='stop_all' && !confirm('停掉后端 / 网易云 / WebUI？')) return;
  const btn = action==='start_all' ? $('#btnStart') : $('#btnStop');
  btn.disabled = true; btn.textContent = action==='start_all' ? '启动中…（约 1~2 分钟）' : '停止中…';
  try{
    const d = await (await fetch('/api/action', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({action})})).json();
    toast(d.msg || (d.ok ? '完成' : '失败'));
  }catch(e){ toast('请求失败：' + e.message); }
  btn.textContent = action==='start_all' ? '⚡ 一键开启服务' : '⏹ 全部停止（面板留着）';
  btn.disabled = false;
  loadStatus();
}

function copyUrl(){
  if(!PUB){ toast('还没有公网地址'); return; }
  navigator.clipboard?.writeText(PUB).then(()=>toast('已复制，发给朋友即可'),()=>toast(PUB));
}

function openUrl(){ if(PUB) window.open(PUB, '_blank'); else toast('还没有公网地址'); }

/* ---------- 版本检测 / 更新 ---------- */
let UPD = null, UPD_BUSY = false, UPD_POPPED = false;

async function updCheck(force){
  const st = $('#verState');
  st.className = 'vtag'; st.textContent = '检测中…';
  try{
    const d = await (await fetch('/api/update/check' + (force ? '?force=1' : ''),
                                {cache:'no-store'})).json();
    UPD = d;
    $('#verCur').textContent = 'v' + (d.current || '?');
    if(!d.ok){
      st.className = 'vtag'; st.textContent = '检测失败';
      $('#verNote').textContent = (d.error || d.msg || '连不上 GitHub') + '（不影响使用，稍后再试）';
      return;
    }
    $('#verLatest').textContent = 'v' + (d.latest || '?');
    if(d.has_update){
      const canAuto = d.can_auto_update !== false;
      st.className = 'vtag new';
      st.textContent = canAuto ? '有新版本' : '有新版本（需手动装）';
      $('#btnUpd').style.display = canAuto ? '' : 'none';
      const day = String(d.published_at || '').slice(0, 10);
      // 没有校验值就明说，别让「立即更新」变成一个装什么都不知道的按钮
      const safe = d.verified ? '安装前会逐字节核对 SHA-256'
                              : '发布页没公布校验值，只能手动下载安装包';
      $('#verNote').textContent = (d.name ? d.name + ' · ' : '') + (day ? '发布于 ' + day + ' · ' : '')
        + (canAuto ? '点「立即更新」先看更新内容，确认后只替换代码（不动模型和个人数据）；' : '')
        + safe;
      if(canAuto && !UPD_POPPED){ UPD_POPPED = true; updShowModal(); }   // 每次打开控制台弹一次
    }else{
      st.className = 'vtag'; st.textContent = '已是最新';
      $('#btnUpd').style.display = 'none';
      $('#verNote').textContent = '检测时间 ' + (d.checked_at || '')
        + (d.cached ? '（10 分钟内的缓存结果）' : '') + ' · 有新版本时会在这里提示';
    }
  }catch(e){
    st.className = 'vtag'; st.textContent = '检测失败';
    $('#verNote').textContent = '请求失败：' + e.message + '（不影响听歌和制作）';
  }
}

function mdInline(s){
  // 先转义再套格式：更新说明来自 GitHub，不能让里面的 HTML 生效
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank">$1</a>');
}

function mdRender(src){
  const lines = String(src || '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let para = [];
  let list = null;          // 'ul' | 'ol'
  const flushPara = () => {
    if(para.length){ out.push('<p>' + para.map(mdInline).join('<br>') + '</p>'); para = []; }
  };
  const flushList = () => {
    if(list){ out.push('</' + list + '>'); list = null; }
  };
  for(let i = 0; i < lines.length; i++){
    const raw = lines[i];
    const line = raw.trim();
    // 表格：|---| 分隔行
    if(line.startsWith('|') && (lines[i+1] || '').trim().match(/^\|[\s:|-]+\|$/)){
      flushPara(); flushList();
      const cells = t => t.replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      let html = '<table><tr>' + cells(line).map(c => '<th>' + mdInline(c) + '</th>').join('') + '</tr>';
      i += 2;
      while(i < lines.length && lines[i].trim().startsWith('|')){
        html += '<tr>' + cells(lines[i].trim()).map(c => '<td>' + mdInline(c) + '</td>').join('') + '</tr>';
        i++;
      }
      out.push(html + '</table>');
      i--;
      continue;
    }
    if(!line){ flushPara(); flushList(); continue; }
    if(/^(-{3,}|\*{3,})$/.test(line)){ flushPara(); flushList(); out.push('<hr>'); continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if(h){
      flushPara(); flushList();
      out.push('<h3>' + mdInline(h[2]) + '</h3>');
      continue;
    }
    const ul = line.match(/^[-*+]\s+(.*)$/);
    if(ul){
      flushPara();
      if(list !== 'ul'){ flushList(); out.push('<ul>'); list = 'ul'; }
      out.push('<li>' + mdInline(ul[1]) + '</li>');
      continue;
    }
    const ol = line.match(/^\d+[.)]\s+(.*)$/);
    if(ol){
      flushPara();
      if(list !== 'ol'){ flushList(); out.push('<ol>'); list = 'ol'; }
      out.push('<li>' + mdInline(ol[1]) + '</li>');
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return out.join('');
}

function updShowModal(){
  if(!UPD || !UPD.has_update){ toast('当前已经是最新版'); return; }
  $('#updTitle').textContent = '发现新版本 v' + UPD.latest + '（当前 v' + UPD.current + '）';
  $('#updMeta').textContent = (UPD.name || '') + (UPD.published_at
    ? ' · 发布于 ' + String(UPD.published_at).slice(0, 10) : '')
    + (UPD.verified ? ' · 安装包 SHA-256 ' + String(UPD.sha256).slice(0, 12) + '…' : '');
  const go = $('#updGo');
  if(go){
    // 没公布校验值就不给「一键装」，否则等于闭着眼睛覆盖代码
    go.disabled = UPD.can_auto_update === false;
    go.textContent = UPD.can_auto_update === false ? '需手动下载安装包' : '⬆ 立即更新';
  }
  $('#updBody').innerHTML = mdRender(UPD.notes || '（这一版没有写更新说明）');
  $('#updLink').href = UPD.url || '#';
  $('#updBody').scrollTop = 0;
  $('#updModal').classList.add('on');
}

function updClose(){ $('#updModal').classList.remove('on'); }

async function updApply(){
  if(UPD_BUSY) return;
  UPD_BUSY = true;
  const btn = $('#updGo');
  btn.disabled = true; btn.textContent = '更新中…（先测速选线，再下载替换）';
  try{
    const r = await fetch('/api/update/apply', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:'{}'});
    const d = await r.json();
    updClose();
    if(d.ok){
      const extra = d.source ? '\n\n下载线路：' + d.source
        + (d.speed_kbps ? '（实测约 ' + Math.round(d.speed_kbps) + ' KB/s）' : '') : '';
      const sha = d.verified && d.sha256
        ? '\n\n安装包 SHA-256 已核对：' + String(d.sha256).slice(0, 16) + '…' : '';
      alert(d.msg + sha + extra + (d.backup ? '\n\n改动前的文件已备份到：\n' + d.backup : ''));
      toast('已更新到 v' + (d.to || ''));
    }else{
      alert(d.msg || '更新失败');
    }
    updCheck(true);
  }catch(e){ toast('更新请求失败：' + e.message); }
  finally{ UPD_BUSY = false; btn.disabled = false; btn.textContent = '⬆ 立即更新'; }
}

/* ---------- 依赖版本（requirements.txt 锁的 vs 实际装的） ---------- */
async function loadDeps(){
  const el = $('#depsLine');
  if(!el) return;
  try{
    const d = await (await fetch('/api/deps', {cache:'no-store'})).json();
    if(d.missing_file){
      el.style.display = '';
      el.innerHTML = '<span style="color:#f0c674">⚠ 安装目录里缺 requirements.txt</span>：'
        + '依赖版本没法核对。重新跑一次一键安装、或把仓库里的 requirements.txt '
        + '放到 RVCSVC 目录下即可（不影响今天用）。';
      return;
    }
    const bad = (d.drift || []);
    if(!bad.length){ el.style.display = 'none'; return; }
    el.style.display = '';
    el.innerHTML = '<span style="color:#f0c674">⚠ 依赖版本和锁定值不一致：</span> '
      + bad.map(x => esc(x.name) + ' 装的 ' + esc(x.have) + '（应为 ' + esc(x.want) + '）').join('；')
      + '<br>多半是被别的程序顺手升过。修法：关掉面板，在安装目录执行<br><code>'
      + esc(d.repair || '') + '</code>';
  }catch(e){ el.style.display = 'none'; }
}

function refresh(){ loadStatus(); loadPublic(); loadDeps(); if(LOG_OPEN) logLoad(); }
refresh(); setInterval(refresh, 8000);
updCheck(false);          // 每次打开控制台都查一次（服务端 10 分钟缓存，不会刷爆额度）
