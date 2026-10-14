// A minimal offline DOM for replaying the driver's page JS against a saved, sanitized fixture
// (cgc_drift.FixtureTab). stdin: {"tree": [...], "path": "/c/x", "js": "<expression>"}; stdout: the
// expression's value as JSON. Selectors: lists, descendant combinators, tag, *, #id, .class,
// [a], [a=v], [a*=v], [a^=v], [a$=v], [a~=v] (with an i flag) and :not(<compound>). No network, no
// timers. Layout, as far as the driver reads it:
//   visibility  an element is 0x0 when it, or an ancestor, carries data-cgc-invisible (the capture's
//               record of a 0x0 box) or `hidden`; every other element is 1x1.
//   innerText   a line break around each block-level child: a block tag, an element the capture
//               recorded as block-level (data-cgc-block), or a child of a flex/grid container
//               (CSS blockifies flex and grid items — a row's label and description spans).
'use strict';
const VOID = new Set(['br', 'hr', 'input', 'img', 'meta', 'link', 'area', 'base', 'col', 'embed',
  'source', 'track', 'wbr']);
const BLOCK = /^(P|DIV|LI|UL|OL|H[1-6]|PRE|BLOCKQUOTE|TABLE|TR|SECTION|ARTICLE|MAIN|FORM|HEADER|FOOTER|NAV)$/;
const FLEXBOX = /(^|\s)(inline-)?(flex|grid)(\s|$)/;
function isBlock(c) {
  if (BLOCK.test(c.tagName) || 'data-cgc-block' in c._a) return true;
  const p = c.parentElement;
  return !!p && p.nodeType === 1 && FLEXBOX.test(p._a['class'] || '');
}

class Text {
  constructor(v) { this.nodeType = 3; this.nodeValue = v; this.parentElement = null; }
  get textContent() { return this.nodeValue; }
}

class El {
  constructor(tag, attrs) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this._a = Object.assign({}, attrs);
    this.childNodes = []; this.parentElement = null; this.scrollTop = 0; this.scrollHeight = 0;
  }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get firstElementChild() { return this.children[0] || null; }
  get previousElementSibling() {
    if (!this.parentElement) return null;
    const s = this.parentElement.children, i = s.indexOf(this);
    return i > 0 ? s[i - 1] : null;
  }
  get attributes() { return Object.keys(this._a).map(k => ({ name: k, value: this._a[k] })); }
  get id() { return this._a.id || ''; }
  get disabled() { return 'disabled' in this._a; }
  getAttribute(n) { return n in this._a ? this._a[n] : null; }
  hasAttribute(n) { return n in this._a; }
  setAttribute(n, v) { this._a[n] = String(v); }
  removeAttribute(n) { delete this._a[n]; }
  get textContent() { return this.childNodes.map(c => c.textContent).join(''); }
  get innerText() {
    let o = '';
    for (const c of this.childNodes) {
      if (c.nodeType === 3) { o += c.nodeValue; continue; }
      if (c.tagName === 'BR') { o += '\n'; continue; }
      if ('data-cgc-invisible' in c._a || 'hidden' in c._a) continue;   // an unrendered box has no text
      const b = isBlock(c), t = c.innerText;
      if (b && o && !o.endsWith('\n')) o += '\n';
      o += t;
      if (b && t && !t.endsWith('\n')) o += '\n';
    }
    return o.replace(/^\n+|\n+$/g, '');   // a block's own leading/trailing breaks are not its text
  }
  getBoundingClientRect() {
    for (let n = this; n && n.nodeType === 1; n = n.parentElement)
      if ('data-cgc-invisible' in n._a || 'hidden' in n._a)
        return { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
    return { width: 1, height: 1, top: 0, left: 0, right: 1, bottom: 1 };
  }
  contains(n) { for (; n; n = n.parentElement) if (n === this) return true; return false; }
  closest(sel) { const L = parse(sel); for (let n = this; n && n.nodeType === 1; n = n.parentElement) if (matchList(n, L)) return n; return null; }
  matches(sel) { return matchList(this, parse(sel)); }
  querySelectorAll(sel) {
    const L = parse(sel), out = [];
    (function w(n) { for (const c of n.children) { if (matchList(c, L)) out.push(c); w(c); } })(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  focus() { document.activeElement = this; }
  click() {}
  dispatchEvent() { return true; }
  scrollIntoView() {}
}

function splitTop(s, ch) {
  const out = []; let d = 0, q = null, cur = '';
  for (const c of s) {
    if (q) { cur += c; if (c === q) q = null; continue; }
    if (c === '"' || c === "'") { q = c; cur += c; continue; }
    if (c === '[' || c === '(') d++;
    if (c === ']' || c === ')') d--;
    if (c === ch && d === 0) { out.push(cur); cur = ''; continue; }
    cur += c;
  }
  out.push(cur);
  return out;
}

const ATTR = /^\[\s*([\w-]+)\s*(?:([*$^~]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]+))\s*([iI])?\s*)?\]/;

