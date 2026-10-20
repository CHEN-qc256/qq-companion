/* qq-companion 管理面板 —— 纯原生 JS，无框架、无构建。
 *
 * 核心思路：前端是「哑」的。
 * 它不认识任何一个具体参数名（没有 if (path === 'llm.port') 这种代码），
 * 只按后端 /api/schema 给的 type 挑一个控件渲染。所以加参数只要改
 * panel/schema.py，这个文件一行都不用动。
 */
'use strict';

/* ══════════════ 状态 ══════════════ */
let SCHEMA = null;          // { groups:[{id,title,desc,params:[]}], total, defaults }
let CFG = {};               // 当前编辑中的配置（真源）
let SNAPSHOT = '';          // 载入时的 JSON 快照，用来判断「有未保存的改动」
let ISSUES = [];            // 后端返回的校验问题
let P = {};                 // path -> param 元数据
let STATUS = {};
let logOffset = 0, logPaused = false, logTimer = null, logLines = [], logSize = 0;

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ══════════════ 小工具 ══════════════ */
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  const txt = await r.text();
  let data;
  try { data = JSON.parse(txt); }
  catch (e) { throw new Error(txt.slice(0, 500) || ('HTTP ' + r.status)); }
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}

function getP(o, p) {
  return p.split('.').reduce((c, k) =>
    (c && typeof c === 'object' && k in c) ? c[k] : undefined, o);
}
function setP(o, p, v) {
  const ks = p.split('.'); let c = o;
  for (let i = 0; i < ks.length - 1; i++) {
    if (typeof c[ks[i]] !== 'object' || c[ks[i]] === null) c[ks[i]] = {};
    c = c[ks[i]];
  }
  c[ks[ks.length - 1]] = v;
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}
function fmtYuan(v) {
  if (v == null) return '—';
  if (v === 0) return '0';
  return v < 0.01 ? v.toFixed(6) : (v < 1 ? v.toFixed(4) : v.toFixed(2));
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtSize(b) {
  if (b == null) return '';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(1) + ' MB';
}
let toastTimer = null;
function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, kind === 'err' ? 9000 : 4200);
}

/* ══════════════ 弹窗 ══════════════ */
function modal(title, bodyNode, buttons) {
  $('#modalTitle').textContent = title;
  const body = $('#modalBody'); body.innerHTML = '';
  body.appendChild(bodyNode);
  const foot = $('#modalFoot'); foot.innerHTML = '';
  (buttons || []).forEach(b => {
    const btn = el('button', 'btn' + (b.primary ? ' primary' : ''), esc(b.label));
    btn.onclick = () => { if (b.keepOpen !== true) closeModal(); b.onClick && b.onClick(); };
    foot.appendChild(btn);
  });
  $('#modal').hidden = false;
}
function closeModal() { $('#modal').hidden = true; }
$('#modalClose').onclick = closeModal;
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

/* ══════════════ 主题 ══════════════ */
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  $('#btnTheme').textContent = t === 'dark' ? '☀️' : '🌙';
  try { localStorage.setItem('qc-theme', t); } catch (e) {}
}
$('#btnTheme').onclick = () =>
  applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');

