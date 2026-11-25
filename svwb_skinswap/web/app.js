'use strict';
/* 影之诗 WB 主战者换皮 —— 界面逻辑（原生 JS，无依赖） */

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const base = (p) => String(p).split('/').pop();

const CLASSES = [
  { cls: 'all', name: '全部', icon: 'i-all' },
  { cls: 1, name: '精灵', icon: 'i-c1', color: 'var(--c1)' },
  { cls: 2, name: '皇家护卫', icon: 'i-c2', color: 'var(--c2)' },
  { cls: 3, name: '巫师', icon: 'i-c3', color: 'var(--c3)' },
  { cls: 4, name: '龙族', icon: 'i-c4', color: 'var(--c4)' },
  { cls: 5, name: '梦魇', icon: 'i-c5', color: 'var(--c5)' },
  { cls: 6, name: '主教', icon: 'i-c6', color: 'var(--c6)' },
  { cls: 7, name: '超越者', icon: 'i-c7', color: 'var(--c7)' },
  { cls: 99, name: '特殊', icon: 'i-c99', color: 'var(--c99)' },
  { cls: 0, name: '中立', icon: 'i-c0', color: 'var(--c0)' },
];
const CLS_COLOR = Object.fromEntries(CLASSES.filter((c) => c.color).map((c) => [c.cls, c.color]));
const RING = 2 * Math.PI * 52;

const S = {
  state: null,
  skins: [],
  byId: new Map(),
  skinsVer: -1,
  cls: 'all',
  q: '',
  quick: new Set(),
  active: 'a',
  a: null,
  b: null,
  autoClass: false,
  preview: null,
  previewKey: '',
  previewSeq: 0,
  openCat: null,
  off: new Set(),        // 预览里取消勾选（不替换）的部分
  logSeq: 0,
  logs: [],
  unread: 0,
  resultSeq: null,
  offline: false,
  dirOpen: false,
  dirDismissed: false,
  detected: null,
  planSig: '',
};

/* ------------------------------------------------------------------ 偏好 */
function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('svwb-prefs') || '{}');
    if (p.cls !== undefined) S.cls = p.cls;
    if (Array.isArray(p.quick)) S.quick = new Set(p.quick);
  } catch (e) { /* 忽略 */ }
}
function savePrefs() {
  try { localStorage.setItem('svwb-prefs', JSON.stringify({ cls: S.cls, quick: [...S.quick] })); } catch (e) { /* 忽略 */ }
}

/* ------------------------------------------------------------------ 接口 */
async function api(path, body) {
  const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    throw new Error('无法连接到工具后台，请确认程序仍在运行');
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `请求失败（${res.status}）`);
  return data;
}

