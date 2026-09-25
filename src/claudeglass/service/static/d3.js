/* claudeglass service UI: d3.js
 *
 * The one door to the vendored d3 (vendor/d3-7.9.0.min.js, pinned by
 * THIRD_PARTY.sha256). Imported for its side effect, the UMD bundle
 * registers itself on globalThis; this module hands that object on, so
 * chart code writes `import d3 from "./d3.js"`.
 *
 * d3-dsv builds its parsers with `new Function`, which the service's
 * CSP (`script-src 'self'`) refuses, so no first-party code calls its
 * CSV/TSV parsers (tests/test_static_vendor.py bans the names).
 */
import "./vendor/d3-7.9.0.min.js";

export default globalThis.d3;