/* ══════════════ 标签页 ══════════════ */
$$('.tab').forEach(tab => {
  tab.onclick = () => {
    $$('.tab').forEach(t => t.classList.remove('active'));
    $$('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    $('#tab-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'logs') startLogPolling();
    else stopLogPolling(true);
    if (tab.dataset.tab === 'stats') loadStats();
    if (tab.dataset.tab === 'voice') loadVoice();
  };
});

/* ══════════════ 控件工厂 ══════════════
 * 每个工厂返回一个 .ctl 元素，并且自己在值变化时写回 CFG + 标脏。 */
function markDirty() {
  const d = JSON.stringify(CFG) !== SNAPSHOT;
  $('#dirtyFlag').hidden = !d;
  return d;
}

function bind(p, node, readFn) {
  const commit = () => {
    setP(CFG, p.path, readFn(node));
    markDirty();
  };
  node.addEventListener('input', commit);
  node.addEventListener('change', commit);
  return node;
}

function ctlText(p, v, kind) {
  const c = el('div', 'ctl');
  const i = el('input', p.type === 'password' ? '' : 'wide');
  i.type = kind || (p.type === 'password' ? 'password' : 'text');
  i.value = v == null ? '' : v;
  if (p.placeholder) i.placeholder = p.placeholder;
  const row = el('div', 'row');
  row.appendChild(i);
  if (p.type === 'password') {
    const eye = el('button', 'btn small', '显示');
    eye.onclick = () => {
      const hid = i.type === 'password';
      i.type = hid ? 'text' : 'password';
      eye.textContent = hid ? '隐藏' : '显示';
    };
    row.appendChild(eye);
  }
  if (p.type === 'path') {
    const pick = el('button', 'btn small', '选择…');
    pick.onclick = () => browsePath(p, i);
    row.appendChild(pick);
  }
  c.appendChild(row);
  if (p.unit) c.appendChild(el('div', 'unit', '单位：' + esc(p.unit)));
  bind(p, i, () => i.value);
  return c;
}

function ctlNumber(p, v) {
  const c = el('div', 'ctl');
  const row = el('div', 'row');
  const i = el('input');
  i.type = 'number';
  if (p.step != null) i.step = p.step;
  if (p.min != null) i.min = p.min;
  if (p.max != null) i.max = p.max;
  i.value = v == null ? '' : v;
  const stack = el('div', 'row');
  stack.appendChild(i);
  const bump = (d) => {
    const step = p.step || 1;
    const cur = parseFloat(i.value);
    let nv = (isNaN(cur) ? 0 : cur) + d * step;
    if (p.min != null) nv = Math.max(p.min, nv);
    if (p.max != null) nv = Math.min(p.max, nv);
    i.value = Math.round(nv * 1e6) / 1e6;
    i.dispatchEvent(new Event('change'));
  };
  const up = el('button', 'btn small', '▲'), dn = el('button', 'btn small', '▼');
  up.onclick = () => bump(1); dn.onclick = () => bump(-1);
  stack.appendChild(up); stack.appendChild(dn);
  if (p.unit) stack.appendChild(el('span', 'unit', esc(p.unit)));
  row.appendChild(stack);
  c.appendChild(row);
  const range = (p.min != null || p.max != null)
    ? `范围 ${p.min != null ? p.min : '−∞'} ~ ${p.max != null ? p.max : '∞'}`
    : '';
  if (range) c.appendChild(el('div', 'unit', esc(range)));
  bind(p, i, () => (i.value === '' ? null : (p.type === 'int' ? parseInt(i.value, 10) : parseFloat(i.value))));
  return c;
}

function ctlBool(p, v) {
  const c = el('div', 'ctl');
  const row = el('div', 'row');
  const sw = el('label', 'sw');
  const i = el('input'); i.type = 'checkbox'; i.checked = !!v;
  const s = el('span');
  sw.appendChild(i); sw.appendChild(s);
  const txt = el('span', 'swText', v ? '开启' : '关闭');
  row.appendChild(sw); row.appendChild(txt);
  c.appendChild(row);
  i.addEventListener('change', () => {
    txt.textContent = i.checked ? '开启' : '关闭';
    setP(CFG, p.path, i.checked); markDirty();
  });
  return c;
}

function ctlEnum(p, v) {
  const c = el('div', 'ctl');
  const row = el('div', 'row');
  const s = el('select');
  (p.choices || []).forEach(ch => {
    const o = el('option'); o.value = ch.value; o.textContent = ch.label;
    if (ch.value === v) o.selected = true;
    s.appendChild(o);
  });
  if (v != null && !(p.choices || []).some(ch => ch.value === v)) {
    const o = el('option'); o.value = v; o.textContent = v + '（当前值）'; o.selected = true;
    s.appendChild(o);
  }
  row.appendChild(s);
  c.appendChild(row);
  bind(p, s, () => s.value);
  return c;
}

/* 数字标签列表 */
function ctlIntList(p, v) {
  const c = el('div', 'ctl');
  const wrap = el('div', 'tags');
  const draw = () => {
    wrap.innerHTML = '';
    (getP(CFG, p.path) || []).forEach((n, idx) => {
      const t = el('span', 'tag', esc(n));
      const x = el('button', null, '✕');
      x.title = '删掉这个';
      x.onclick = () => {
        const a = getP(CFG, p.path) || []; a.splice(idx, 1);
        setP(CFG, p.path, a); draw(); markDirty();
      };
      t.appendChild(x); wrap.appendChild(t);
    });
    const i = el('input'); i.type = 'number'; i.placeholder = p.placeholder || '输入后回车';
    i.style.width = '150px';
    i.onkeydown = e => {
      if (e.key !== 'Enter') return;
      const n = parseInt(i.value, 10);
      if (isNaN(n)) { toast('只能填整数', 'err'); return; }
      const a = getP(CFG, p.path) || [];
      if (a.includes(n)) { toast('已经有了'); return; }
      a.push(n); setP(CFG, p.path, a); i.value = ''; draw(); markDirty();
    };
    wrap.appendChild(i);
  };
  draw();
  c.appendChild(wrap);
  c.appendChild(el('div', 'unit', '回车添加，点 ✕ 删除'));
  return c;
}

/* 字符串列表 */
function ctlStrList(p, v) {
  const c = el('div', 'ctl');
  const wrap = el('div', 'tags');
  const draw = () => {
    wrap.innerHTML = '';
    (getP(CFG, p.path) || []).forEach((n, idx) => {
      const t = el('span', 'tag', esc(n));
      const x = el('button', null, '✕');
      x.onclick = () => {
        const a = getP(CFG, p.path) || []; a.splice(idx, 1);
        setP(CFG, p.path, a); draw(); markDirty();
      };
      t.appendChild(x); wrap.appendChild(t);
    });
    const i = el('input'); i.type = 'text'; i.placeholder = p.placeholder || '输入后回车';
    i.style.width = '150px';
    i.onkeydown = e => {
      if (e.key !== 'Enter') return;
      const s = i.value.trim();
      if (!s) return;
      const a = getP(CFG, p.path) || [];
      if (a.includes(s)) { toast('已经有了'); return; }
      a.push(s); setP(CFG, p.path, a); i.value = ''; draw(); markDirty();
    };
    wrap.appendChild(i);
  };
  draw();
  c.appendChild(wrap);
  c.appendChild(el('div', 'unit', '回车添加，点 ✕ 删除'));
  return c;
}

/* 单个区间 [从, 到] */
function ctlRange(p, v) {
  const c = el('div', 'ctl');
  const row = el('div', 'row');
  const [la, lb] = p.pairLabels || ['起', '止'];
  const cur = Array.isArray(v) ? v.slice(0, 2) : [0, 0];
  const mk = (idx) => {
    const i = el('input'); i.type = 'number'; i.style.width = '92px';
    i.value = cur[idx] == null ? 0 : cur[idx];
    i.addEventListener('change', () => {
      const a = (getP(CFG, p.path) || [0, 0]).slice(0, 2);
      a[idx] = parseInt(i.value, 10) || 0;
      setP(CFG, p.path, a); markDirty();
    });
    return i;
  };
  row.appendChild(el('span', 'unit', esc(la)));
  row.appendChild(mk(0));
  row.appendChild(el('span', 'sep', '→'));
  row.appendChild(el('span', 'unit', esc(lb)));
  row.appendChild(mk(1));
  if (p.unit) row.appendChild(el('span', 'unit', esc(p.unit)));
  c.appendChild(row);
  return c;
}

/* 多个区间 [[9,12],[14,18]] */
function ctlRangeList(p, v) {
  const c = el('div', 'ctl');
  const wrap = el('div', 'pairs');
  const draw = () => {
    wrap.innerHTML = '';
    const [la, lb] = p.pairLabels || ['起', '止'];
    (getP(CFG, p.path) || []).forEach((pair, idx) => {
      const row = el('div', 'pair');
      row.appendChild(el('span', 'unit', esc(la)));
      const a = el('input'); a.type = 'number'; a.value = pair[0];
      const b = el('input'); b.type = 'number'; b.value = pair[1];
      const upd = () => {
        const arr = getP(CFG, p.path) || [];
        arr[idx] = [parseInt(a.value, 10) || 0, parseInt(b.value, 10) || 0];
        setP(CFG, p.path, arr); markDirty();
      };
      a.addEventListener('change', upd); b.addEventListener('change', upd);
      const rm = el('button', 'rm', '删');
      rm.onclick = () => {
        const arr = getP(CFG, p.path) || []; arr.splice(idx, 1);
        setP(CFG, p.path, arr); draw(); markDirty();
      };
      row.appendChild(a); row.appendChild(el('span', 'sep', '→'));
      row.appendChild(el('span', 'unit', esc(lb))); row.appendChild(b); row.appendChild(rm);
      wrap.appendChild(row);
    });
    const add = el('button', 'btn small', '＋ 加一段');
    add.onclick = () => {
      const arr = getP(CFG, p.path) || []; arr.push([0, 0]);
      setP(CFG, p.path, arr); draw(); markDirty();
    };
    wrap.appendChild(add);
  };
  draw();
  c.appendChild(wrap);
  return c;
}

/* [[正则, 回复], ...] */
function ctlPairList(p, v) {
  const c = el('div', 'ctl');
  const wrap = el('div', 'pairs');
  const [la, lb] = p.pairLabels || ['一', '二'];
  const draw = () => {
    wrap.innerHTML = '';
    (getP(CFG, p.path) || []).forEach((pair, idx) => {
      const row = el('div', 'pair');
      const a = el('input', 're'); a.value = pair[0] || ''; a.placeholder = la;
      const b = el('input'); b.value = pair[1] || ''; b.placeholder = lb;
      const upd = () => {
        const arr = getP(CFG, p.path) || [];
        arr[idx] = [a.value, b.value];
        setP(CFG, p.path, arr); markDirty();
      };
      a.addEventListener('input', upd); b.addEventListener('input', upd);
      const rm = el('button', 'rm', '删');
      rm.onclick = () => {
        const arr = getP(CFG, p.path) || []; arr.splice(idx, 1);
        setP(CFG, p.path, arr); draw(); markDirty();
      };
      row.appendChild(a); row.appendChild(b); row.appendChild(rm);
      wrap.appendChild(row);
    });
    const add = el('button', 'btn small', '＋ 加一条');
    add.onclick = () => {
      const arr = getP(CFG, p.path) || []; arr.push(['', '']);
      setP(CFG, p.path, arr); draw(); markDirty();
    };
    wrap.appendChild(add);
  };
  draw();
  c.appendChild(wrap);
  return c;
}

/* 固定键的映射表 {sleeping: "...", working: "..."} */
function ctlMap(p, v) {
  const c = el('div', 'ctl');
  const g = el('div', 'maptbl');
  const keys = p.keys || Object.keys(v || {});
  keys.forEach(k => {
    const lab = el('label', null, esc((p.keyLabels && p.keyLabels[k]) || k));
    lab.title = k;
    const i = el('input'); i.className = 'wide'; i.value = (v && v[k]) || '';
    i.addEventListener('input', () => {
      const o = Object.assign({}, getP(CFG, p.path) || {});
      o[k] = i.value; setP(CFG, p.path, o); markDirty();
    });
    g.appendChild(lab); g.appendChild(i);
  });
  c.appendChild(g);
  return c;
}

/* 计价表 {model: {cache_hit:{peak,offpeak}, ...}} */
function ctlPriceTable(p, v) {
  const c = el('div', 'ctl');
  const buckets = ['cache_hit', 'cache_miss', 'output'];
  const bands = ['peak', 'offpeak'];
  const [bp, bo] = p.pairLabels || ['peak', 'offpeak'];
  const tbl = el('table', 'priceTbl');
  const thead = el('thead');
  const hr = el('tr');
  hr.appendChild(el('th', null, '计费项'));
  hr.appendChild(el('th', null, esc(bp)));
  hr.appendChild(el('th', null, esc(bo)));
  thead.appendChild(hr); tbl.appendChild(thead);
  const tb = el('tbody');

  (p.keys || Object.keys(v || {})).forEach(model => {
    const mr = el('tr');
    const mc = el('td');
    mc.colSpan = 3;
    mc.style.fontWeight = '650';
    mc.style.background = 'var(--panel2)';
    mc.textContent = model;
    mr.appendChild(mc); tb.appendChild(mr);

    buckets.forEach(bk => {
      const row = el('tr');
      row.appendChild(el('td', null, esc((p.keyLabels && p.keyLabels[bk]) || bk)));
      bands.forEach(band => {
        const v0 = ((v || {})[model] || {})[bk] || {};
        const inp = el('input'); inp.type = 'number'; inp.step = '0.01'; inp.min = '0';
        inp.value = v0[band] == null ? 0 : v0[band];
        inp.addEventListener('change', () => {
          const o = getP(CFG, p.path) || {};
          o[model] = o[model] || {};
          o[model][bk] = o[model][bk] || {};
          o[model][bk][band] = parseFloat(inp.value) || 0;
          setP(CFG, p.path, o); markDirty();
        });
        const td = el('td'); td.appendChild(inp); row.appendChild(td);
      });
      tb.appendChild(row);
    });
  });
  tbl.appendChild(tb);
  c.appendChild(tbl);
  c.appendChild(el('div', 'unit', '单位：元 / 百万 token'));
  return c;
}

function makeControl(p, v) {
  switch (p.type) {
    case 'bool':        return ctlBool(p, v);
    case 'int':         return ctlNumber(p, v);
    case 'float':       return ctlNumber(p, v);
    case 'enum':        return ctlEnum(p, v);
    case 'int_list':    return ctlIntList(p, v);
    case 'str_list':    return ctlStrList(p, v);
    case 'range':       return ctlRange(p, v);
    case 'range_list':  return ctlRangeList(p, v);
    case 'pair_list':   return ctlPairList(p, v);
    case 'map':         return ctlMap(p, v);
    case 'price_table': return ctlPriceTable(p, v);
    case 'password':    return ctlText(p, v, 'password');
    case 'path':        return ctlText(p, v, 'text');
    default:            return ctlText(p, v, 'text');
  }
}

/* ══════════════ 文件浏览（给 path 类型用） ══════════════ */
async function browsePath(p, input) {
  const body = el('div');
  let cur = input.value || '.';
  const list = el('ul', 'fsList');
  const pathLab = el('div', 'fsPath');
  body.appendChild(pathLab); body.appendChild(list);

  const load = async (dir) => {
    let d;
    try { d = await api('/api/fs?path=' + encodeURIComponent(dir)); }
    catch (e) { toast(e.message, 'err'); return; }
    cur = d.path;
    pathLab.textContent = '当前目录：' + (d.path === '.' ? '（项目根目录）' : d.path);
    list.innerHTML = '';
    if (d.parent != null) {
      const li = el('li', 'dir', '⬆ ..');
      li.onclick = () => load(d.parent);
      list.appendChild(li);
    }
    d.dirs.forEach(n => {
      const li = el('li', 'dir', '📁 ' + esc(n));
      li.onclick = () => load(cur === '.' ? n : cur + '/' + n);
      list.appendChild(li);
    });
    d.files.forEach(f => {
      const li = el('li', null, '📄 ' + esc(f.name) + `<span class="sz">${fmtSize(f.size)}</span>`);
      li.onclick = () => {
        input.value = (d.path === '.' ? '' : d.path + '/') + f.name;
        input.dispatchEvent(new Event('change'));
        closeModal();
      };
      list.appendChild(li);
    });
    if (!d.dirs.length && !d.files.length) list.appendChild(el('li', null, '（空目录）'));
  };

  modal('选择文件 —— ' + p.label, body, [{ label: '关闭' }]);
  await load(cur === '.' ? '.' : (cur.split(/[\\/]/)[0] || '.'));
}

/* ══════════════ 渲染配置 ══════════════ */
function issueFor(path) {
  return ISSUES.filter(i => i.path === path);
}

function renderGroups() {
  const box = $('#groups');
  box.innerHTML = '';
  const showAdv = $('#showAdvanced').checked;
  let shown = 0;

  SCHEMA.groups.forEach(g => {
    const det = el('details', 'group');
    det.open = g.params.length <= 8;   // 小卡片默认展开
    det.dataset.gid = g.id;

    const sum = el('summary');
    sum.appendChild(el('span', null, esc(g.title)));
    sum.appendChild(el('span', 'gdesc', esc(g.desc)));
    const cnt = el('span', 'gcount', g.params.length + ' 项');
    sum.appendChild(cnt);
    det.appendChild(sum);

    const body = el('div', 'groupBody');
    g.params.forEach(p => {
      if (p.advanced && !showAdv) return;
      body.appendChild(renderParam(p));
      shown++;
    });
    if (!body.children.length) return;
    det.appendChild(body);
    box.appendChild(det);
  });

  $('#paramCount').textContent = `显示 ${shown} / ${SCHEMA.total} 个参数`;
  applySearch();
}

function renderParam(p) {
  const wrap = el('div', 'param');
  wrap.dataset.path = p.path;

  const left = el('div', 'paramLeft');
  const lab = el('div', 'paramLabel');
  lab.appendChild(el('span', null, esc(p.label)));
  const tip = el('span', 'tip', '?');
  tip.appendChild(el('span', 'tipText',
    esc(p.desc) + (p.note ? '<i>' + esc(p.note) + '</i>' : '')));
  lab.appendChild(tip);
  lab.appendChild(el('span', 'badge ' + (p.restart ? 'cold' : 'hot'),
    p.restart ? '需重启' : '热更新'));
  left.appendChild(lab);
  left.appendChild(el('div', 'paramKey', esc(p.path)));
  wrap.appendChild(left);

  const right = el('div');
  right.appendChild(makeControl(p, getP(CFG, p.path)));
  if (p.hint) right.appendChild(el('div', 'paramHint', esc(p.hint)));
  const msg = el('div');
  msg.dataset.role = 'msg';
  right.appendChild(msg);
  wrap.appendChild(right);
  return wrap;
}

function paintIssues() {
  // 先清掉上一次的高亮
  $$('.param').forEach(e => {
    e.classList.remove('bad', 'warnRow');
    const m = $('[data-role=msg]', e); if (m) m.innerHTML = '';
    $$('input,select', e).forEach(i => i.classList.remove('invalid'));
  });

  const bar = $('#issueBar');
  if (!ISSUES.length) { bar.hidden = true; return; }
  const errs = ISSUES.filter(i => i.level === 'error');
  bar.hidden = false;
  bar.className = 'issueBar' + (errs.length ? ' hasErr' : '');
  bar.innerHTML = '';
  const head = el('div', null,
    `<b>${errs.length ? '✗ ' + errs.length + ' 个错误' : '⚠ 提醒'}</b>` +
    (errs.length ? '　下面标红的地方要改掉才能保存' : '　这些不是错误，但多半不是你想要的'));
  bar.appendChild(head);
  ISSUES.forEach(i => {
    const row = el('div', 'issueRow ' + (i.level === 'error' ? 'err' : 'warn'));
    row.appendChild(el('span', 'lv', i.level === 'error' ? '错误' : '提醒'));
    const a = el('a', null, esc(i.path));
    a.onclick = () => {
      const e = $(`.param[data-path="${i.path}"]`);
      if (!e) { toast('这个参数被折叠或隐藏了，展开对应分组就能看到'); return; }
      const det = e.closest('details'); if (det) det.open = true;
      e.scrollIntoView({ behavior: 'smooth', block: 'center' });
      e.style.transition = 'box-shadow .3s';
      e.style.boxShadow = '0 0 0 3px var(--accent)';
      setTimeout(() => { e.style.boxShadow = ''; }, 1400);
    };
    row.appendChild(a);
    row.appendChild(el('span', null, esc(i.msg)));
    bar.appendChild(row);
  });

  ISSUES.forEach(i => {
    const e = $(`.param[data-path="${i.path}"]`);
    if (!e) return;
    if (i.level === 'error') {
      e.classList.add('bad');
      $$('input,select', e).forEach(x => x.classList.add('invalid'));
    } else if (!e.classList.contains('bad')) {
      e.classList.add('warnRow');
    }
    const m = $('[data-role=msg]', e);
    if (m) {
      const cls = i.level === 'error' ? 'paramErr' : 'paramWarn';
      m.appendChild(el('div', cls, (i.level === 'error' ? '✗ ' : '⚠ ') + esc(i.msg)));
    }
  });
}

function applySearch() {
  const q = ($('#search').value || '').trim().toLowerCase();
  $$('.group').forEach(g => {
    let visible = 0;
    $$('.param', g).forEach(p => {
      const meta = P[p.dataset.path] || {};
      const hay = (p.dataset.path + ' ' + (meta.label || '') + ' ' +
                   (meta.desc || '') + ' ' + (meta.note || '')).toLowerCase();
      const hit = !q || hay.includes(q);
      p.style.display = hit ? '' : 'none';
      if (hit) visible++;
    });
    g.style.display = visible ? '' : 'none';
    if (q && visible) g.open = true;
  });
}

/* ══════════════ 载入 / 保存 ══════════════ */
async function loadConfig() {
  const d = await api('/api/config');
  CFG = d.values;
  ISSUES = d.issues || [];
  SNAPSHOT = JSON.stringify(CFG);
  $('#cfgPath').textContent = d.path;
  $('#dirtyFlag').hidden = true;
  renderGroups();
  paintIssues();
}

async function doValidate() {
  try {
    const d = await api('/api/config/validate',
      { method: 'POST', body: JSON.stringify({ values: CFG }) });
    ISSUES = d.issues || [];
    paintIssues();
    return d.issues || [];
  } catch (e) { toast(e.message, 'err'); return ISSUES; }
}

async function doSave() {
  const btn = $('#btnSave');
  btn.disabled = true; btn.classList.add('busy');
  try {
    const d = await api('/api/config',
      { method: 'POST', body: JSON.stringify({ values: CFG }) });
    ISSUES = d.issues || [];
    paintIssues();

    if (!d.ok) {
      toast(d.error || '保存失败', 'err');
      return;
    }
    SNAPSHOT = JSON.stringify(CFG);
    $('#dirtyFlag').hidden = true;
    toast('已保存到 ' + $('#cfgPath').textContent +
          (d.backup ? '\n旧文件备份在 config.yaml.bak' : ''), 'ok');

    // 刷新一下服务状态（保存过程可能顺手重启了）
    await loadStatus();

    if (d.restartRequired) {
      const cold = d.coldChanged || [];
      const body = el('div');
      body.innerHTML =
        `<p>这些参数改完<b>必须重启机器人</b>才会生效（它们在启动时就被固化了）：</p>` +
        `<p style="font-family:var(--mono);font-size:12.5px;background:var(--panel2);` +
        `padding:9px 12px;border-radius:8px">${esc(cold.join('、'))}</p>` +
        (d.reloaded
          ? `<p style="color:var(--ok)">另外 ${(d.hotChanged || []).length} 个热更新参数已经直接推给正在跑的进程了，那部分不用重启。</p>`
          : '') +
        `<p>现在重启吗？（重启期间 QQ 上会短暂收不到回复，大约 2~3 秒）</p>`;
      modal('需要重启才生效', body, [
        { label: '稍后自己重启' },
        { label: '立即重启', primary: true, onClick: () => serviceAction('restart', true) },
      ]);
    }
  } catch (e) {
    toast('保存出错：' + e.message, 'err');
  } finally {
    btn.disabled = false; btn.classList.remove('busy');
  }
}

/* ══════════════ 重置 / 导入 / 导出 ══════════════ */
$('#btnReset').onclick = async () => {
  const body = el('div');
  body.innerHTML =
    '<p>把所有参数恢复成<b>出厂默认值</b>。</p>' +
    '<p style="color:var(--warn)">注意：</p>' +
    '<ul style="margin:4px 0 10px 18px;line-height:1.9">' +
    '<li>你的 <b>API Key</b> 和 <b>群白名单</b> 会被清空</li>' +
    '<li><b>法定节假日</b>会变空（要重新跑 tools/holidays.py --write）</li>' +
    '<li>人设文件路径会回到 persona/default.md</li>' +
    '</ul><p>这一步只是填进表单，<b>还没有写盘</b>，你还能再撤回来。</p>';
  modal('恢复默认值', body, [
    { label: '取消' },
    {
      label: '确认恢复', primary: true, onClick: async () => {
        try {
          const d = await api('/api/config/defaults');
          // 保住 API Key —— 把它清掉是最容易让人骂人的一个「贴心功能」
          const oldKey = getP(CFG, 'llm.api_key');
          const oldTok = getP(CFG, 'onebot.access_token');
          CFG = d.values;
          if (oldKey) setP(CFG, 'llm.api_key', oldKey);
          if (oldTok) setP(CFG, 'onebot.access_token', oldTok);
          ISSUES = d.issues || [];
          renderGroups(); paintIssues(); markDirty();
          toast('已填成默认值（API Key 帮你留着了）。确认没问题再点保存。');
        } catch (e) { toast(e.message, 'err'); }
      }
    },
  ]);
};

$('#btnExport').onclick = () => {
  const body = el('div');
  body.innerHTML =
    '<p>导出当前表单里的配置（不是磁盘上那份）。</p>' +
    '<label class="chk" style="display:block;margin:10px 0"><input type="checkbox" id="expMask" checked> ' +
    '把 API Key 和连接令牌打码</label>' +
    '<pre id="expPre" style="max-height:320px;overflow:auto;background:var(--panel2);' +
    'padding:12px;border-radius:8px;font-size:12px;white-space:pre-wrap"></pre>';
  const draw = () => {
    const mask = $('#expMask', body).checked;
    const obj = JSON.parse(JSON.stringify(CFG));
    const m = s => (s && s.length > 8) ? s.slice(0, 5) + '...' + s.slice(-4) : (s ? '***' : '');
    if (mask) {
      if (getP(obj, 'llm.api_key')) setP(obj, 'llm.api_key', m(getP(obj, 'llm.api_key')));
      if (getP(obj, 'onebot.access_token')) setP(obj, 'onebot.access_token', m(getP(obj, 'onebot.access_token')));
    }
    $('#expPre', body).textContent = JSON.stringify(obj, null, 2);
  };
  modal('导出配置', body, [
    { label: '关闭' },
    {
      label: '下载 config.yaml', primary: true, onClick: () => {
        const mask = $('#expMask', body).checked ? '1' : '0';
        const a = document.createElement('a');
        a.href = '/api/config/export?format=yaml&mask=' + mask + '&t=' + Date.now();
        a.download = 'config.yaml';
        document.body.appendChild(a); a.click(); a.remove();
      }
    },
    {
      label: '下载 config.json', onClick: () => {
        const mask = $('#expMask', body).checked ? '1' : '0';
        const a = document.createElement('a');
        a.href = '/api/config/export?format=json&mask=' + mask + '&t=' + Date.now();
        a.download = 'config.json';
        document.body.appendChild(a); a.click(); a.remove();
      }
    },
  ]);
  $('#expMask', body).onchange = draw;
  draw();
};

$('#btnImport').onclick = () => {
  const body = el('div');
  body.innerHTML =
    '<p>粘贴一份 YAML 或 JSON（可以只写你想改的那几项，其余按默认值补齐）。</p>' +
    '<textarea id="impText" placeholder="llm:\n  model: deepseek-flash\n  thinking: false"></textarea>' +
    '<div id="impMsg" style="margin-top:10px"></div>';
  modal('导入配置', body, [
    { label: '取消' },
    {
      label: '解析并填进表单', primary: true, keepOpen: true, onClick: async () => {
        try {
          const d = await api('/api/config/import',
            { method: 'POST', body: JSON.stringify({ text: $('#impText', body).value }) });
          if (!d.ok) { $('#impMsg', body).innerHTML = `<span style="color:var(--err)">✗ ${esc(d.error)}</span>`; return; }
          CFG = d.values;
          ISSUES = d.issues || [];
          renderGroups(); paintIssues(); markDirty();
          closeModal();
          toast(`导入成功：${SCHEMA.total} 个参数都已按默认值补齐，请检查后点保存。`, 'ok');
        } catch (e) {
          $('#impMsg', body).innerHTML = `<span style="color:var(--err)">✗ ${esc(e.message)}</span>`;
        }
      }
    },
  ]);
};

$('#btnSave').onclick = doSave;
$('#search').oninput = applySearch;
$('#showAdvanced').onchange = () => { renderGroups(); };
$('#btnExpandAll').onclick = () => $$('.group').forEach(g => g.open = true);
$('#btnCollapseAll').onclick = () => $$('.group').forEach(g => g.open = false);

// Ctrl+S 保存
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault(); doSave();
  }
});
window.addEventListener('beforeunload', e => {
  if (JSON.stringify(CFG) !== SNAPSHOT) { e.preventDefault(); e.returnValue = ''; }
});