/* ------------------------------------------------------------------ 提示 */
function toast(kind, title, text, ms = 4200) {
  const icon = { ok: 'i-check', err: 'i-warn', warn: 'i-warn', info: 'i-info' }[kind] || 'i-info';
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<svg class="ico"><use href="#${icon}"/></svg><div><b>${esc(title)}</b>${text ? esc(text) : ''}</div>`;
  $('#toasts').appendChild(el);
  const kill = () => { el.classList.add('out'); setTimeout(() => el.remove(), 220); };
  const t = setTimeout(kill, ms);
  el.addEventListener('click', () => { clearTimeout(t); kill(); });
}
const fail = (e) => toast('err', '操作失败', e.message || String(e), 6000);

/* ------------------------------------------------------------------ 轮询 */
async function poll() {
  try {
    const st = await api('/api/state');
    if (S.offline) toast('ok', '已重新连接到后台');
    S.offline = false;
    onState(st);
  } catch (e) {
    if (!S.offline) {
      S.offline = true;
      toast('err', '与工具后台的连接中断', '如果关掉了程序，请重新打开。', 8000);
      setDot('err');
    }
  }
  const fast = S.state && S.state.task;
  setTimeout(poll, fast ? 400 : 1000);
}

function onState(st) {
  const first = !S.state;
  S.state = st;

  $('#demoBadge').hidden = !st.app.demo;
  renderVersion(st);
  renderDir(st);
  renderBanners(st);
  const auto = $('#autoRescan');
  if (document.activeElement !== auto) auto.checked = !!st.options.autoRescan;

  if (st.skins.ver !== S.skinsVer && !st.skins.loading) loadSkins(st.skins.ver);
  if (st.skins.loading && !S.skins.length) renderSkeleton();

  const sig = JSON.stringify([st.plan, st.applied, st.dirty]);
  if (sig !== S.planSig) { S.planSig = sig; renderPlan(); renderGrid(); }
  renderActions(st);
  renderTask(st.task);

  if (st.logSeq !== S.logSeq) loadLogs();

  if (st.result) {
    if (S.resultSeq === null) S.resultSeq = st.result.seq;
    else if (st.result.seq !== S.resultSeq) {
      S.resultSeq = st.result.seq;
      const r = st.result;
      const nm = { revert: '还原', restore: '恢复', apply: '替换' }[r.kind] || '操作';
      if (r.ok) toast('ok', `${nm}完成`, r.summary + (r.kind === 'apply' ? '。启动游戏即可看到效果。' : ''), 7000);
      else toast('err', `${nm}失败`, r.summary, 9000);
    }
  } else if (S.resultSeq === null) S.resultSeq = 0;

  if (!st.game.ok && !S.dirOpen && !S.dirDismissed) openDir(true);
  if (first) document.body.classList.add('ready');
}

function setDot(kind) {
  const d = $('#dirDot');
  d.className = 'dot' + (kind ? ' ' + kind : '');
}

function renderDir(st) {
  const g = st.game;
  $('#dirText').textContent = g.dir || '未选择游戏目录';
  $('#dirChip').title = g.persist ? `资源目录：${g.persist}\n点击更换` : '点击选择游戏目录';
  setDot(st.task ? 'busy' : g.ok ? 'ok' : g.dir ? 'err' : '');
}

function renderBanners(st) {
  const run = st.game.running;
  $('#bannerRunning').hidden = !run;
  if (run) $('#runningName').textContent = run;
  const m = st.meta;
  const broken = !!(m && m.broken);
  $('#bannerMeta').hidden = !broken;
  if (broken) {
    $('#metaText').textContent = m.canRestore
      ? `检测到游戏把资源索引重置了（会要求重新下载约 ${m.needGb} GB）。缓存文件其实都还在，点“一键恢复”即可，不用重新下载。`
      : `检测到游戏把资源索引重置了（会要求重新下载约 ${m.needGb} GB），但没有找到可用的备份，只能在游戏里重新下载。`;
    $('#btnRestoreMeta').hidden = !m.canRestore;
    $('#btnRestoreMeta').disabled = !!run || !!st.task;
  }
  const h = st.health;
  const showH = !!(h && h.need && st.plan.length && !st.dirty && !st.task);
  $('#bannerHealth').hidden = !showH;
  if (showH) $('#healthText').textContent = `替换需要更新：${h.reason}。`;
  $('#bannerApply').disabled = !!run;
}

/* ------------------------------------------------------------------ 皮肤列表 */
async function loadSkins(ver) {
  S.skinsVer = ver;
  try {
    const d = await api('/api/skins');
    S.skinsVer = d.ver;
    S.skins = d.skins || [];
    S.byId = new Map(S.skins.map((s) => [s.id, s]));
    if (S.a && !S.byId.has(S.a)) S.a = null;
    if (S.b && !S.byId.has(S.b)) S.b = null;
    applyUrlParams();
    renderClassBar();
    renderGrid();
    renderSlots();
    renderPlan();
    if (S.a && S.b) loadPreview();
  } catch (e) {
    S.skinsVer = -1;
    fail(e);
  }
}

/* 地址栏参数（?a=1202&b=1203&cls=3）：方便直接打开某一组，只在首次加载时生效 */
let urlParamsUsed = false;
function applyUrlParams() {
  if (urlParamsUsed) return;
  urlParamsUsed = true;
  const q = new URLSearchParams(location.search);
  const a = q.get('a'), b = q.get('b'), cls = q.get('cls');
  if (a && S.byId.has(a)) S.a = a;
  if (b && S.byId.has(b) && b !== S.a) S.b = b;
  if (cls) S.cls = cls === 'all' ? 'all' : Number(cls);
  if (S.a) setActive('b');
}

function matchText(s, q) {
  if (!q) return true;
  return [s.name, s.nameJp, s.id, s.cv, s.cvEn, s.clsName].some((v) => v && String(v).toLowerCase().includes(q));
}
function matchQuick(s) {
  if (S.quick.has('deluxe') && !s.deluxe) return false;
  if (S.quick.has('voice') && !s.hasVoice) return false;
  if (S.quick.has('evolve') && !s.hasEvolve) return false;
  if (S.quick.has('evopoint') && !s.hasEvoPoint) return false;
  return true;
}
function filtered() {
  const q = S.q.trim().toLowerCase();
  return S.skins.filter((s) => (S.cls === 'all' || s.cls === S.cls) && matchQuick(s) && matchText(s, q));
}

function renderClassBar() {
  const q = S.q.trim().toLowerCase();
  const pool = S.skins.filter((s) => matchQuick(s) && matchText(s, q));
  const present = new Set(S.skins.map((s) => s.cls));
  const bar = $('#classBar');
  bar.innerHTML = '';
  CLASSES.forEach((c, i) => {
    if (c.cls !== 'all' && !present.has(c.cls)) return;
    const n = c.cls === 'all' ? pool.length : pool.filter((s) => s.cls === c.cls).length;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'cchip' + (S.cls === c.cls ? ' on' : '');
    b.dataset.cls = c.cls;
    if (c.color) b.style.setProperty('--cc', c.color);
    b.title = i < 9 ? `快捷键 ${i}` : '';
    b.setAttribute('aria-pressed', S.cls === c.cls);
    b.innerHTML = `<svg class="ico"><use href="#${c.icon}"/></svg>${esc(c.name)}<span class="n">${n}</span>`;
    bar.appendChild(b);
  });
}

function renderSkeleton() {
  const g = $('#grid');
  if (g.dataset.skeleton) return;
  g.dataset.skeleton = '1';
  g.innerHTML = Array.from({ length: 18 }, () => '<div class="skeleton"></div>').join('');
  $('#empty').hidden = true;
}

function planTargets() { return new Set((S.state ? S.state.plan : []).map((p) => p.target)); }

function renderGrid() {
  if (!S.skins.length) return;
  const g = $('#grid');
  delete g.dataset.skeleton;
  const list = filtered();
  const inPlan = planTargets();
  const frag = document.createDocumentFragment();
  for (const s of list) {
    const b = document.createElement('button');
    b.type = 'button';
    const pickingB = S.active === 'b';
    const disabled = pickingB && !s.downloaded;
    b.className = 'skin' + (S.a === s.id ? ' sel-a' : '') + (S.b === s.id ? ' sel-b' : '') + (!s.downloaded ? ' nodl' : '') + (disabled ? ' disabled' : '') + (inPlan.has(s.id) ? ' in-plan' : '');
    b.dataset.id = s.id;
    b.style.setProperty('--cc', CLS_COLOR[s.cls] || 'var(--c0)');
    b.setAttribute('role', 'option');
    b.setAttribute('aria-selected', S.a === s.id || S.b === s.id);
    b.title = `${s.name}（${s.nameJp}）\n${s.clsName} · ID ${s.id}${s.cv ? '\n声优：' + s.cv : ''}${!s.downloaded ? '\n未下载到本地' : ''}`;
    const feats = [];
    if (s.deluxe) feats.push('<span class="gold" title="豪华版（有豪华进化演出）"><svg class="ico"><use href="#i-crown"/></svg></span>');
    if (s.hasVoice) feats.push('<span title="有语音"><svg class="ico"><use href="#i-voice"/></svg></span>');
    if (s.hasEvolve) feats.push('<span title="有进化演出"><svg class="ico"><use href="#i-evo"/></svg></span>');
    if (s.hasEvoPoint) feats.push('<span title="有进化点插图"><svg class="ico"><use href="#i-orb"/></svg></span>');
    let tag = '';
    if (S.a === s.id) tag = '<span class="tag tag-a">我拥有的</span>';
    else if (S.b === s.id) tag = '<span class="tag tag-b">想显示的</span>';
    else if (!s.downloaded) tag = '<span class="tag tag-dl">未下载</span>';
    b.innerHTML = `<img class="thumb" loading="lazy" decoding="async" alt="" src="/img/thumb/${s.id}">` +
      `<span class="shade"></span><span class="feats">${feats.join('')}</span>${tag}` +
      `<span class="info"><span class="nm">${esc(s.name)}</span><span class="sub"><span class="cl">${esc(s.clsName)}</span><span class="id">#${s.id}</span></span></span>`;
    const img = b.firstChild;
    img.addEventListener('error', () => {
      const ph = document.createElement('span');
      ph.className = 'ph';
      ph.textContent = (s.name || '?').slice(0, 1);
      img.replaceWith(ph);
    }, { once: true });
    frag.appendChild(b);
  }
  g.innerHTML = '';
  g.appendChild(frag);
  $('#empty').hidden = list.length > 0;
  $('#resultCount').textContent = `${list.length} / ${S.skins.length} 个皮肤`;
  renderSameClassHint();
}

