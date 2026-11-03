/* 模型市场「🔄 更新声线」前端逻辑回归（node 直跑，无外部依赖）
 *
 * 为什么单独测：这段逻辑全是异步轮询，光靠 node --check 只能保证语法不炸；
 * 「端口先关后开才判定成功」「WebUI 没起就不该发重启」「失败要能说清原因」
 * 这些真实行为，只有把 market_page.html 里的代码抽出来跑一遍才验证得了。
 *
 * 跑法： node ui_market_restart_test.js
 */
const fs = require('fs');
const path = require('path');
const here = __dirname;

const html = fs.readFileSync(path.join(here, 'market_page.html'), 'utf8');
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x => x[1]).join('\n');
const start = script.indexOf('/* ---------- 更新声线');
const end = script.indexOf('/* ---------- 线路');
if (start < 0 || end < 0) { console.error('market_page.html 里没找到「更新声线」代码块'); process.exit(1); }
let block = script.slice(start, end);
block = block.replace('const sleep = ms => new Promise(r => setTimeout(r, ms));', '');  // 桩里已定义

const STUB = String.raw`
// 桩：市场页脚本依赖的最小环境
let BOX = {className: '', innerHTML: ''};
let BTN = {disabled: false};
const $ = s => s === '#rstBox' ? BOX : s === '#updBtn' ? BTN : {};
let PORTS = {};
let POST_RESULT = {ok: true, msg: 'ok'};
let POSTED = [];
let INSTALLED_RELOADED = 0;
let api = async (url) => {
  if (url === '/api/status') return {ports: Object.keys(PORTS).map(p => ({port: +p, open: PORTS[p]}))};
  return {ok: true};
};
const post = async (url, body) => { POSTED.push([url, body]); return POST_RESULT; };
const esc = s => String(s).replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
const loadInstalled = async () => { INSTALLED_RELOADED++; };
// 测试里不需要真的等 1.5 秒
const sleep = ms => Promise.resolve();
let JOBS = [];

`;

const TEST = String.raw`
// 测试体：在同一个函数作用域里跑，能直接看到 block 里的 UPD / DONE_KNOWN
function reset() {
  BOX = {className: '', innerHTML: ''};
  BTN = {disabled: false};
  POSTED = [];
  INSTALLED_RELOADED = 0;
  UPD.running = false;
}

(async () => {
  let fail = 0;
  const chk = (name, ok, extra) => {
    console.log((ok ? '  [OK] ' : '  [!!] ') + name + (extra ? '  -> ' + extra : ''));
    if (!ok) fail++;
  };

  // 1. WebUI 没开：直接拒绝，不发重启请求
  reset();
  PORTS = {3333: true, 7865: false};
  await restartApi();
  chk('WebUI 未运行时不发重启请求', POSTED.length === 0);
  chk('给出 WebUI 未运行的指引',
      BOX.className.includes('bad') && BOX.innerHTML.includes('一键开启服务'), BOX.innerHTML.slice(0, 46));
  chk('按钮恢复可点', BTN.disabled === false && UPD.running === false);

  // 2. 正常重启：端口 开(旧) → 关 → 开(新)
  reset();
  POST_RESULT = {ok: true, msg: '已停 / 已启'};
  let calls = 0;
  api = async (url) => {
    if (url !== '/api/status') return {ok: true};
    calls++;
    // 第 1 次（重启前）开着；第 2~5 次（停止/启动中）关着；之后新进程开了
    const open3333 = calls <= 1 ? true : calls <= 5 ? false : true;
    return {ports: [{port: 3333, open: open3333}, {port: 7865, open: true}]};
  };
  await restartApi();
  chk('重启成功显示绿色提示', BOX.className.includes('ok') && BOX.innerHTML.includes('已更新'), BOX.innerHTML.slice(0, 40));
  chk('成功后刷新已装列表', INSTALLED_RELOADED === 1);
  chk('请求打到 /api/action restart api',
      POSTED.length === 1 && POSTED[0][1].do === 'restart' && POSTED[0][1].target === 'api',
      JSON.stringify(POSTED[0] || null));
  chk('结束后按钮恢复', BTN.disabled === false && UPD.running === false);

  // 2b. 3333 本来没开：起来后直接算成功
  reset();
  let c2 = 0;
  POST_RESULT = {ok: true, msg: 'ok'};
  api = async (url) => {
    if (url !== '/api/status') return {ok: true};
    c2++;
    const open3333 = c2 <= 2 ? false : true;   // 前两次查询：没开
    return {ports: [{port: 3333, open: open3333}, {port: 7865, open: true}]};
  };
  await restartApi();
  chk('后端原本未运行也能启动成功', BOX.className.includes('ok'), BOX.innerHTML.slice(0, 40));

  // 3. 面板拒绝重启
  reset();
  api = async () => ({ports: [{port: 3333, open: true}, {port: 7865, open: true}]});
  POST_RESULT = {ok: false, msg: '启动失败: 端口被占用'};
  await restartApi();
  chk('重启失败时给出原因', BOX.className.includes('bad') && BOX.innerHTML.includes('端口被占用'), BOX.innerHTML.slice(0, 40));

  // 4. 装完任务后自动亮提示（首次加载不提示历史任务）
  reset();
  JOBS = [{id: 'a', state: 'done', installed: {name: '旧声线.pth'}, pth: 'x/old.pth'}];
  noteNewDone();
  chk('首次加载不提示历史任务', BOX.className === '' && BOX.innerHTML === '');
  JOBS = [
    {id: 'a', state: 'done', installed: {name: '旧声线.pth'}, pth: 'x/old.pth'},
    {id: 'b', state: 'done', installed: {name: '新声线.pth'}, pth: 'x/new.pth'},
  ];
  noteNewDone();
  chk('新任务完成后亮提示条',
      BOX.className.includes('on') && BOX.innerHTML.includes('新声线.pth'), BOX.innerHTML.slice(0, 50));
  const before = BOX.innerHTML;
  noteNewDone();
  chk('重复轮询不重复覆盖', BOX.innerHTML === before);

  // 5. 失败任务不提示
  reset();
  JOBS = [{id: 'a', state: 'done', installed: {name: 'x'}, pth: 'a'}];
  noteNewDone();
  JOBS = [
    {id: 'a', state: 'done', installed: {name: 'x'}, pth: 'a'},
    {id: 'c', state: 'error', pth: 'y/bad.pth'},
  ];
  noteNewDone();
  chk('失败任务不亮提示', BOX.className === '', BOX.className);

  console.log(fail ? '\n==== ' + fail + ' 项失败 ====' : '\n==== 全部通过 ====');
  process.exitCode = fail ? 1 : 0;
})();

`;

try {
  new Function(STUB + '\n' + block + '\n' + TEST)();
} catch (e) {
  console.error('执行失败:', e.message);
  process.exit(1);
}
