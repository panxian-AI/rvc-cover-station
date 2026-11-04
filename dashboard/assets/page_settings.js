/* RVC 控制台 · settings —— 由 console_page.html 拆出（v2.0.0 多页面） */

/* $ / toast / esc / jsq 在 assets/common.js（页面里的 <script> 顺序保证先加载） */

const SVC_NAME = {api:'后端 3333', webui:'WebUI 7865', ncm:'网易云 3002', panel:'面板 8899'};

const ST = {pending:'等批准', queued:'排队中', running:'制作中', done:'已完成', rejected:'已拒绝', error:'失败'};

const SC = {pending:'b-wait', queued:'b-q', running:'b-run', done:'b-done', rejected:'b-no', error:'b-no'};

/* ---------- 模型导入 ---------- */
let IMP = null;

async function impPost(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify(body||{})});
  return await r.json();
}

function impRender(){
  const box = $('#impBox');
  if(!IMP){ box.innerHTML = '<div class="empty">还没有导入过模型</div>'; return; }
  box.innerHTML = IMP.items.map((it,i)=>{
    const icon = it.type==='pth' ? '🎙' : (it.type==='index' ? '🗂' : '❓');
    const head = icon + ' ' + esc(it.name) + (it.mb!=null ? ' · ' + it.mb + 'MB' : '');
    let detail = '';
    if(it.ok && it.type==='pth'){
      const bits = [];
      bits.push('采样率 ' + (it.sr || '?'));
      bits.push(it.version || '版本未知');
      bits.push(it.f0 === true ? 'f0 变调' : (it.f0 === false ? '非 f0' : 'f0 ?'));
      if(it.epoch) bits.push(it.epoch + ' 轮');
      if(it.params) bits.push(it.params + ' 个参数张量');
      detail = '<div style="color:#8fa3c2;font-size:12px;margin-top:2px">' + esc(bits.join(' · '))
             + (it.note ? ' · ' + esc(it.note) : '')
             + (it.author ? ' · by ' + esc(it.author) : '') + '</div>';
      if(it.match_index) detail += '<div style="color:#7fbf7f;font-size:12px">已自动配对索引：'
                                    + esc(it.match_index.split(/[\\/]/).pop()) + '</div>';
      else detail += '<div style="color:#f0c674;font-size:12px">这次没选到配套索引（音质会略差，可以之后单独导入 .index）</div>';
      if(it.warns && it.warns.length) detail += '<div style="color:#f0c674;font-size:12px">注意：' + esc(it.warns.join('；')) + '</div>';
    }
    const state = it.installed
      ? '<span style="color:#7fbf7f">✓ 已导入</span>'
      : (it.ok
          ? (it.type==='pth' ? '<button onclick="importInstall('+i+')">导入</button>'
                             : '<span style="color:#7fbf7f">✓ 索引有效</span>')
          : '<span style="color:#ff8f8f">✗ ' + esc(it.msg) + '</span>');
    return '<div style="border-bottom:1px solid #223;padding:8px 0">'
         + '<div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">'
         + '<b style="word-break:break-all">' + head + '</b><span style="white-space:nowrap">' + state + '</span></div>'
         + (!it.ok && it.msg ? '<div style="color:#ff8f8f;font-size:12px;margin-top:2px">' + esc(it.msg) + '</div>' : detail)
         + '</div>';
  }).join('');
}

async function importPick(){
  const box = $('#impBox');
  box.innerHTML = '<div class="empty">已弹出文件选择窗口…（选完自动开始识别）</div>';
  let p;
  try{ p = await impPost('/api/admin/import_pick'); }
  catch(e){ box.innerHTML = '<div class="empty">请求失败：' + esc(e.message) + '</div>'; return; }
  if(!p.ok){ box.innerHTML = '<div class="empty">' + esc(p.msg || '打开文件选择框失败') + '</div>'; return; }
  if(p.cancelled){ box.innerHTML = '<div class="empty">已取消，没有选择文件</div>'; return; }
  box.innerHTML = '<div class="empty">识别中…（大模型要读几秒钟）</div>';
  let c;
  try{ c = await impPost('/api/admin/import_check', {paths: p.paths}); }
  catch(e){ box.innerHTML = '<div class="empty">识别请求失败：' + esc(e.message) + '</div>'; return; }
  if(!c.ok){ box.innerHTML = '<div class="empty">' + esc(c.msg || '识别失败') + '</div>'; return; }
  IMP = c; impRender();
}