function renderSameClassHint() {
  let el = $('#sameClass');
  const show = S.autoClass && S.active === 'b' && S.cls !== 'all';
  if (!show) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement('span');
    el.id = 'sameClass';
    el.className = 'same-class';
    $('.quickbar').insertBefore(el, $('#resultCount'));
  }
  const c = CLASSES.find((x) => x.cls === S.cls);
  el.innerHTML = `已切到同职业：${esc(c ? c.name : '')}<button type="button" id="showAll">显示全部</button>`;
  $('#showAll').onclick = () => setClass('all');
}

function setClass(cls) {
  S.cls = cls;
  S.autoClass = false;
  savePrefs();
  renderClassBar();
  renderGrid();
  $('#gridWrap').scrollTop = 0;
}

/* ------------------------------------------------------------------ 选择两侧 */
function setActive(slot) {
  S.active = slot;
  $('#slotA').classList.toggle('is-active', slot === 'a');
  $('#slotB').classList.toggle('is-active', slot === 'b');
  const p = $('#pickingHint');
  p.classList.toggle('b', slot === 'b');
  const a = S.a && S.byId.get(S.a);
  $('#pickingText').textContent = slot === 'a'
    ? '第 1 步：点选你拥有的皮肤（游戏里装备的那个）'
    : `第 2 步：点选想显示成的皮肤${a ? ` · 「${a.name}」将显示为它` : ''}`;
  renderGrid();
}

function pick(id) {
  const s = S.byId.get(id);
  if (!s) return;
  if (S.active === 'a') {
    if (S.b === id) S.b = null;
    S.a = id;
    if (!S.b) {
      if (S.cls === 'all' || S.cls !== s.cls) { S.cls = s.cls; S.autoClass = true; renderClassBar(); }
      setActive('b');
      $('#gridWrap').scrollTop = 0;
    } else {
      setActive('a');
    }
  } else {
    if (!s.downloaded) { toast('warn', '这个皮肤还没下载到本地', '先在游戏里查看一次该皮肤让它下载，再回来选择。'); return; }
    if (S.a === id) { toast('warn', '两侧不能是同一个皮肤', '请选一个不同的皮肤来显示。'); return; }
    S.b = id;
    renderGrid();
  }
  renderSlots();
  loadPreview();
}