/* ══════════════ 服务状态 / 启停 ══════════════ */
async function loadStatus() {
  try {
    STATUS = await api('/api/status');
  } catch (e) {
    STATUS = { running: false, error: e.message };
  }
  const pill = $('#runPill'), txt = $('#runText'), extra = $('#statPill');
  if (STATUS.running) {
    pill.className = 'pill';
    txt.textContent = `运行中　PID ${STATUS.pid}`;
    const bits = [];
    if (STATUS.uptimeSeconds) bits.push('已跑 ' + fmtDur(STATUS.uptimeSeconds));
    bits.push(STATUS.mode === 'reverse' ? `监听 :${STATUS.port}` : `连 ${STATUS.port}`);
    if (!STATUS.portOpen) bits.push('端口还没起来');
    extra.textContent = bits.join('　·　');
    extra.hidden = false;
  } else {
    pill.className = 'pill off';
    txt.textContent = '未运行';
    if (STATUS.stale_pid) {
      extra.textContent = `有个残留的 PID ${STATUS.stale_pid}，可能是之前没退干净`;
      extra.hidden = false;
    } else { extra.hidden = true; }
  }
  if (STATUS.configNewerThanProcess) {
    extra.textContent = (extra.textContent ? extra.textContent + '　·　' : '') +
      '⚠ config.yaml 比进程新，可能有改动没生效';
    extra.hidden = false;
  }
  $('#btnStart').disabled = !!STATUS.running;
  $('#btnStop').disabled = !STATUS.running;
  return STATUS;
}