async function importInstall(i){
  const it = IMP.items[i];
  const rep = !!($('#impReplace') && $('#impReplace').checked);
  try{
    const d = await impPost('/api/admin/import_install',
                            {pth: it.path, index: it.match_index || null, replace: rep});
    if(d.ok){
      toast(d.msg + ' —— 直接点「🔄 更新声线」重启后端即可生效');
      it.installed = true; impRender(); loadStatus();
      try{ localStorage.setItem('rvcNeedRestart', it.name || ''); }catch(e){}
    }else if(d.exists){
      if(confirm(d.msg + '\n\n要覆盖吗？')){ $('#impReplace').checked = true; importInstall(i); }
    }else{
      toast(d.msg || '导入失败');
    }
  }catch(e){ toast('请求失败：' + e.message); }
}

/* ---------- 更新声线：就地重启 3333 后端（不用去模型市场） ---------- */
/* 3333 的模型列表是启动时读一次的，新装的声线要重启它才进点歌列表。
   判定标准与市场页一致：端口必须「先关后开」才算重启成功。 */

let IMPUPD = {running:false};

function impRstShow(kind, html){
  const b = $('#impRst');
  b.style.display = 'block';
  const bg = kind==='ok' ? '#10291f' : (kind==='bad' ? '#2a1414' : '#2a2313');
  const bd = kind==='ok' ? '#1f5340' : (kind==='bad' ? '#5c2626' : '#5c4a1f');
  const fg = kind==='ok' ? '#6fd39a' : (kind==='bad' ? '#ff9b9b' : '#f0c674');
  b.style.background = bg; b.style.border = '1px solid ' + bd; b.style.color = fg;
  b.innerHTML = html;
}

async function impRestartApi(){
  if(IMPUPD.running) return;
  const btn = $('#impUpdBtn');
  IMPUPD.running = true; btn.disabled = true;

  let wasOpen = null, webui = null;
  try{
    const s = await (await fetch('/api/status', {cache:'no-store'})).json();
    const p3333 = (s.ports||[]).find(x=>x.port===3333);
    wasOpen = p3333 ? !!p3333.open : null;
    webui = !!((s.ports||[]).find(x=>x.port===7865)||{}).open;
  }catch(e){}
  if(webui === false){
    impRstShow('bad', 'RVC WebUI(7865) 没在跑，此时重启 3333 也读不到声线列表。先点「⚡ 一键开启服务」，再回来点这里。');
    IMPUPD.running = false; btn.disabled = false; return;
  }

  const t0 = Date.now();
  const el = () => ((Date.now() - t0) / 1000).toFixed(0);
  impRstShow('', '正在重启 3333 后端…先停掉旧进程，再重新读模型列表（约 30~90 秒；正在生成的会被中断）');

  let r;
  try{
    r = await (await fetch('/api/action', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({do:'restart', target:'api'})})).json();
  }catch(e){ r = {ok:false, msg:e.message}; }
  if(!r || !r.ok){
    impRstShow('bad', '重启失败：' + esc((r && r.msg) || '面板没有响应') + '。可以用上面的服务开关手动重启 3333。');
    IMPUPD.running = false; btn.disabled = false; return;
  }

  let sawClosed = !wasOpen;   // 本来就没开，等它第一次出现即可
  for(let i = 0; i < 80; i++){
    await new Promise(rs => setTimeout(rs, 1500));
    let open = null;
    try{
      const s = await (await fetch('/api/status', {cache:'no-store'})).json();
      open = !!((s.ports||[]).find(x=>x.port===3333)||{}).open;
    }catch(e){ continue; }
    if(!open){
      if(!sawClosed){ sawClosed = true; impRstShow('', '旧进程已停止，正在重新启动并读取声线列表…（' + el() + ' 秒）'); }
      continue;
    }
    if(open && sawClosed){
      impRstShow('ok', '✅ 已更新（' + el() + ' 秒）：3333 后端重启完成，新声线现在就能点了。');
      try{ localStorage.removeItem('rvcNeedRestart'); }catch(e){}
      IMPUPD.running = false; btn.disabled = false;
      return;
    }
  }
  impRstShow('bad', sawClosed
    ? '等了 2 分钟 3333 还没起来。看服务开关旁的日志，或双击 停止RVC.bat 清干净后重新开启。'
    : '3333 一直没断过，旧进程可能没停掉。双击 停止RVC.bat 清干净，再「⚡ 一键开启服务」。');
  IMPUPD.running = false; btn.disabled = false;
}