function renderSlots() {
  for (const k of ['a', 'b']) {
    const id = S[k];
    const s = id && S.byId.get(id);
    const slot = $(k === 'a' ? '#slotA' : '#slotB');
    const img = $(k === 'a' ? '#artA' : '#artB');
    slot.style.setProperty('--cc', s ? (CLS_COLOR[s.cls] || 'var(--c0)') : 'transparent');
    $(k === 'a' ? '#emptyA' : '#emptyB').hidden = !!s;
    if (s) {
      if (img.dataset.id !== id) {
        img.hidden = false;
        img.dataset.id = id;
        img.onerror = () => { img.hidden = true; };
        img.src = `/img/art/${id}`;
        img.style.animation = 'none';
        void img.offsetWidth;
        img.style.animation = '';
      }
      $(k === 'a' ? '#nameA' : '#nameB').textContent = s.name;
      $(k === 'a' ? '#metaA' : '#metaB').innerHTML = `<b>${esc(s.clsName)}</b> · #${s.id}${s.cv ? ' · ' + esc(s.cv) : ''}`;
    } else {
      img.hidden = true;
      img.removeAttribute('src');
      delete img.dataset.id;
      $(k === 'a' ? '#nameA' : '#nameB').textContent = '未选择';
      $(k === 'a' ? '#metaA' : '#metaB').textContent = k === 'a' ? '会被替换的那个' : '模型、语音、字幕的来源';
    }
  }
}

/* ------------------------------------------------------------------ 预览 */
async function loadPreview() {
  const { a, b } = S;
  if (!a || !b) {
    $('#preview').hidden = true;
    $('#previewLoading').hidden = true;
    S.preview = null;
    S.previewKey = '';
    return;
  }
  const key = `${a}>${b}`;
  if (key === S.previewKey && S.preview) { renderPreview(); return; }
  S.previewKey = key;
  const seq = ++S.previewSeq;
  $('#preview').hidden = true;
  $('#previewLoading').hidden = false;
  try {
    const p = await api(`/api/preview?target=${encodeURIComponent(a)}&source=${encodeURIComponent(b)}`);
    if (seq !== S.previewSeq) return;
    S.preview = p;
    S.openCat = null;
    S.off = initialOff(p);
    renderPreview();
  } catch (e) {
    if (seq === S.previewSeq) { $('#previewLoading').hidden = true; S.previewKey = ''; fail(e); }
  }
}

/* 选择替换哪些部分：S.off = 取消勾选的类别 key（默认全选） */
function allParts() { return (S.state && S.state.parts) || []; }
function planEntry(target) { return S.state ? S.state.plan.find((x) => x.target === target) : null; }
function offFromParts(parts) {
  if (!parts) return new Set();
  return new Set(allParts().filter((k) => !parts.includes(k)));
}
function initialOff(p) {
  const e = planEntry(p.target);
  return e && e.source === p.source ? offFromParts(e.parts) : new Set();
}
function selectedParts(p) {
  // 只把预览里出现的类别算作“取消勾选”；全选时返回 null（= 全部替换）
  const off = [...S.off].filter((k) => p.categories.some((c) => c.key === k));
  if (!off.length) return null;
  return allParts().filter((k) => !off.includes(k));
}
function sameParts(x, y) {
  const norm = (v) => (v ? [...v].sort() : null);
  return JSON.stringify(norm(x)) === JSON.stringify(norm(y));
}
function partsSummary(parts) {
  if (!parts) return '';
  const names = { model: '模型', vs: '对战立绘', evolve: '进化演出', evo_point: '进化点插图', battle_ui: '战斗界面',
    portrait: '头像与卡组', voice: '语音', sfx: '音效', other: '其他', subtitle: '字幕与名字' };
  const off = allParts().filter((k) => !parts.includes(k));
  if (!parts.length) return '一项都不换';
  return parts.length <= off.length ? '只换' + parts.map((k) => names[k] || k).join('、')
    : '不换' + off.map((k) => names[k] || k).join('、');
}