function fmtDur(s) {
  s = Math.round(s);
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟';
  return Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分';
}

async function serviceAction(action, silent) {
  const label = { start: '启动', stop: '停止', restart: '重启' }[action] || action;
  const btn = { start: '#btnStart', stop: '#btnStop', restart: '#btnRestart' }[action];
  const b = $(btn);
  const was = b.textContent;
  b.disabled = true; b.classList.add('busy'); b.textContent = label + '中';
  try {
    const d = await api('/api/service/' + action, { method: 'POST', body: '{}' });
    if (d.status) STATUS = d.status;
    toast((d.ok ? '✓ ' : '✗ ') + (d.msg || (label + '完成')), d.ok ? 'ok' : 'err');
    await loadStatus();
  } catch (e) {
    toast(label + '失败：' + e.message, 'err');
    await loadStatus();
  } finally {
    b.classList.remove('busy');
    if (document.querySelector(btn) === b) { b.disabled = false; b.textContent = was; }
    await loadStatus();
  }
}
$('#btnStart').onclick = () => serviceAction('start');
$('#btnStop').onclick = () => {
  const body = el('div');
  body.innerHTML = '<p>停掉之后就不会再回复任何消息了。</p>' +
    '<p>NapCat 那边会显示连接断开 —— 这是正常的，重新点「启动」就接回来。</p>';
  modal('确认停止？', body, [
    { label: '取消' },
    { label: '停止', primary: true, onClick: () => serviceAction('stop') },
  ]);
};
$('#btnRestart').onclick = () => serviceAction('restart');