function compound(s) {
  const c = { tag: null, parts: [] };
  let i = 0;
  const tm = s.match(/^([a-zA-Z][\w-]*|\*)/);
  if (tm) { if (tm[1] !== '*') c.tag = tm[1].toUpperCase(); i = tm[0].length; }
  while (i < s.length) {
    const r = s.slice(i);
    let m;
    if ((m = r.match(/^#([\w-]+)/))) { const v = m[1]; c.parts.push(e => e.getAttribute('id') === v); }
    else if ((m = r.match(/^\.([\w-]+)/))) {
      const v = m[1]; c.parts.push(e => (e.getAttribute('class') || '').split(/\s+/).indexOf(v) >= 0);
    } else if ((m = r.match(ATTR))) {
      const name = m[1], op = m[2], ci = !!m[6];
      let want = m[3] !== undefined ? m[3] : m[4] !== undefined ? m[4] : m[5];
      if (ci && want) want = want.toLowerCase();
      c.parts.push(e => {
        let v = e.getAttribute(name);
        if (v === null) return false;
        if (!op) return true;
        if (ci) v = v.toLowerCase();
        if (op === '=') return v === want;
        if (op === '*=') return !!want && v.indexOf(want) >= 0;
        if (op === '^=') return !!want && v.indexOf(want) === 0;
        if (op === '$=') return !!want && v.slice(-want.length) === want;
        return v.split(/\s+/).indexOf(want) >= 0;
      });
    } else if (r.startsWith(':not(')) {
      let d = 0, j = 4;
      for (; j < r.length; j++) { if (r[j] === '(') d++; if (r[j] === ')' && --d === 0) break; }
      const inner = compound(r.slice(5, j));
      c.parts.push(e => !matchC(e, inner));
      m = [r.slice(0, j + 1)];
    } else throw new Error('fixture_dom: unsupported selector ' + JSON.stringify(s));
    i += m[0].length;
  }
  return c;
}

function parse(sel) {
  return splitTop(sel, ',').map(s => splitTop(s.trim().replace(/\s+/g, ' '), ' ').filter(Boolean).map(compound));
}

function matchC(e, c) {
  if (c.tag && e.tagName !== c.tag) return false;
  return c.parts.every(p => p(e));
}

function matchComplex(e, cs) {
  if (!matchC(e, cs[cs.length - 1])) return false;
  let n = e.parentElement;
  for (let k = cs.length - 2; k >= 0; k--) {
    while (n && n.nodeType === 1 && !matchC(n, cs[k])) n = n.parentElement;
    if (!n || n.nodeType !== 1) return false;
    n = n.parentElement;
  }
  return true;
}

function matchList(e, L) { return L.some(cs => matchComplex(e, cs)); }

function build(node, parent) {
  let n;
  if (typeof node === 'string') n = new Text(node);
  else { n = new El(node[0], node[1]); for (const k of node[2]) build(k, n); }
  n.parentElement = parent;
  parent.childNodes.push(n);
  return n;
}

let raw = '';
process.stdin.on('data', d => { raw += d; });
process.stdin.on('end', () => {
  const inp = JSON.parse(raw);
  const doc = new El('#document', {});
  for (const k of inp.tree) build(k, doc);
  doc.documentElement = doc.children[0] || null;
  doc.body = doc.querySelector('body') || doc.documentElement;
  doc.scrollingElement = doc.documentElement;
  doc.activeElement = doc.body;
  doc.getElementById = id => doc.querySelector('#' + id);
  doc.execCommand = () => false;
  globalThis.document = doc;
  globalThis.window = globalThis;
  globalThis.location = { pathname: inp.path || '/', hostname: 'chatgpt.com' };
  globalThis.MouseEvent = globalThis.KeyboardEvent = class { constructor(t, o) { this.type = t; Object.assign(this, o); } };
  const r = (0, eval)(inp.js);
  process.stdout.write(JSON.stringify(r === undefined ? null : r));
});