function renderPreview() {
  const p = S.preview;
  if (!p) return;
  const A = S.byId.get(p.target), B = S.byId.get(p.source);
  $('#previewLoading').hidden = true;
  $('#preview').hidden = false;
  const chosen = p.categories.filter((c) => !S.off.has(c.key)).reduce((n, c) => n + c.count, 0);
  const offN = p.categories.filter((c) => S.off.has(c.key)).length;
  $('#previewTotal').innerHTML = `共 <b>${chosen}</b> 项会被替换${offN ? `<span style="color:var(--text3)"> · ${offN} 部分不换</span>` : ''}${p.missing ? `<span style="color:var(--text3)"> · ${p.missing} 项无对应位置</span>` : ''}`;

  // 进化点插图对比
  const evo = $('#evoCompare');
  const hasEvo = B && B.hasEvoPoint;
  evo.hidden = !hasEvo;
  if (hasEvo) {
    const setImg = (el, url) => { el.hidden = false; el.onerror = () => { el.hidden = true; }; el.src = url; };
    setImg($('#evoA'), `/img/evo/${p.target}`);
    setImg($('#evoSA'), `/img/evosuper/${p.target}`);
    setImg($('#evoB'), `/img/evo/${p.source}`);
    setImg($('#evoSB'), `/img/evosuper/${p.source}`);
  }

  const cats = $('#cats');
  const allOn = p.categories.every((c) => !S.off.has(c.key));
  cats.innerHTML = `<div class="cats-head"><span>要替换的部分</span><button type="button" class="cats-all" data-all="${allOn ? 0 : 1}">${allOn ? '全不选' : '全选'}</button></div>`
    + p.categories.map((c) => {
      const off = S.off.has(c.key);
      return `<span class="cat${S.openCat === c.key ? ' on' : ''}${off ? ' off' : ''}" data-key="${c.key}">`
        + `<button type="button" class="cat-chk" data-key="${c.key}" role="checkbox" aria-checked="${!off}" title="${off ? '点一下：替换这部分' : '点一下：不替换这部分'}"><svg class="ico"><use href="#i-check"/></svg></button>`
        + `<button type="button" class="cat-lbl" data-key="${c.key}" title="查看包含哪些资源">${esc(c.label)} <b>${c.count}</b>${c.missing ? `<span class="miss">+${c.missing}</span>` : ''}</button></span>`;
    }).join('');
  renderCatDetail();

  const keep = $('#keepNote');
  const notes = [];
  if (p.evolve && p.evolve.deluxe && B) {
    notes.push(`豪华进化演出、进化点插图、进化音效与战斗边框：直接使用「${B.name}」的原生资源。`);
  }
  if (p.keep && p.keep.length && A && B) {
    notes.push(`另有 ${p.keep.length} 项是「${A.name}」独有、「${B.name}」没有对应的资源，会保持原样。`);
  }
  notes.push('模型或图片比原文件大时，会自动适当降低纹理分辨率以放进原文件。');
  keep.hidden = !notes.length;
  keep.innerHTML = notes.map((t, i) => `<div${i === 0 && p.evolve && p.evolve.deluxe ? ' style="color:var(--gold-hi)"' : ''}>${esc(t)}</div>`).join('');

  const exists = planEntry(p.target);
  const parts = selectedParts(p);
  const same = !!(exists && exists.source === p.source && sameParts(exists.parts, parts));
  const none = p.categories.length > 0 && p.categories.every((c) => S.off.has(c.key));
  const btn = $('#btnAdd');
  btn.innerHTML = none ? '<svg class="ico"><use href="#i-close"/></svg>至少选一个要替换的部分'
    : same ? '<svg class="ico"><use href="#i-check"/></svg>已在方案中'
      : exists ? '<svg class="ico"><use href="#i-swap"/></svg>更新方案中的这一组'
        : '<svg class="ico"><use href="#i-plus"/></svg>加入替换方案';
  btn.disabled = same || none;
}

function renderCatDetail() {
  const d = $('#catDetail');
  const p = S.preview;
  const c = p && p.categories.find((x) => x.key === S.openCat);
  if (!c) { d.hidden = true; d.innerHTML = ''; return; }
  d.hidden = false;
  const st = {
    replace: '',
    native: '<span class="st add">直接用来源原生资源</span>',
    skip: '<span class="st">原皮肤无此项，保持原样</span>',
    missing: '<span class="st missing">来源未下载</span>',
    target_missing: '<span class="st missing">原皮肤这项未下载</span>',
  };
  d.innerHTML = c.items.map((it) => c.key === 'subtitle'
    ? `<div class="row"><span class="k">${esc(it.name.replace(/_\d+$/, '').replace(/^ET_/, '').replace(/^LSPT_N/, '名字').replace(/^LSPT_C/, '声优').replace(/^LSPT_I/, '介绍'))}</span><span class="v" title="${esc(it.text)}">${esc(it.text)}</span></div>`
    : `<div class="row"><span class="v" title="${esc(it.name)}">${esc(base(it.name))}</span>${st[it.status] || ''}</div>`).join('');
}

/* ------------------------------------------------------------------ 方案 */
async function savePlan(plan, okMsg) {
  try {
    const st = await api('/api/plan', { plan });
    onState(st);
    if (okMsg) toast('ok', okMsg[0], okMsg[1]);
  } catch (e) { fail(e); }
}

function addToPlan() {
  const p = S.preview;
  if (!p || !S.state) return;
  const parts = selectedParts(p);
  const item = { target: p.target, source: p.source };
  if (parts) item.parts = [...parts].sort();
  const plan = S.state.plan.filter((x) => x.target !== p.target).concat([item]);
  const A = S.byId.get(p.target), B = S.byId.get(p.source);
  const note = parts ? `（${partsSummary(parts)}）` : '';
  savePlan(plan, ['已加入替换方案', `「${A ? A.name : p.target}」将显示为「${B ? B.name : p.source}」${note}。点「一键替换」生效。`]).then(renderPreview);
}

function renderPlan() {
  const st = S.state;
  if (!st) return;
  const list = $('#planList');
  const sig = (x) => `${x.target}>${x.source}>${JSON.stringify(x.parts || null)}`;
  const applied = new Set(st.applied.map(sig));
  $('#planEmpty').hidden = st.plan.length > 0;
  $('#dirtyTag').hidden = !st.dirty;
  list.innerHTML = st.plan.map((x) => {
    const A = S.byId.get(x.target), B = S.byId.get(x.source);
    const ok = applied.has(sig(x));
    const ps = partsSummary(x.parts);
    return `<div class="plan-row" data-t="${x.target}" data-s="${x.source}" tabindex="0" title="点击在上方查看这一组">
      <img class="av a" src="/img/icon/${x.target}" alt="" onerror="this.style.visibility='hidden'">
      <span class="nm">${esc(A ? A.name : x.target)}<small>#${x.target}</small></span>
      <svg class="ico to"><use href="#i-arrow"/></svg>
      <img class="av b" src="/img/icon/${x.source}" alt="" onerror="this.style.visibility='hidden'">
      <span class="nm">${esc(B ? B.name : x.source)}<small>#${x.source}${ps ? ` · <span class="parts" title="${esc(ps)}">${esc(ps)}</span>` : ''}</small></span>
      ${ok ? '<span class="pill pill-ok"><svg class="ico"><use href="#i-check"/></svg>已生效</span>' : '<span class="pill pill-wait">待应用</span>'}
      <button class="rm" type="button" title="从方案中移除" aria-label="移除"><svg class="ico"><use href="#i-close"/></svg></button>
    </div>`;
  }).join('');
  // 还有已生效但已从方案移除的
  const removed = st.applied.filter((x) => !st.plan.some((p) => p.target === x.target && p.source === x.source));
  if (removed.length && st.plan.length === 0) {
    list.innerHTML = `<div class="plan-empty" style="color:var(--warn)">方案已清空，但游戏里仍有 ${removed.length} 组替换生效中。点下方按钮即可撤销。</div>`;
    $('#planEmpty').hidden = true;
  }
}