/* ══════════════ 开机自启 ══════════════ */
// 真相在注册表里（HKCU\...\Run\qq-companion），config.yaml 的 boot.enabled 只是记录。
let BOOT = null;

async function loadBoot() {
  try {
    BOOT = await api('/api/boot/status');
  } catch (e) {
    BOOT = null;
  }
  const b = $('#btnBoot');
  if (!b) return BOOT;
  if (!BOOT || BOOT.supported === false) {
    b.disabled = true;
    b.textContent = '开机自启：不支持';
    return BOOT;
  }
  b.disabled = false;
  if (BOOT.stale) {
    b.textContent = '开机自启：需修复';
    b.className = 'btn';
    b.title = '注册表里的路径已经不对了（项目挪过位置？）。点一下重写。';
  } else {
    b.textContent = '开机自启：' + (BOOT.enabled ? '开' : '关');
    b.className = 'btn' + (BOOT.enabled ? ' primary' : '');
    b.title = BOOT.enabled
      ? '下次登录 Windows 会自动启动。点一下关闭。'
      : '登录 Windows 后自动启动机器人，全程不弹黑框。';
  }
  return BOOT;
}

async function setBoot(on, runNow) {
  try {
    const d = await api('/api/boot', { method: 'POST', body: JSON.stringify({ on }) });
    if (!d.ok) { toast(d.error || '设置失败', 'err'); return; }
    toast('✓ ' + d.msg, 'ok');
    BOOT = d;
    await loadBoot();

    // 配置页那份 CFG 也要跟着改，否则之后在配置页保存别的参数会把开关写回去
    if (CFG && CFG.boot) {
      const wasClean = JSON.stringify(CFG) === SNAPSHOT;
      setP(CFG, 'boot.enabled', on);
      if (wasClean) SNAPSHOT = JSON.stringify(CFG);
      const inp = $('.param[data-path="boot.enabled"] input');
      if (inp) inp.checked = on;
    }
    if (on && runNow) {
      const r = await api('/api/boot/run', { method: 'POST', body: '{}' });
      toast(r.ok ? '✓ ' + r.msg : '✗ ' + (r.error || '试跑失败'), r.ok ? 'ok' : 'err');
    }
  } catch (e) {
    toast('设置失败：' + e.message, 'err');
  }
}

$('#btnBoot').onclick = async () => {
  if (!BOOT) return;
  const on = !BOOT.enabled;
  const body = el('div');
  if (on) {
    const s = BOOT.settings || {};
    body.innerHTML =
      '<p>开启后，每次<b>登录 Windows</b> 会自动按这个顺序拉起，全程不弹黑框：</p>' +
      '<ol style="margin:8px 0 8px 20px;line-height:1.8">' +
      `<li>等 ${s.delay != null ? s.delay : 10} 秒，让网络和桌面先就位</li>` +
      `<li>语音服务${s.tts ? '' : '（未开启）'}</li>` +
      '<li>机器人本体 —— 先占住端口，NapCat 才能连上来</li>' +
      '<li>BiliNote 视频笔记后端（视频推送关着时跳过）</li>' +
      `<li>NapCat${s.napcat === false ? '（未开启）' : ''} —— 登录 QQ</li>` +
      `<li>管理面板${s.panel ? '' : '（未开启）'}</li>` +
      '</ol>' +
      '<p class="muted">写入 <code>HKCU\\…\\Run\\qq-companion</code>，' +
      '不需要管理员权限。想改拉起哪些程序，去「配置参数 → 开机自启」。</p>';
    modal('开启开机自启？', body, [
      { label: '取消' },
      { label: '现在试跑一次', onClick: () => setBoot(true, true) },
      { label: '只开启', primary: true, onClick: () => setBoot(true, false) },
    ]);
  } else {
    body.innerHTML = '<p>关掉之后，下次登录 Windows 就不会自动启动了。</p>' +
      '<p class="muted">当前正在跑的这个进程不受影响 —— 要停它请按旁边的「停止」。</p>';
    modal('关闭开机自启？', body, [
      { label: '取消' },
      { label: '关闭', primary: true, onClick: () => setBoot(false, false) },
    ]);
  }
};

/* ══════════════ 视频推送 ══════════════ */
$('#btnVideopush').onclick = async () => {
  try {
    const r = await api('/api/videopush/run', { method: 'POST', body: '{}' });
    toast(r.ok ? '✓ ' + r.msg : '✗ ' + (r.error || '排队失败'), r.ok ? 'ok' : 'err');
  } catch (e) {
    toast('排队失败：' + e.message, 'err');
  }
};

/* ══════════════ 日志 ══════════════ */
function startLogPolling() {
  stopLogPolling();
  const iv = parseInt($('#logInterval').value, 10);
  pollLogs();
  if (iv > 0) logTimer = setInterval(pollLogs, iv);
}
function stopLogPolling(clearTimerOnly) {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  if (!clearTimerOnly) return;
}
$('#logInterval').onchange = () => { if ($('#tab-logs').classList.contains('active')) startLogPolling(); };
$('#logFollow').onchange = () => { if ($('#logFollow').checked) scrollLogBottom(); };
$('#logFilter').oninput = renderLog;

async function pollLogs() {
  if (logPaused) return;
  try {
    const d = await api('/api/logs?offset=' + logOffset);
    if (d.reset) { logLines = []; logOffset = 0; }
    logOffset = d.offset || 0;
    logSize = d.size || 0;
    if (d.text) {
      logLines = logLines.concat(d.text.split('\n'));
      if (logLines.length > 4000) logLines = logLines.slice(-3000);
      renderLog();
    }
    $('#logMeta').textContent = `文件 ${fmtSize(logSize)}　已读 ${logOffset} 字节　缓存 ${logLines.length} 行`;
    if (!d.exists) $('#logMeta').textContent = '日志文件还没生成 —— 机器人启动后才会写。';
  } catch (e) {
    $('#logMeta').textContent = '读取日志出错：' + e.message;
  }
}

