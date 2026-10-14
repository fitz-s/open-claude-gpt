// Gives a test's hand-written mock DOM what cdp_consult's click-authority guard (__cgcMayClick /
// __cgcMayType) reads: matches/closest backed by fixture_dom.js's real selector matcher, getAttribute
// and tagName where a mock lacks them. Spliced before a mock's own code; AUTH(node) upgrades a node.
var __M = require(process.env.CGC_FIXTURE_DOM || require('path').join(process.cwd(), 'skill/scripts/fixture_dom.js'));
function __ALL(root, sel) {
  var out = [];
  (function w(x) {
    (x.children || []).forEach(function (k) {
      if (!k || k.nodeType === 3) return;
      AUTH(k); if (__M.matches(k, sel)) out.push(k); w(k);
    });
  })(root);
  return out;
}
// A mock's document whose querySelectorAll refuses a selector: the guard's own queries fall back to
// the real matcher over ROOT.
function AUTHDOC(doc, root) {
  var own = doc.querySelectorAll;
  doc.querySelectorAll = function (sel) {
    try { return own.call(this, sel); } catch (e) {
      if (!/unsupported selector/.test(e.message)) throw e;
      return [root].filter(function (r) { AUTH(r); return __M.matches(r, sel); }).concat(__ALL(root, sel));
    }
  };
  return doc;
}
function AUTH(n) {
  if (!n || n.nodeType === 3 || n.__auth) return n;
  n.__auth = true;
  n.nodeType = 1;
  if (!n.tagName) n.tagName = String(n.tag || n.t || 'div').toUpperCase();
  if (!n._a) n._a = n.a || {};
  if (!n.getAttribute) n.getAttribute = function (k) { return k in this._a ? this._a[k] : null; };
  if (!n.parentElement && n.parent) n.parentElement = n.parent;
  var up = function (x) { return x.parentElement || x.parent || null; };
  n.matches = function (sel) { return __M.matches(this, sel); };
  n.closest = function (sel) { for (var x = this; x && x.nodeType !== 3; x = up(x)) { AUTH(x); if (__M.matches(x, sel)) return x; } return null; };
  // The guard's own queries (comma lists a mock's narrow matcher refuses) fall back to the real
  // matcher over the node's element children; every other query is the mock's own.
  var own = n.querySelector, ownAll = n.querySelectorAll;
  n.querySelector = function (sel) {
    if (own) { try { return own.call(this, sel); } catch (e) { if (!/unsupported selector/.test(e.message)) throw e; } }
    return __ALL(this, sel)[0] || null;
  };
  n.querySelectorAll = function (sel) {
    if (ownAll) { try { return ownAll.call(this, sel); } catch (e) { if (!/unsupported selector/.test(e.message)) throw e; } }
    return __ALL(this, sel);
  };
  return n;
}