/* ------------------------------------------------------------------ 操作区 */
function renderActions(st) {
  const busy = !!st.task;
  const blocked = !st.game.ok || !!st.game.running || busy || !!(st.meta && st.meta.broken);
  const btn = $('#btnApply');
  const txt = $('#applyText');
  const planN = st.plan.length, appliedN = st.applied.length;
  let label = '一键替换', disabled = blocked;
  if (!planN && !appliedN) { disabled = true; btn.title = '先在上方选好两侧皮肤并加入方案'; }
  else if (!planN && appliedN) { label = '应用（撤销全部替换）'; btn.title = ''; }
  else if (st.dirty) { label = '一键替换'; btn.title = ''; }
  else if (st.health && st.health.need) { label = '重新替换'; btn.title = st.health.reason; }
  else { label = '已生效 · 重新替换'; btn.title = '替换已生效；游戏更新后也可以点这里重新替换'; }
  txt.textContent = label;
  btn.disabled = disabled;
  $('#btnRevert').disabled = blocked || (!appliedN && !planN);
  $('#btnCheck').disabled = !st.game.ok || busy;
}

function renderTask(task) {
  const el = $('#task');
  if (!task) { el.hidden = true; return; }
  el.hidden = false;
  const pct = task.total ? Math.min(100, Math.round((task.done / task.total) * 100)) : 0;
  $('#ringFg').style.strokeDashoffset = String(RING * (1 - pct / 100));
  $('#ringFg').style.stroke = task.kind === 'revert' ? 'var(--violet)' : 'var(--gold)';
  $('#taskPct').textContent = `${pct}%`;
  $('#taskLabel').textContent = `正在${task.label}…`;
  $('#taskText').textContent = task.text ? base(task.text) : '';
}

async function doApply() {
  const st = S.state;
  if (!st) return;
  if (!st.plan.length && st.applied.length) { openConfirm(); return; }
  try { onState(await api('/api/apply', {})); } catch (e) { fail(e); }
}

/* ------------------------------------------------------------------ 工具新版本 */
function renderVersion(st) {
  const u = st.appUpdate || {};
  const cur = `v${st.app.version}`;
  const chip = $('#verChip');
  let text = cur, cls = '', title = '点一下：检查 GitHub 上有没有新版本';
  if (u.checking) { text = `${cur} · 检查中…`; cls = 'checking'; }
  else if (u.error) { text = `${cur} · 检查失败`; cls = 'err'; title = `检查新版本失败：${u.error}\n点一下重试`; }
  else if (u.newer) {
    text = `新版本 v${u.latest}`; cls = 'new';
    title = `当前 ${cur}，GitHub 上已有 v${u.latest}。点一下打开下载页${u.notes ? '\n\n' + u.notes.slice(0, 300) : ''}`;
  } else if (u.latest) { text = `${cur} · 已是最新`; cls = 'ok'; title = `GitHub 上是 v${u.latest}。点一下重新检查`; }
  chip.className = 'ver-chip' + (cls ? ' ' + cls : '');
  $('#verText').textContent = text;
  chip.title = title;
  // 结果提示：发现新版本只提示一次；手动检查完成时提示结果
  if (!u.checking && u.time && u.time !== S.verSeen) {
    const manual = S.verManual;
    S.verSeen = u.time;
    S.verManual = false;
    if (u.newer) toast('warn', `发现新版本 v${u.latest}`, '点左上角的版本号打开下载页。', 8000);
    else if (manual && u.error) toast('err', '检查新版本失败', u.error, 6500);
    else if (manual) toast('ok', '已是最新版本', `当前 ${cur}，GitHub 上是 v${u.latest}。`);
  }
}

async function onVersionClick() {
  const u = (S.state && S.state.appUpdate) || {};
  if (u.checking) return;
  try {
    if (u.newer) { await api('/api/open', { what: 'release' }); return; }
    S.verManual = true;
    onState(await api('/api/app_update', {}));
  } catch (e) { fail(e); }
}