function renderLog() {
  const box = $('#logBox');
  const raw = $('#logFilter').value.trim();
  let re = null, plain = null;
  if (raw) {
    try { re = new RegExp(raw, 'i'); } catch (e) { plain = raw.toLowerCase(); }
  }
  const keep = [];
  for (const line of logLines) {
    if (!line && keep.length && !keep[keep.length - 1]) continue;
    if (raw) {
      const hit = re ? re.test(line) : line.toLowerCase().includes(plain);
      if (!hit) continue;
    }
    let html = esc(line);
    if (raw && re) {
      try { html = esc(line).replace(new RegExp('(' + raw + ')', 'gi'), '<mark>$1</mark>'); } catch (e) {}
    }
    const m = line.match(/\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/);
    keep.push(m ? `<span class="L-${m[1]}">${html}</span>` : html);
  }
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  box.innerHTML = keep.join('\n') || (raw ? '（没有匹配的行）' : '（还没有日志）');
  if ($('#logFollow').checked && atBottom) scrollLogBottom();
}
function scrollLogBottom() {
  const box = $('#logBox');
  box.scrollTop = box.scrollHeight;
}
$('#btnLogToggle').onclick = () => {
  logPaused = !logPaused;
  $('#btnLogToggle').textContent = logPaused ? '继续' : '暂停';
  $('#btnLogToggle').className = 'btn small' + (logPaused ? '' : ' primary');
};
$('#btnLogClear').onclick = async () => {
  const body = el('div');
  body.innerHTML = '<p>清空 <code>data/companion.log</code>。</p>' +
    '<p>正在跑的机器人会继续往同一个文件写，所以清空之后马上又会有新行。</p>';
  modal('清空日志？', body, [
    { label: '取消' },
    {
      label: '清空', primary: true, onClick: async () => {
        try {
          const d = await api('/api/logs/clear', { method: 'POST', body: '{}' });
          logLines = []; logOffset = 0; renderLog();
          toast(d.msg || '已清空', d.ok ? 'ok' : 'err');
        } catch (e) { toast(e.message, 'err'); }
      }
    },
  ]);
};

/* ══════════════ 花费与记忆 ══════════════ */
async function loadStats() {
  let d;
  try { d = await api('/api/stats'); }
  catch (e) { toast('读花费数据失败：' + e.message, 'err'); return; }

  const cards = $('#statCards');
  cards.innerHTML = '';
  if (!d.exists) {
    cards.appendChild(el('div', 'card',
      '<h3>还没有数据</h3><div class="cardSub">数据库还没建 —— 机器人第一次启动后会出现在 ' +
      esc(d.dbPath || 'data/companion.db') + '</div>'));
    return;
  }

  const mk = (title, big, unit, sub) => {
    const c = el('div', 'card');
    c.appendChild(el('h3', null, esc(title)));
    c.appendChild(el('div', 'big', esc(big) + (unit ? `<span class="u">${esc(unit)}</span>` : '')));
    if (sub) c.appendChild(el('div', 'cardSub', sub));
    return c;
  };

  const budget = getP(CFG, 'budget.daily_yuan') || 0;
  const pct = budget > 0 ? (d.todayYuan / budget * 100) : 0;
  cards.appendChild(mk('今天花了',
    fmtYuan(d.todayYuan), '元',
    `预算 ${budget} 元　已用 ${pct.toFixed(1)}%${pct > 100 ? '　⚠ 已进省电模式' : ''}`));
  cards.appendChild(mk('今天的请求', String(d.todayCalls), '次',
    d.hitRate == null ? '还没有调用' :
      `缓存命中率 ${d.hitRate}%${d.hitRate < 60 ? '　⚠ 偏低，说明 prefix 被破坏了' : ''}`));
  cards.appendChild(mk('今天输出', String(d.todayOutput), 'token',
    `未命中输入 ${(d.todayMiss / 1000).toFixed(1)}K tok　命中输入 ${(d.todayHit / 1000).toFixed(1)}K tok`));
  cards.appendChild(mk('长期记忆', String(d.facts), '条',
    `${d.sessions} 个会话　共 ${d.messages} 条消息`));

  // 每日柱状
  const bars = $('#dailyBars');
  bars.innerHTML = '';
  const max = Math.max(0.000001, ...d.daily.map(x => x.yuan));
  d.daily.forEach(x => {
    const b = el('div', 'bar');
    b.appendChild(el('div', 'val', x.yuan ? fmtYuan(x.yuan) : ''));
    const fill = el('div', 'fill');
    fill.style.height = Math.max(2, (x.yuan / max) * 100) + '%';
    fill.title = `${x.day}: ${fmtYuan(x.yuan)} 元 / ${x.calls} 次`;
    b.appendChild(fill);
    b.appendChild(el('div', 'lab', esc(x.day)));
    bars.appendChild(b);
  });

  // 按用途
  const tb = $('#purposeTbl').querySelector('tbody');
  tb.innerHTML = '';
  if (!d.byPurpose.length) {
    tb.appendChild(el('tr', null, '<td class="muted">今天还没有调用</td>'));
  } else {
    const head = el('tr');
    ['用途', '次数', '花费(元)', '命中率'].forEach(h => head.appendChild(el('th', null, h)));
    tb.appendChild(head);
    const NAME = { chat: '聊天回复', summarize: '压缩摘要', memory: '记忆抽取', proactive: '主动搭话' };
    d.byPurpose.forEach(r => {
      const tr = el('tr');
      tr.appendChild(el('td', null, esc(NAME[r.purpose] || r.purpose)));
      tr.appendChild(el('td', 'num', r.calls));
      tr.appendChild(el('td', 'num', fmtYuan(r.yuan)));
      tr.appendChild(el('td', 'num', r.hitRate == null ? '—' : r.hitRate + '%'));
      tb.appendChild(tr);
    });
  }

  // 记忆 / 绑定 / 消息
  $('#factCount').textContent = d.facts ? `（共 ${d.facts} 条，下面是最近几条）` : '';
  const fl = $('#factList'); fl.innerHTML = '';
  if (!d.recentFacts.length) {
    fl.appendChild(el('li', null, '<span class="b muted">还没有长期记忆。默认聊满 6 轮才会抽第一次，别急。</span>'));
  }
  d.recentFacts.forEach(f => {
    const li = el('li');
    li.appendChild(el('span', 't', fmtTime(f.createdAt)));
    li.appendChild(el('span', 'b', esc(f.content)));
    li.appendChild(el('span', 'imp', '★' + f.importance.toFixed(1)));
    li.title = `被想起过 ${f.accessCount} 次　${f.session}`;
    fl.appendChild(li);
  });

  const al = $('#allowList'); al.innerHTML = '';
  if (!d.allowlist.length) {
    al.appendChild(el('li', null, '<span class="b muted">还没有绑定任何人。拿你的主号给小号发一条消息就会自动绑定。</span>'));
  }
  d.allowlist.forEach(a => {
    const li = el('li');
    li.appendChild(el('span', 't', fmtTime(a.addedAt)));
    li.appendChild(el('span', 'b', esc(a.nickname || '（无昵称）') + '　' + a.uid));
    al.appendChild(li);
  });

  const ml = $('#msgList'); ml.innerHTML = '';
  d.recentMessages.forEach(m => {
    const li = el('li');
    li.appendChild(el('span', 't', fmtTime(m.createdAt)));
    li.appendChild(el('span', 'b', (m.role === 'user' ? '他：' : '它：') + esc(m.content)));
    ml.appendChild(li);
  });
  if (!d.recentMessages.length) ml.appendChild(el('li', null, '<span class="b muted">还没有消息</span>'));
}

/* ══════════════ 声音（GPT-SoVITS） ══════════════
 * 这一页是唯一一处「不哑」的前端：它管的不是配置项，而是一堆文件和
 * 一个外部服务的状态，没有统一的 type 可以套。所以 DOM 结构写死在这里。
 *
 * 三条约定：
 *   1. 所有接口失败都返回 {ok:false,error}，这里原样弹出 —— 不要自己编原因。
 *   2. 合成音频走 /api/voice/audio?f=<uuid>，加时间戳防缓存。
 *   3. 情绪不是「参数」，是「用哪段参考音频」。没有参考音频就没有情绪。
 */
const EMOTIONS = [
  ['neutral', '平静'], ['happy', '开心'], ['sad', '低落'],
  ['angry', '不耐烦'], ['gentle', '温柔'],
];
let VOICE = { status: null, refs: [], emotions: {}, groups: [], current: {} };

