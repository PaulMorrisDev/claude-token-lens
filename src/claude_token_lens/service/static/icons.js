/* claude-token-lens service UI: icons.js
 *
 * The dashboard's one icon set: drawn in-house on a 16px grid with a
 * 1.5px round-capped stroke in currentColor, so an icon takes the colour
 * of the text beside it. The same icon always means the same thing.
 * Inline SVG only: the static scans ban emoji, arrow and check-mark
 * characters, so every glyph of that kind is drawn here.
 *
 * Built from markup rather than createElementNS: the HTML parser puts an
 * <svg> into the SVG namespace by itself, which keeps the namespace
 * URI's URL-scheme literal out of this file (see page-spend.js's
 * timeline for the same choice). Every string below is a constant.
 */

// A dot drawn as a filled circle, for the i and ! marks.
function dot(cx, cy) {
  return '<circle cx="' + cx + '" cy="' + cy + '" r="0.85" fill="currentColor" stroke="none"/>';
}

var PATHS = {
  // Pages.
  overview:
    '<rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1"/>' +
    '<rect x="2.5" y="9" width="4.5" height="4.5" rx="1"/><rect x="9" y="9" width="4.5" height="4.5" rx="1"/>',
  actions:
    '<path d="M2.5 9.5h3l1 1.75h3l1-1.75h3"/><path d="M4.1 3h7.8l1.6 6.5V12a1.5 1.5 0 0 1-1.5 1.5h-8A1.5 1.5 0 0 1 2.5 12V9.5z"/>',
  spend:
    '<ellipse cx="8" cy="4.5" rx="5" ry="2"/><path d="M3 4.5V8c0 1.1 2.24 2 5 2s5-.9 5-2V4.5"/>' +
    '<path d="M3 8v3.5c0 1.1 2.24 2 5 2s5-.9 5-2V8"/>',
  cache: '<path d="M8 2.5l5.5 3L8 8.5l-5.5-3z"/><path d="M2.5 8.25 8 11.25l5.5-3"/><path d="M2.5 10.75 8 13.75l5.5-3"/>',
  agents:
    '<rect x="3" y="5" width="10" height="8" rx="2"/><path d="M8 5V3"/>' +
    dot(8, 2.25) +
    dot(6, 9) +
    dot(10, 9),
  habits: '<path d="M1.75 8.5h2.5L6 4l3 8 1.75-4.5h3.5"/>',
  setup:
    '<path d="M2.5 4.5h6M11.5 4.5h2M2.5 11.5h2M7.5 11.5h6"/><circle cx="10" cy="4.5" r="1.5"/><circle cx="6" cy="11.5" r="1.5"/>',
  data: '<path d="M8 2l5 2v4c0 3-2.2 5.1-5 6-2.8-.9-5-3-5-6V4z"/><path d="M5.75 8l1.5 1.5 3-3"/>',
  glossary: '<path d="M3.5 12.75V3.5A1.5 1.5 0 0 1 5 2h7.5v9.5H5a1.5 1.5 0 0 0-1.5 1.25A1.25 1.25 0 0 0 4.75 14h7.75"/>',

  // Chrome.
  search: '<circle cx="7" cy="7" r="4.25"/><path d="m10.25 10.25 3.25 3.25"/>',
  sun:
    '<circle cx="8" cy="8" r="2.75"/><path d="M8 1.75v1.5M8 12.75v1.5M1.75 8h1.5M12.75 8h1.5M3.6 3.6l1.05 1.05' +
    'M11.35 11.35l1.05 1.05M3.6 12.4l1.05-1.05M11.35 4.65l1.05-1.05"/>',
  moon: '<path d="M13 9.6A5.5 5.5 0 0 1 6.4 3 5.5 5.5 0 1 0 13 9.6z"/>',
  system: '<rect x="2" y="3" width="12" height="8" rx="1.5"/><path d="M6 14h4M8 11v3"/>',
  sidebar: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M6 2.5v11"/>',
  "chevron-down": '<path d="m4.5 6.25 3.5 3.5 3.5-3.5"/>',
  "chevron-up": '<path d="m4.5 9.75 3.5-3.5 3.5 3.5"/>',
  "chevron-right": '<path d="m6.25 4.5 3.5 3.5-3.5 3.5"/>',
  "chevron-left": '<path d="M9.75 4.5 6.25 8l3.5 3.5"/>',
  "arrow-right": '<path d="M3 8h10M9 4l4 4-4 4"/>',
  "arrow-up": '<path d="M8 13V3M4 7l4-4 4 4"/>',
  "arrow-down": '<path d="M8 3v10M4 9l4 4 4-4"/>',
  close: '<path d="m4 4 8 8M12 4l-8 8"/>',
  check: '<path d="m3.5 8.5 3 3 6-7"/>',
  minus: '<path d="M3.5 8h9"/>',
  plus: '<path d="M8 3.5v9M3.5 8h9"/>',
  copy:
    '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/>' +
    '<path d="M10.5 5.5V4A1.5 1.5 0 0 0 9 2.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5"/>',
  link:
    '<path d="m6.75 9.25 2.5-2.5"/><path d="m7.5 4.75 1-1a2.5 2.5 0 0 1 3.54 3.54l-1 1"/>' +
    '<path d="m8.5 11.25-1 1a2.5 2.5 0 0 1-3.54-3.54l1-1"/>',
  refresh: '<path d="M13 8a5 5 0 1 1-1.46-3.54"/><path d="M13 2.75v2.5h-2.5"/>',
  clock: '<circle cx="8" cy="8" r="5.75"/><path d="M8 4.75V8l2.25 1.5"/>',
  table: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M2 6.5h12M2 10h12M6.5 6.5v7"/>',
  chart: '<path d="M2.5 13.5h11"/><path d="M4.5 11V8M8 11V4M11.5 11V6.5"/>',
  filter: '<path d="M2.5 3.5h11l-4.25 5v4l-2.5 1.5V8.5z"/>',
  terminal: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="m5 6.5 2 1.5-2 1.5M8.5 10H11"/>',
  prompt: '<path d="M3 3h10a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H7.5l-3 2.5V11H3a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/>',
  keyboard:
    '<rect x="1.75" y="4" width="12.5" height="8" rx="1.5"/>' + dot(4.75, 6.75) + dot(7, 6.75) + dot(9.25, 6.75) + dot(11.5, 6.75) +
    '<path d="M5.25 9.5h5.5"/>',
  "trend-up": '<path d="m2.5 11 4-4 2.5 2.5 4.5-4.5"/><path d="M10 5h3.5v3.5"/>',
  "trend-down": '<path d="m2.5 5 4 4 2.5-2.5 4.5 4.5"/><path d="M10 11h3.5V7.5"/>',

  // Status: each pairs with a text label, never colour alone.
  info: '<circle cx="8" cy="8" r="5.75"/><path d="M8 7.25V11"/>' + dot(8, 5),
  success: '<circle cx="8" cy="8" r="5.75"/><path d="m5.75 8.25 1.5 1.5 3-3.25"/>',
  warning:
    '<path d="M7.13 2.5a1 1 0 0 1 1.74 0l5.2 9.5a1 1 0 0 1-.87 1.5H2.8a1 1 0 0 1-.87-1.5z"/><path d="M8 6.25v3"/>' + dot(8, 11.25),
  critical: '<path d="M5.6 2h4.8L14 5.6v4.8L10.4 14H5.6L2 10.4V5.6z"/><path d="M8 5v3.5"/>' + dot(8, 10.75),

  // The Token Lens mark: a lens over a stack of tokens.
  mark:
    '<circle cx="7" cy="7" r="4.5"/><path d="m10.25 10.25 3.5 3.5"/><path d="M5 6h4M5 8h2.5"/>',
};

export var ICON_NAMES = Object.keys(PATHS);

// An inline 16px icon, hidden from assistive tech (the label beside it
// carries the meaning). opts.size scales it; opts.label makes it a
// labelled image for the rare icon that stands alone.
export function icon(name, opts) {
  opts = opts || {};
  var body = PATHS[name];
  if (!body) throw new Error("unknown icon: " + name);
  var size = opts.size || 16;
  var a11y = opts.label ? 'role="img" aria-label="' + opts.label.replace(/[&<>"]/g, "") + '"' : 'aria-hidden="true"';
  var holder = document.createElement("span");
  holder.innerHTML =
    '<svg class="icon icon-' + name + '" viewBox="0 0 16 16" width="' + size + '" height="' + size + '" fill="none"' +
    ' stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" focusable="false" ' +
    a11y + ">" + body + "</svg>";
  return holder.firstChild;
}