/* ---------- 高级设置 ---------- */
let ADV = null;

function advField(f){
  const cur = (ADV.values && ADV.values[f.key] != null) ? ADV.values[f.key] : f.value;
  const label = '<div style="flex:1;min-width:160px"><b style="font-size:12.5px">' + esc(f.label) + '</b>'
    + '<div style="color:#75869f;font-size:11.5px;line-height:1.45;margin-top:2px">' + esc(f.hint) + '</div></div>';
  let input;
  if(f.type === 'enum'){
    input = '<select data-k="' + esc(f.key) + '" style="width:110px">'
      + f.choices.map(c => '<option value="' + esc(c) + '"' + (c === cur ? ' selected' : '') + '>'
                           + esc(c) + '</option>').join('') + '</select>';
  }else{
    const step = (f.step != null) ? f.step : 1;
    input = '<input type="number" data-k="' + esc(f.key) + '" value="' + esc(String(cur)) + '"'
      + ' min="' + f.min + '" max="' + f.max + '" step="' + step + '" style="width:92px">';
  }
  return '<div style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-bottom:1px solid #1d2738">'
       + label + input + '</div>';
}

function advRender(){
  const box = $('#advBox');
  if(!ADV){ box.innerHTML = '<div class="empty">还没读到设置</div>'; return; }
  const v = ADV.values || {};
  $('#advSum').textContent = '· 同时制作 ' + (v.max_parallel || 1) + ' 单 · '
    + (v.f0_method || 'rmvpe') + ' · 检索 ' + (v.index_rate != null ? v.index_rate : '');
  box.innerHTML = ADV.fields.map(advField).join('');
}

let ADV_DIRTY = false;      // 用户改过、还没保存

async function loadAdv(){
  try{
    const d = await (await fetch('/api/admin/settings', {cache:'no-store'})).json();
    if(!d.ok){ $('#advBox').innerHTML = '<div class="empty">只能在本机管理</div>'; return; }
    ADV = d;
    // 15 秒一次的定时刷新不能重建表单：用户改到一半（尤其输数字时停手
    // 超过 15 秒）会被服务端旧值覆盖，焦点也丢，看起来「改好了」其实没改。
    if(ADV_DIRTY || $('#advBox').contains(document.activeElement)) return;
    advRender();
  }catch(e){ if(!ADV) $('#advBox').innerHTML = '<div class="empty">读取失败</div>'; }
}