const baseName = p => (p ? String(p).split(/[\\/]/).pop() : '');
function fmtSec(s) {
  if (s == null) return '—';
  return (s >= 10 ? s.toFixed(1) : s.toFixed(2)) + ' 秒';
}
function emotionLabel(v) {
  const hit = EMOTIONS.find(e => e[0] === v);
  return hit ? hit[1] : (v || '');
}

async function loadVoice(silent) {
  try {
    const st = await api('/api/voice/status');
    VOICE.status = st;
    const [w, r] = await Promise.all([api('/api/voice/weights'), api('/api/voice/refs')]);
    VOICE.groups = w.groups || [];
    VOICE.current = w.current || st.current || {};
    VOICE.refs = r.refs || [];
    VOICE.emotions = r.emotions || {};
    renderVoiceStatus();
    renderVoiceWeights();
    renderVoiceRefs();
    fillVoiceSelects();
  } catch (e) {
    if (!silent) toast('读取声音信息失败：' + e.message, 'err');
  }
}

function renderVoiceStatus() {
  const s = VOICE.status || {};
  const pill = $('#vPill');
  pill.className = 'pill' + (s.apiAlive ? '' : ' off');
  $('#vPillText').textContent = s.apiAlive ? '语音服务在线' : '语音服务未连接';
  $('#vMeta').textContent = s.apiBase || '';

  const miss = $('#vMissing');
  const missing = s.missing || [];
  miss.hidden = !missing.length;
  if (missing.length) {
    miss.innerHTML = missing.map(m =>
      '<div class="issueRow err"><span class="lv">问题</span><span>' + esc(m) + '</span></div>').join('');
  }

  const note = $('#vNote');
  const off = s.settings && s.settings.enabled === false;
  const rawAlways = !!(s.settings && s.settings.alwaysVoice);
  const always = rawAlways && !off;
  note.hidden = !(off || always);
  note.className = 'issueBar';
  if (off) {
    note.innerHTML = '<div class="issueRow warn"><span class="lv">提示</span><span>' +
      '机器人现在还不会发语音（tts.enabled = false）。这一页的试听和克隆照常能用 —— ' +
      '想让它开口，去「配置参数 → 语音（TTS）」把开关打开。</span></div>';
  } else if (always) {
    note.innerHTML = '<div class="issueRow warn"><span class="lv">全语音</span><span>' +
      '每条回复都会变成语音条（群里也一样），每条多等 3~5 秒。点右上角「全语音模式：开」可以关掉，' +
      '关掉后立刻回到原来的概率设置。</span></div>';
  }

  const btnAlways = $('#vAlways');
  if (btnAlways) {
    btnAlways.textContent = '全语音模式：' + (rawAlways ? '开' : '关');
    btnAlways.className = 'btn small' + (rawAlways ? ' primary' : '');
  }

  const cur = s.current || {};
  const gpu = s.gpu;
  const cards = [
    ['语音服务', s.apiAlive ? '在线' : '未连接', s.apiAlive ? '可以合成' : (s.apiWhy || '')],
    ['当前音色（SoVITS）', baseName(cur.sovits) || '预训练默认', '决定像谁'],
    ['当前 GPT 权重', baseName(cur.gpt) || '预训练默认', '决定怎么说话'],
    ['参考音频', String(s.refs == null ? 0 : s.refs) + ' 条',
      Object.keys(s.emotions || {}).length + ' 个情绪已绑定'],
    ['显存', gpu ? (gpu.usedMiB / 1024).toFixed(1) + ' / ' + (gpu.totalMiB / 1024).toFixed(1) + ' GB' : '—',
      gpu ? gpu.name : '读不到（没装 nvidia-smi？）'],
  ];
  $('#vCards').innerHTML = cards.map(c =>
    '<div class="card"><div class="muted">' + esc(c[0]) + '</div>' +
    '<div class="big" style="font-size:18px;line-height:1.5">' + esc(c[1]) + '</div>' +
    '<div class="muted">' + esc(c[2] || '') + '</div></div>').join('');

  $('#vCurrent').textContent = baseName(cur.sovits) ? '· ' + baseName(cur.sovits) : '';
}

function renderVoiceWeights() {
  const tb = $('#vWeights tbody');
  const groups = VOICE.groups || [];
  const others = (VOICE.current && VOICE.current.sovits) ? baseName(VOICE.current.sovits) : '';
  $('#vWeightCount').textContent = groups.length ? '（' + groups.length + ' 组）' : '（还没有微调权重）';
  tb.innerHTML = '';

  if (!groups.length) {
    tb.innerHTML = '<tr><td class="muted">还没有训练出来的音色。零样本克隆就够了 —— ' +
      '在右边传一段参考音频，不用等训练。</td></tr>';
    return;
  }

  groups.forEach(g => {
    const label = g.label || g.name;
    const hasRef = (VOICE.refs || []).some(r => r.file === label + '.wav');
    const tr = el('tr');
    const tdName = el('td');
    tdName.innerHTML = '<b>' + esc(label) + '</b>' +
      (g.sovits && g.sovits.file === others ? '<span class="muted"> ·使用中</span>' : '') +
      (hasRef ? '<span class="muted"> ·自带参考音频</span>' : '');
    tr.appendChild(tdName);

    tr.appendChild(el('td', 'muted', g.gpt ? esc(g.gpt.file) : '缺（只切 SoVITS）'));
    tr.appendChild(el('td', 'muted', g.sovits ? esc(g.sovits.file) : '缺（只切 GPT）'));

    const tdBtn = el('td');
    const btn = el('button', 'btn small', '用这个');
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = '切换中…';
      try {
        const r = await api('/api/voice/use', {
          method: 'POST',
          body: JSON.stringify({
            gpt: g.gpt ? g.gpt.path : '',
            sovits: g.sovits ? g.sovits.path : '',
            label: label
          })
        });
        if (!r.ok) throw new Error(r.error || '切换失败');
        toast(r.msg || ('已切到「' + label + '」'), 'ok');
        await loadVoice(true);
      } catch (e) {
        toast(e.message, 'err');
      } finally {
        btn.disabled = false;
        btn.textContent = '用这个';
      }
    };
    tdBtn.appendChild(btn);
    tr.appendChild(tdBtn);
    tb.appendChild(tr);
  });
}

function renderVoiceRefs() {
  const box = $('#vRefs');
  const refs = VOICE.refs || [];
  $('#vRefCount').textContent = refs.length ? '（' + refs.length + ' 条）' : '';
  box.innerHTML = '';

  if (!refs.length) {
    box.appendChild(el('p', 'muted',
      'refs 目录还是空的。先在右边上传一段参考音频 —— 3~10 秒、单人、没有背景音乐。'));
    return;
  }

  refs.forEach(r => {
    const row = el('div', 'refRow');

    const main = el('div', 'refMain');
    main.appendChild(el('div', null,
      '<b>' + esc(r.file) + '</b> <span class="muted">' +
      fmtSec(r.duration) + ' · ' + fmtSize(r.size) + ' · ' + fmtTime(r.mtime) + '</span>' +
      (r.emotion ? ' <span class="tag">' + esc(emotionLabel(r.emotion)) + '</span>' : '')));
    main.appendChild(el('div', 'muted',
      r.text ? esc(r.text) : '没写参考文本 —— 合成会明显掉相似度，建议补上同名 .txt'));
    const audio = el('audio');
    audio.controls = true;
    audio.preload = 'none';
    audio.src = '/api/voice/ref-audio?f=' + encodeURIComponent(r.file) +
      '&t=' + Math.round((r.mtime || 0) * 1000);
    main.appendChild(audio);
    row.appendChild(main);

    const side = el('div', 'refSide');
    const sel = el('select');
    sel.innerHTML = '<option value="">不绑定情绪</option>' +
      EMOTIONS.map(e => '<option value="' + e[0] + '"' +
        (r.emotion === e[0] ? ' selected' : '') + '>' + e[1] + '（' + e[0] + '）</option>').join('');
    sel.onchange = async () => {
      try {
        let d;
        if (!sel.value) {
          if (!r.emotion) return;
          d = await api('/api/voice/refs/unbind',
            { method: 'POST', body: JSON.stringify({ emotion: r.emotion }) });
        } else {
          d = await api('/api/voice/refs/bind',
            { method: 'POST', body: JSON.stringify({ emotion: sel.value, file: r.file }) });
        }
        if (!d.ok) throw new Error(d.error || '保存失败');
        toast(d.msg || '已保存', 'ok');
        await loadVoice(true);
      } catch (e) {
        toast(e.message, 'err');
      }
    };
    side.appendChild(sel);

    const del = el('button', 'btn small', '删除');
    del.onclick = async () => {
      if (!confirm('把 ' + r.file + ' 移到 _trash？（不会真删，可以手动找回）')) return;
      try {
        const d = await api('/api/voice/refs/delete',
          { method: 'POST', body: JSON.stringify({ file: r.file }) });
        if (!d.ok) throw new Error(d.error || '删除失败');
        toast(d.msg || '已删除', 'ok');
        await loadVoice(true);
      } catch (e) {
        toast(e.message, 'err');
      }
    };
    side.appendChild(del);
    row.appendChild(side);
    box.appendChild(row);
  });
}

