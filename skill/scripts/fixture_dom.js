// A minimal offline DOM for replaying the driver's page JS against a saved, sanitized fixture
// (cgc_drift.FixtureTab). stdin: {"tree": [...], "path": "/c/x", "js": "<expression>"}; stdout: the
// expression's value as JSON. Selectors: lists, descendant combinators, tag, *, #id, .class,
// [a], [a=v], [a*=v], [a^=v], [a$=v], [a~=v] (with an i flag) and :not(<compound>). No layout:
// every element is visible. No network, no timers.
'use strict';
const VOID = new Set(['br', 'hr', 'input', 'img', 'meta', 'link', 'area', 'base', 'col', 'embed',
  'source', 'track', 'wbr']);
const BLOCK = /^(P|DIV|LI|UL|OL|H[1-6]|PRE|BLOCKQUOTE|TABLE|TR|SECTION|ARTICLE|MAIN|FORM|HEADER|FOOTER|NAV)$/;

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
      const b = BLOCK.test(c.tagName), t = c.innerText;
      if (b && o && !o.endsWith('\n')) o += '\n';
      o += t;
      if (b && t && !t.endsWith('\n')) o += '\n';
    }
    return o;
  }
  getBoundingClientRect() { return { width: 1, height: 1, top: 0, left: 0, right: 1, bottom: 1 }; }
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