async function doCheck() {
  const btn = $('#btnCheck');
  btn.disabled = true;
  try {
    const r = await api('/api/check', {});
    onState(r.state);
    if (r.need) toast('warn', '需要更新', `${r.reason}。点「一键替换」即可更新。`, 6500);
    else toast('ok', '检查完成', r.reason === '没有变化' ? '替换仍然有效，没有新增资源。' : r.reason);
  } catch (e) { fail(e); } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------------ 确认还原 */
function openConfirm() {
  const st = S.state;
  const n = st ? st.applied.length : 0;
  $('#confirmText').textContent = n
    ? `游戏里 ${n} 组替换都会撤销，恢复成原本的皮肤，替换方案也会清空。`
    : '替换方案会被清空。';
  $('#confirmOverlay').hidden = false;
  $('#confirmNo').focus();
}
function closeConfirm() { $('#confirmOverlay').hidden = true; }
async function doRevert() {
  closeConfirm();
  try { onState(await api('/api/revert', { clearPlan: true })); } catch (e) { fail(e); }
}

/* ------------------------------------------------------------------ 目录 */
function openDir(firstRun = false) {
  S.dirOpen = true;
  const st = S.state;
  $('#dirOverlay').hidden = false;
  $('#dirCancel').hidden = !(st && st.game.ok);
  $('#dirError').hidden = !(st && st.game.error && !st.game.ok);
  if (st && st.game.error && !st.game.ok) $('#dirError').textContent = st.game.error;
  $('#dirInput').value = (st && st.game.dir) || '';
  $('#dirTitle').textContent = firstRun ? '先告诉我影之诗装在哪里' : '更换影之诗的安装目录';
  renderDetected();
  if (!S.detected) {
    api('/api/detect').then((d) => { S.detected = d.dirs || []; renderDetected(); }).catch(() => { S.detected = []; renderDetected(); });
  }
}
function closeDir() {
  if (!(S.state && S.state.game.ok)) return;
  S.dirOpen = false;
  $('#dirOverlay').hidden = true;
}
function renderDetected() {
  const box = $('#detected');
  if (!S.detected) { box.innerHTML = '<div class="detected-loading"><div class="spinner"></div>正在自动查找…</div>'; return; }
  if (!S.detected.length) { box.innerHTML = '<div class="detected-none">没有自动找到，请点「浏览…」或在下面粘贴路径。</div>'; return; }
  const cur = (S.state && S.state.game.ok && S.state.game.dir || '').toLowerCase();
  box.innerHTML = S.detected.map((d) => {
    const isCur = d.dir.toLowerCase() === cur;
    return `<button type="button" class="detected-item${isCur ? ' current' : ''}" data-dir="${esc(d.dir)}">
      <svg class="ico"><use href="#i-folder"/></svg>
      <span class="p">${esc(d.dir)}<span class="s" style="display:block">自动检测到</span></span>
      <span class="go">${isCur ? '当前使用' : '使用这个'}</span></button>`;
  }).join('');
}
async function useDir(path) {
  path = (path || '').trim().replace(/^"|"$/g, '');
  if (!path) { showDirError('请输入游戏目录'); return; }
  $('#dirError').hidden = true;
  try {
    const st = await api('/api/dir', { path });
    S.skinsVer = -1;
    S.a = S.b = null;
    renderSlots();
    loadPreview();
    onState(st);
    S.dirOpen = false;
    $('#dirOverlay').hidden = true;
    toast('ok', '已载入游戏目录', path);
  } catch (e) { showDirError(e.message); }
}
function showDirError(msg) { const el = $('#dirError'); el.hidden = false; el.textContent = msg; }
async function browseDir() {
  const btn = $('#dirBrowse');
  btn.disabled = true;
  try {
    const r = await api('/api/browse', {});
    if (r.path) {
      S.skinsVer = -1;
      S.a = S.b = null;
      renderSlots();
      loadPreview();
      onState(r.state);
      S.dirOpen = false;
      $('#dirOverlay').hidden = true;
      toast('ok', '已载入游戏目录', r.path);
    }
  } catch (e) { showDirError(e.message); } finally { btn.disabled = false; }
}

/* ------------------------------------------------------------------ 日志 */
async function loadLogs() {
  try {
    const d = await api(`/api/log?after=${S.logSeq}`);
    S.logSeq = d.seq;
    const open = !$('#logDrawer').hidden;
    for (const l of d.lines) {
      S.logs.push(l);
      if (!open && (l.level === 'warn' || l.level === 'error')) S.unread++;
    }
    if (S.logs.length > 800) S.logs.splice(0, S.logs.length - 800);
    renderLogBadge();
    if (open) renderLogs();
  } catch (e) { /* 下次再拉 */ }
}
function renderLogBadge() {
  const b = $('#logBadge');
  b.hidden = !S.unread;
  b.textContent = S.unread > 99 ? '99+' : S.unread;
}
function renderLogs() {
  const showDebug = $('#showDebug').checked;
  const box = $('#logList');
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.innerHTML = S.logs.filter((l) => showDebug || l.level !== 'debug')
    .map((l) => `<div class="logline ${l.level}"><span class="t">${l.t}</span><span class="x">${esc(l.text)}</span></div>`).join('') ||
    '<div class="logline"><span class="x" style="color:var(--text3)">暂无日志</span></div>';
  if (stick) box.scrollTop = box.scrollHeight;
}
function toggleLog(force) {
  const d = $('#logDrawer');
  const open = force !== undefined ? force : d.hidden;
  d.hidden = !open;
  if (open) { S.unread = 0; renderLogBadge(); renderLogs(); const box = $('#logList'); box.scrollTop = box.scrollHeight; }
}

/* ------------------------------------------------------------------ 事件 */
function bind() {
  $('#classBar').addEventListener('click', (e) => {
    const b = e.target.closest('.cchip');
    if (!b) return;
    const v = b.dataset.cls;
    setClass(v === 'all' ? 'all' : Number(v));
  });
  document.querySelectorAll('.qchip').forEach((b) => {
    b.classList.toggle('on', S.quick.has(b.dataset.q));
    b.addEventListener('click', () => {
      const q = b.dataset.q;
      S.quick.has(q) ? S.quick.delete(q) : S.quick.add(q);
      b.classList.toggle('on', S.quick.has(q));
      savePrefs();
      renderClassBar();
      renderGrid();
    });
  });
  let t;
  $('#search').addEventListener('input', (e) => {
    clearTimeout(t);
    t = setTimeout(() => { S.q = e.target.value; renderClassBar(); renderGrid(); }, 120);
  });
  $('#clearFilters').addEventListener('click', () => {
    S.q = ''; $('#search').value = ''; S.quick.clear();
    document.querySelectorAll('.qchip').forEach((b) => b.classList.remove('on'));
    setClass('all');
  });
  $('#grid').addEventListener('click', (e) => {
    const c = e.target.closest('.skin');
    if (c) pick(c.dataset.id);
  });
  $('#slotA').addEventListener('click', () => setActive('a'));
  $('#slotB').addEventListener('click', () => setActive('b'));
  $('#cats').addEventListener('click', (e) => {
    const all = e.target.closest('.cats-all');
    if (all && S.preview) {
      S.off = all.dataset.all === '1' ? new Set() : new Set(S.preview.categories.map((c) => c.key));
      renderPreview();
      return;
    }
    const chk = e.target.closest('.cat-chk');
    if (chk) {
      const k = chk.dataset.key;
      if (S.off.has(k)) S.off.delete(k); else S.off.add(k);
      renderPreview();
      return;
    }
    const b = e.target.closest('.cat-lbl');
    if (!b) return;
    S.openCat = S.openCat === b.dataset.key ? null : b.dataset.key;
    document.querySelectorAll('.cat').forEach((x) => x.classList.toggle('on', x.dataset.key === S.openCat));
    renderCatDetail();
  });
  $('#btnAdd').addEventListener('click', addToPlan);
  $('#planList').addEventListener('click', (e) => {
    const row = e.target.closest('.plan-row');
    if (!row) return;
    if (e.target.closest('.rm')) {
      const plan = S.state.plan.filter((x) => x.target !== row.dataset.t);
      const A = S.byId.get(row.dataset.t);
      savePlan(plan, ['已从方案中移除', `${A ? A.name : row.dataset.t}（点「一键替换」后生效）`]);
      return;
    }
    S.a = row.dataset.t; S.b = row.dataset.s;
    setActive('b');
    renderSlots();
    loadPreview();
  });
  $('#planList').addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('plan-row')) { e.preventDefault(); e.target.click(); }
  });
  $('#btnApply').addEventListener('click', doApply);
  $('#btnRestoreMeta').addEventListener('click', async () => {
    try { onState(await api('/api/restore_meta', {})); } catch (e) { fail(e); }
  });
  $('#bannerApply').addEventListener('click', doApply);
  $('#btnRevert').addEventListener('click', openConfirm);
  $('#confirmNo').addEventListener('click', closeConfirm);
  $('#confirmYes').addEventListener('click', doRevert);
  $('#btnCheck').addEventListener('click', doCheck);
  $('#verChip').addEventListener('click', onVersionClick);
  $('#autoRescan').addEventListener('change', async (e) => {
    const on = e.target.checked;
    try {
      onState(await api('/api/options', { autoRescan: on }));
      toast('ok', on ? '已开启：启动时自动重新扫描' : '已关闭自动重新扫描', on ? '以后每次打开工具，发现游戏更新覆盖了替换或有新资源时会自动重新替换。' : '');
    } catch (err) { e.target.checked = !on; fail(err); }
  });
  $('#btnLog').addEventListener('click', () => toggleLog());
  $('#logClose').addEventListener('click', () => toggleLog(false));
  $('#showDebug').addEventListener('change', renderLogs);
  $('#dirChip').addEventListener('click', () => openDir(false));
  $('#dirCancel').addEventListener('click', closeDir);
  $('#dirUse').addEventListener('click', () => useDir($('#dirInput').value));
  $('#dirInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') useDir(e.target.value); });
  $('#dirBrowse').addEventListener('click', browseDir);
  $('#detected').addEventListener('click', (e) => {
    const b = e.target.closest('.detected-item');
    if (b) useDir(b.dataset.dir);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!$('#confirmOverlay').hidden) closeConfirm();
      else if (!$('#dirOverlay').hidden) closeDir();
      else if (!$('#logDrawer').hidden) toggleLog(false);
      return;
    }
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
    if ((e.ctrlKey && e.key.toLowerCase() === 'f') || (!typing && e.key === '/')) {
      e.preventDefault();
      $('#search').focus();
      $('#search').select();
      return;
    }
    if (!typing && !e.ctrlKey && !e.altKey && /^[0-8]$/.test(e.key) && $('#dirOverlay').hidden && $('#confirmOverlay').hidden) {
      const chips = [...document.querySelectorAll('.cchip')];
      const c = chips[Number(e.key)];
      if (c) c.click();
    }
  });
  window.addEventListener('pagehide', () => { try { navigator.sendBeacon('/api/bye', ''); } catch (e) { /* 忽略 */ } });
}

/* ------------------------------------------------------------------ 启动 */
loadPrefs();
bind();
setActive('a');
renderSkeleton();
poll();
