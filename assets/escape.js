// Shared HTML-escaping helper. Used before interpolating any server-supplied
// text (campaign terms, brand names, handles, etc.) into innerHTML/template
// strings — never trust that data is safe to render as-is.
//
// Exposed as both names because pages already call one or the other
// (dashboard/merchant/leaderboard use escapeHtml, browse/admin/index/feed use
// esc) — this file replaces each page's own copy rather than renaming call
// sites, so both stay callable.
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const esc = escapeHtml;

// Allowlist HTML sanitizer for fields that are meant to carry real markup
// (currently: campaign `terms`, e.g. "<br>"-separated bullets plus a
// "<a href=terms.html>Full terms</a>" link) but originate from a merchant's
// campaign submission, not from us. Unlike escapeHtml, this returns HTML,
// not text — only call it where the caller actually needs the markup to
// render (deal.html's terms list), never as a general-purpose escape.
const ALLOWED_TAGS = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'P', 'BR', 'UL', 'OL', 'LI', 'SPAN', 'A']);
// Resolves relative links (e.g. terms' own "terms.html") against the current
// page before checking the scheme, so a same-site relative href survives
// while "javascript:"/"data:" etc. (which resolve to their own protocol
// regardless of base) don't.
function isSafeHref(href) {
  try {
    const url = new URL(href, document.baseURI);
    return ['http:', 'https:', 'mailto:', 'tel:'].includes(url.protocol);
  } catch {
    return false;
  }
}
function sanitizeHtml(html) {
  const container = document.createElement('div');
  container.innerHTML = String(html ?? '');
  (function walk(node) {
    Array.from(node.childNodes).forEach(child => {
      if (child.nodeType === Node.TEXT_NODE) return;
      if (child.nodeType !== Node.ELEMENT_NODE) { child.remove(); return; }
      if (!ALLOWED_TAGS.has(child.tagName)) {
        child.replaceWith(document.createTextNode(child.textContent));
        return;
      }
      Array.from(child.attributes).forEach(attr => {
        if (child.tagName === 'A' && attr.name === 'href' && isSafeHref(attr.value)) return;
        child.removeAttribute(attr.name);
      });
      if (child.tagName === 'A' && child.hasAttribute('href')) {
        child.setAttribute('rel', 'noopener noreferrer');
        child.setAttribute('target', '_blank');
      }
      walk(child);
    });
  })(container);
  return container.innerHTML;
}
