/* RVC 控制台 · 前端公共小工具
 *
 * v2.1.2 起从四个 page_*.js 里提出来：原来每页各存一份一模一样的 $ / toast / esc，
 * 改一处忘三处是迟早的事（esc 的安全变体 jsq 就只在访客页有，任务页 8 个内联
 * onclick 还在用 esc 拼字符串）。
 *
 * 加载顺序：必须排在 page_*.js 之前（页面里 <script> 顺序已经保证了这一点）。
 * 这四个都是顶层 const/function，页面脚本里不许再定义同名 —— e2e_modules 会查。
 */

/* 单个元素；查不到返回 null，由调用方自己判 */
const $ = s => document.querySelector(s);

/* 提示条：共用一个 #toast 节点，重复调用只重置计时，不会叠出一堆定时器 */
function toast(m) {
  const t = $('#toast');
  if (!t) return;
  t.textContent = m;
  t.classList.add('on');
  clearTimeout(window._t);
  window._t = setTimeout(() => t.classList.remove('on'), 2400);
}

/* 转义给「HTML 文本 / 双引号属性」用。
 * 注意它不处理单引号：放在 onclick="f('...')" 里救不了你，那里必须用 jsq。 */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* 转义给「内联事件里的 JS 字符串字面量」用，即 onclick="fn('...')"。
 * esc 不转义单引号，IP / 文件名 / 用户输入里一个 ' 就把整句 JS 断掉
 * （表现是按钮点了没反应）；\x27 不是 HTML 实体，解析器不会提前解码，
 * 到 JS 手里才变回单引号，字符串边界不会被破坏。
 * 换行要一并转成 \n，否则内联属性里会真的断行。 */
function jsq(s) {
  return String(s == null ? '' : s)
    .replace(/\\/g, '\\\\').replace(/'/g, '\\x27').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/\r?\n/g, '\\n');
}
