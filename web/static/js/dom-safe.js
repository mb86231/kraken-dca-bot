// Shared DOM-safety helpers for the web dashboard.
//
// Rule: any API- or user-influenced string rendered into the page must go
// through one of these helpers (or be assigned via textContent / element
// properties). Never interpolate dynamic values into innerHTML markup —
// escapeHtml() exists only for the few places that must assemble HTML
// strings (e.g. attribute values inside a static template).
//
// escapeHtml escapes &, <, >, " and ' so the result is safe both in text
// nodes and inside double-quoted HTML attributes.
window.escapeHtml = function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
};

// Build an element with a className and text content. Text is set via
// textContent, so it can never be parsed as markup.
window.el = function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