async function advPost(body){
  const r = await fetch('/api/admin/settings', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  return await r.json();
}

async function saveAdv(){
  ADV_DIRTY = false;
  const values = {};
  document.querySelectorAll('#advBox [data-k]').forEach(el => { values[el.dataset.k] = el.value; });
  try{
    const d = await advPost({values});
    toast(d.msg || (d.ok ? '已保存' : '保存失败'));
    if(d.ok){ if(d.values) ADV.values = d.values; advRender(); refresh(); }
  }catch(e){ toast('请求失败：' + e.message); }
}

async function resetAdv(){
  ADV_DIRTY = false;
  if(!confirm('把所有高级设置恢复成默认值？（同时制作数量会回到 1）')) return;
  try{
    const d = await advPost({act:'reset'});
    toast(d.msg || (d.ok ? '已恢复默认' : '恢复失败'));
    if(d.ok){ if(d.values) ADV.values = d.values; advRender(); refresh(); }
  }catch(e){ toast('请求失败：' + e.message); }
}

let DIO = null;

let DIO_COVERS = {indexed: 0, bytes: 0};

function dioTime(ts){
  const d = new Date((ts || 0) * 1000), p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
       + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function dioMsg(kind, html){
  const b = $('#dioRst');
  b.style.display = 'block';
  const c = kind === 'ok' ? ['#10291f', '#1f5340', '#6fd39a']
          : kind === 'bad' ? ['#2a1414', '#5c2626', '#ff9b9b']
          : ['#2a2313', '#5c4a1f', '#f0c674'];
  b.style.background = c[0]; b.style.border = '1px solid ' + c[1]; b.style.color = c[2];
  b.innerHTML = html;
}

function dioAudioHint(){
  const on = $('#dioAudio').checked;
  const mb = Math.round((DIO_COVERS.bytes || 0) / 1048576);
  $('#dioAudio').parentNode.lastChild.textContent =
    on ? ' 连成品音频一起打包（约 ' + mb + ' MB，会慢一些）' : ' 连成品音频一起打包';
}

async function dioLoad(){
  try{
    const d = await (await fetch('/api/data/summary', {cache:'no-store'})).json();
    if(!d.ok) throw new Error(d.msg || '读取失败');
    const c = d.counts || {}, cv = d.covers || {};
    DIO_COVERS = cv;
    $('#dioSum').textContent = '访客 ' + (c.visits||0) + ' · 申请 ' + (c.requests||0)
      + ' · 白名单 ' + (c.whitelist||0) + ' · 成品 ' + (cv.indexed||0) + ' 首';
    dioList(d.backups || [], d.total_text || '0 B');
    dioAudioHint();
  }catch(e){
    $('#dioSum').textContent = '读取失败：' + e.message;
  }
}

function dioList(bks, totalText){
  if(!bks.length){
    $('#dioList').innerHTML = '<div class="empty">还没有备份 —— 建议现在就点上面「⬇ 导出备份」存一份。</div>';
    return;
  }
  let h = '<div style="display:flex;justify-content:space-between;font-size:11.5px;color:#75869f;margin-bottom:5px;flex-wrap:wrap;gap:6px">'
        + '<span>已有 ' + bks.length + ' 份，共 ' + esc(totalText) + '</span>'
        + '<span>🕘 = 导入前自动存的，💾 = 手动导出的</span></div>';
  h += bks.map(b =>
    '<div style="display:flex;align-items:center;gap:9px;padding:6px 0;border-bottom:1px dashed #212b40;flex-wrap:wrap">'
    + '<span style="flex:1;min-width:190px;font-size:12.5px;word-break:break-all">'
    +   (b.auto ? '🕘' : '💾') + ' ' + esc(b.name) + '</span>'
    + '<span style="color:#75869f;font-size:11.5px;white-space:nowrap">'
    +   esc(b.size_text) + ' · ' + dioTime(b.mtime) + '</span>'
    + '<button class="gray mini" onclick=\'dioGet(this,' + JSON.stringify(b.name) + ')\'>下载</button>'
    + '<button class="red mini" onclick=\'dioDel(this,' + JSON.stringify(b.name) + ')\'>删</button>'
    + '</div>').join('');
  $('#dioList').innerHTML = h;
}

async function dioExport(){
  const btn = $('#dioExpBtn'), audio = $('#dioAudio').checked;
  btn.disabled = true; btn.textContent = '打包中…';
  dioMsg('wait', audio ? '正在打包（含成品音频，可能要几十秒）…' : '正在打包…');
  try{
    const d = await impPost('/api/data/export', {audio: audio});
    if(!d.ok){ dioMsg('bad', esc(d.msg || '导出失败')); return; }
    let extra = '';
    if(d.skipped && d.skipped.length){
      extra = '<br>没打进去的：' + d.skipped.map(x => esc(x.label + '（' + x.why + '）')).join('；');
    }
    dioMsg('ok', '已导出 <b>' + esc(d.name) + '</b>（' + esc(d.size_text) + '，用时 '
          + d.secs + ' 秒），浏览器正在下载。备份也存在：<br>' + esc(d.path) + extra);
    toast('已导出 ' + d.size_text);
    window.location.href = '/api/data/file?name=' + encodeURIComponent(d.name);
    dioLoad();
  }catch(e){
    dioMsg('bad', '请求失败：' + esc(e.message));
  }finally{
    btn.disabled = false; btn.textContent = '⬇ 导出备份';
  }
}

function dioGet(btn, name){
  window.location.href = '/api/data/file?name=' + encodeURIComponent(name);
}

async function dioDel(btn, name){
  if(!confirm('删掉这份备份？\n\n' + name + '\n\n删了就找不回来了（如果它正是你要恢复的那份）。')) return;
  btn.disabled = true;
  try{
    const d = await impPost('/api/data/delete', {name: name});
    toast(d.msg || (d.ok ? '已删除' : '删除失败'));
    dioLoad();
  }catch(e){ toast('请求失败：' + e.message); btn.disabled = false; }
}

async function dioOpen(){
  try{
    const d = await impPost('/api/data/open', {});
    toast(d.msg || (d.ok ? '已打开备份文件夹' : '打不开'));
  }catch(e){ toast('请求失败：' + e.message); }
}

async function dioPick(){
  const box = $('#dioBox');
  $('#dioRst').style.display = 'none';
  box.innerHTML = '<div class="empty">已弹出文件选择窗口…（选完自动读包）</div>';
  let p;
  try{ p = await impPost('/api/data/import_pick', {}); }
  catch(e){ box.innerHTML = '<div class="empty">请求失败：' + esc(e.message) + '</div>'; return; }
  if(!p.ok){ box.innerHTML = '<div class="empty">' + esc(p.msg || '打开文件选择框失败') + '</div>'; return; }
  if(p.cancelled){ box.innerHTML = '<div class="empty">已取消，没有选择文件</div>'; return; }
  box.innerHTML = '<div class="empty">正在校验这份备份…（带音频的包要几秒钟）</div>';
  let c;
  try{ c = await impPost('/api/data/import_check', {path: p.path}); }
  catch(e){ box.innerHTML = '<div class="empty">校验请求失败：' + esc(e.message) + '</div>'; return; }
  if(!c.ok){
    box.innerHTML = '<div class="empty" style="color:#ff9b9b">这份备份不能用：' + esc(c.msg || '校验没通过') + '</div>';
    return;
  }
  DIO = c; dioRenderCheck(c);
}

function dioRenderCheck(d){
  const b = d.current || {}, r = (d.preview || {}).replace || {}, m = (d.preview || {}).merge || {};
  const rows = [['访客', 'visits'], ['申请', 'requests'], ['其中待批', 'pending'],
                ['白名单', 'whitelist'], ['白名单申请', 'wl_apps'], ['队列里等着的', 'queue']];
  const cell = (v, base, col) =>
    '<td align="right" style="padding:5px 7px;color:' + ((v||0) !== (base||0) ? col : '#c8d6ea') + '">' + (v||0) + '</td>';
  let t = '<div style="overflow:auto;background:#0e1524;border:1px solid #2a3550;border-radius:10px;padding:10px 12px">'
        + '<div style="font-size:12.5px;color:#cfe3ff;margin-bottom:8px">'
        + '包来自 <b>' + esc(d.machine) + '</b>，导出于 ' + esc(d.created)
        + '（v' + esc(d.version) + '，' + esc(d.size_text) + '）</div>'
        + '<table style="width:100%;border-collapse:collapse;font-size:12.5px">'
        + '<tr style="color:#75869f;font-size:11.5px"><th align="left" style="padding:4px 7px">项目</th>'
        + '<th align="right" style="padding:4px 7px">本机现在</th>'
        + '<th align="right" style="padding:4px 7px">覆盖导入后</th>'
        + '<th align="right" style="padding:4px 7px">合并导入后</th></tr>';
  rows.forEach(([label, k]) => {
    t += '<tr><td style="padding:5px 7px;color:#a9bcd8">' + label + '</td>'
       + '<td align="right" style="padding:5px 7px;color:#8fa3c2">' + (b[k]||0) + '</td>'
       + cell(r[k], b[k], '#f0c674') + cell(m[k], b[k], '#7ef0a8') + '</tr>';
  });
  t += '</table>';

  const bits = [];
  bits.push('成品索引 ' + (d.covers.incoming || 0) + ' 首（本机现有 ' + (d.covers.now || 0) + ' 首）');
  if(d.audio_files) bits.push('成品音频 <b>' + d.audio_files + '</b> 个（' + esc(d.audio_text) + '）');
  bits.push(d.settings_included ? '含高级设置' : '不含高级设置');
  t += '<div style="font-size:12px;color:#8fa3c2;margin-top:8px;line-height:1.7">' + bits.join(' · ') + '</div>';
  if(d.missing_models_n){
    t += '<div style="font-size:12px;color:#f0c674;margin-top:6px;line-height:1.7">'
       + '⚠ 备份里用到 ' + d.missing_models_n + ' 个本机没装的声线：'
       + d.missing_models.slice(0, 6).map(esc).join('、')
       + (d.missing_models_n > 6 ? ' 等' : '') + ' —— 导入后这些歌要先去模型市场补齐声线。'
       + '</div>';
  }
  if(d.audio_files === 0 && d.covers.incoming > d.covers.now){
    t += '<div style="font-size:12px;color:#8fa3c2;margin-top:6px;line-height:1.7">'
       + '这个包没带音频文件，导入后新增的那 ' + (d.covers.incoming - d.covers.now)
       + ' 首只在成品库列表里看到、点开会提示缺文件。需要音频就导出时勾「连成品音频一起打包」。</div>';
  }
  t += '<div style="margin-top:11px;display:flex;gap:9px;flex-wrap:wrap;align-items:center">'
     + '<label style="font-size:12px;color:#f0c674;display:flex;align-items:center;gap:6px">'
     + '<input type="checkbox" id="dioSure"> 我确认要改写本机数据</label>'
     + '<button class="red mini" onclick="dioApply(\'replace\', this)">用这份备份覆盖本机</button>'
     + '<button class="green mini" onclick="dioApply(\'merge\', this)">合并进本机（同一条不重复）</button>'
     + '<button class="gray mini" onclick="DIO=null;$(\'#dioBox\').innerHTML=\'\'">取消</button>'
     + '</div>'
     + '<div style="font-size:11.5px;color:#6f8099;margin-top:7px;line-height:1.7">'
     + '覆盖 = 本机数据全部换成包里的（本机独有的记录会消失）；合并 = 两边合起来，同一条取更新的那次状态。'
     + '两种方式都会在动手前把本机现有数据自动存一份。</div></div>';
  $('#dioBox').innerHTML = t;
}

async function dioApply(mode, btn){
  if(!DIO) return;
  if(!$('#dioSure').checked){ toast('先勾选「我确认要改写本机数据」'); return; }
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '导入中…';
  dioMsg('wait', '正在导入，别关这个页面…');
  try{
    const d = await impPost('/api/data/import_apply',
      {path: DIO.path, token: DIO.token, mode: mode, confirm: true});
    if(!d.ok){
      dioMsg('bad', esc(d.msg || '导入失败')
        + (d.backup ? '<br>导入前的数据已存在：' + esc(d.backup) : ''));
      return;
    }
    let extra = '';
    if(d.stats && d.stats.running_to_queued){
      extra += '<br>有 ' + d.stats.running_to_queued + ' 单原本是「制作中」，已改回「排队中」等重新制作。';
    }
    if(d.missing_models_n){
      extra += '<br>还缺 ' + d.missing_models_n + ' 个声线：'
             + d.missing_models.slice(0, 5).map(esc).join('、')
             + ' —— 去「模型市场」补齐后这些歌才做得出。';
    }
    if(d.warns && d.warns.length){
      extra += '<br>设置项提示：' + d.warns.map(esc).join('；');
    }
    dioMsg('ok', esc(d.msg) + extra
      + '<br>导入前的本机数据已存档：<b>' + esc(d.backup_name || '（本机原本没有数据）') + '</b>');
    DIO = null; $('#dioBox').innerHTML = '';
    dioLoad(); refresh();
  }catch(e){
    dioMsg('bad', '请求失败：' + esc(e.message));
  }finally{
    btn.disabled = false; btn.textContent = label;
  }
}

/* ---------- 模型市场概览 ---------- */
async function loadMarket(){
  try{
    const d = await (await fetch('/api/market/installed', {cache:'no-store'})).json();
    if(d.ok){
      const withIdx = (d.items||[]).filter(x => x.has_index).length;
      $('#mkSum').textContent = (d.n||0) + ' 个已装' + (d.n ? '（' + withIdx + ' 个带索引）' : '');
    }
  }catch(e){}
  try{
    const l = await (await fetch('/api/market/lines', {cache:'no-store'})).json();
    if(l.ok && l.lines){
      $('#mkLine').textContent = '线路：' + l.lines.map(x => x.label + (x.skipped ? ' 已停用' : (x.ok ? ' ' + x.ms + 'ms' : ' 不通'))).join(' · ');
    }
  }catch(e){}
}

// 用户一动手就标脏，定时刷新只更新其它卡片，不再重建这张表单
document.addEventListener('input', e => {
  if(e.target && e.target.closest && e.target.closest('#advBox')) ADV_DIRTY = true;
});
document.addEventListener('change', e => {
  if(e.target && e.target.closest && e.target.closest('#advBox')) ADV_DIRTY = true;
});

function refresh(){ loadAdv(); loadMarket(); dioLoad(); }
refresh(); setInterval(refresh, 15000);