function fillVoiceSelects() {
  const emo = $('#vEmotion');
  const bound = Object.keys(VOICE.emotions || {});
  const defEmo = (VOICE.status && VOICE.status.settings && VOICE.status.settings.defaultEmotion) || 'neutral';
  const opts = [];
  EMOTIONS.forEach(e => {
    const isBound = bound.indexOf(e[0]) >= 0;
    opts.push('<option value="' + e[0] + '"' + (e[0] === defEmo ? ' selected' : '') + '>' +
      e[1] + (isBound ? '' : '（未绑定参考音频）') + '</option>');
  });
  emo.innerHTML = '<option value="">（用默认）</option>' + opts.join('');

  const ref = $('#vRef');
  ref.innerHTML = '<option value="">（按情绪自动选）</option>' +
    (VOICE.refs || []).map(r => '<option value="' + esc(r.file) + '">' + esc(r.file) + '</option>').join('');

  const bind = $('#vBind');
  const cur = bind.value;
  bind.innerHTML = '<option value="">不绑定情绪</option>' +
    EMOTIONS.map(e => '<option value="' + e[0] + '">' + e[1] + '（' + e[0] + '）</option>').join('');
  if (cur) bind.value = cur;
}

async function doVoicePreview() {
  const text = ($('#vText').value || '').trim();
  if (!text) return toast('先打一句话', 'err');
  const btn = $('#vPreview');
  btn.disabled = true;
  btn.textContent = '合成中…';
  try {
    const d = await api('/api/voice/preview', {
      method: 'POST',
      body: JSON.stringify({
        text: text,
        emotion: $('#vEmotion').value,
        ref: $('#vRef').value,
        speed: Number($('#vSpeed').value || 1),
      }),
    });
    if (!d.ok) throw new Error(d.error || '合成失败');
    const a = $('#vPlayer');
    a.src = d.url + '&t=' + Date.now();
    a.play().catch(() => {});
    $('#vPreviewMeta').textContent =
      '参考音频 ' + (d.ref || '—') + '　时长 ' + fmtSec(d.seconds) + '　大小 ' + fmtSize(d.bytes);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '合成试听';
  }
}

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result || ''));
    fr.onerror = () => reject(new Error('读文件失败'));
    fr.readAsDataURL(file);
  });
}

async function doVoiceUpload() {
  const input = $('#vFile');
  const f = input.files && input.files[0];
  if (!f) return toast('先选一个 wav 文件', 'err');
  if (f.size > 20 * 1048576) return toast('文件太大（上限 20 MB）', 'err');
  if (!/\.wav$/i.test(f.name)) return toast('只支持 wav。用 ffmpeg 转一下：ffmpeg -i 输入.mp3 输出.wav', 'err');

  const name = ($('#vName').value || '').trim() || f.name.replace(/\.wav$/i, '');
  const text = ($('#vPrompt').value || '').trim();
  if (!text) toast('没填参考文本，相似度会明显下降（可以先这样试）', 'err');

  const btn = $('#vUpload');
  btn.disabled = true;
  btn.textContent = '上传中…';
  try {
    const data = await readBase64(f);
    const d = await api('/api/voice/refs/upload', {
      method: 'POST',
      body: JSON.stringify({ name: name, data: data, text: text, emotion: $('#vBind').value }),
    });
    if (!d.ok) throw new Error(d.error || '上传失败');
    toast(d.msg || '已上传', 'ok');
    input.value = '';
    $('#vName').value = '';
    $('#vPrompt').value = '';
    await loadVoice(true);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '上传并启用';
  }
}

async function voiceService(action) {
  try {
    const d = await api('/api/voice/service',
      { method: 'POST', body: JSON.stringify({ action: action }) });
    if (!d.ok) throw new Error(d.error || '操作失败');
    toast(d.msg || '已执行', 'ok');
    setTimeout(() => loadVoice(true), 1500);
  } catch (e) {
    toast(e.message, 'err');
  }
}

$('#vRefresh').onclick = () => loadVoice();
$('#vPreview').onclick = doVoicePreview;
$('#vUpload').onclick = doVoiceUpload;
$('#vStart').onclick = () => voiceService('start');
$('#vRestart').onclick = () => voiceService('restart');
$('#vAlways').onclick = () => toggleAlwaysVoice();

/* 全语音模式：一个覆盖开关。开了每条回复都发语音，关掉回到原来的 trigger/chance。
 * 它只动 tts.always_voice 这一个布尔值，所以「关掉之后保留原来的设置」是天然成立的。 */
async function toggleAlwaysVoice() {
  const s = (VOICE.status && VOICE.status.settings) || {};
  const next = !s.alwaysVoice;
  if (next) {
    if (s.enabled === false) {
      toast('先把「允许发语音」打开（配置参数 → 语音（TTS）→ 允许发语音），\n否则全语音模式也发不出声。', 'err');
      return;
    }
    if (!confirm('开启后每条回复都会变成语音条，群里也一样，每条多等 3~5 秒。\n\n确定开启？')) return;
  }
  try {
    const d = await api('/api/voice/always',
      { method: 'POST', body: JSON.stringify({ on: next }) });
    if (!d.ok) { toast(d.error || '设置失败', 'err'); return; }
    toast(d.msg || (next ? '已开启全语音模式' : '已关闭全语音模式'), 'ok');

    // 配置页那份 CFG 也要跟着改，否则之后在配置页保存别的参数会把这个开关写回去。
    if (CFG && CFG.tts) {
      const wasClean = JSON.stringify(CFG) === SNAPSHOT;
      setP(CFG, 'tts.always_voice', next);
      if (wasClean) SNAPSHOT = JSON.stringify(CFG);
      const inp = $('.param[data-path="tts.always_voice"] input');
      if (inp) inp.checked = next;
    }
    await loadVoice(true);
  } catch (e) {
    toast('设置失败：' + e.message, 'err');
  }
}
$('#vSpeed').addEventListener('input', () => {
  $('#vSpeedVal').textContent = Number($('#vSpeed').value).toFixed(2);
});
$('#vFile').addEventListener('change', () => {
  const f = $('#vFile').files && $('#vFile').files[0];
  $('#vFileInfo').textContent = f ? f.name + '（' + fmtSize(f.size) + '）' : '支持 wav，最大 20 MB';
  if (f && !$('#vName').value) $('#vName').value = f.name.replace(/\.wav$/i, '');
});

/* ══════════════ 启动 ══════════════ */
async function init() {
  try { applyTheme(localStorage.getItem('qc-theme') || 'light'); } catch (e) { applyTheme('light'); }

  let schemaOk = false;
  try {
    SCHEMA = await api('/api/schema');
    SCHEMA.groups.forEach(g => g.params.forEach(p => { P[p.path] = p; }));
    schemaOk = true;
  } catch (e) {
    $('#groups').innerHTML = `<div class="card" style="color:var(--err)">读参数表失败：${esc(e.message)}</div>`;
  }

  if (schemaOk) {
    try { await loadConfig(); }
    catch (e) { toast('读配置失败：' + e.message, 'err'); }
  }

  await loadStatus();
  setInterval(loadStatus, 5000);

  await loadBoot();
  setInterval(loadBoot, 30000);

  // QQ 没登录时，机器人照样「运行中」，但一条消息都收不到 —— 这是最容易误判成
  // 「自启失败」的情况，所以进页面就直说。
  if (BOOT && BOOT.napcat && BOOT.napcat.qqRunning && BOOT.napcat.connected === false) {
    toast('QQ 还没登录成功（NapCat 停在登录界面）。扫一下码，或等最多 5 分钟自动重试。', 'err');
  }

  if (schemaOk && !Object.keys(CFG || {}).length) {
    toast('config.yaml 是空的或读不出来 —— 先跑一次 setup.py。', 'err');
  }
}

init();
