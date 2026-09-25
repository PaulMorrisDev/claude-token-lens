/* claudeglass service UI: theme-boot.js
 *
 * A classic script in <head>, so it runs before the first paint: a module
 * script is deferred and would flash the wrong theme first. It copies the
 * viewer's pick (tls:theme: "light" or "dark"; anything else follows the
 * system) onto <html data-theme>, and app.css does the rest. Storage is
 * wrapped in try/catch: a private window or blocked site data just
 * follows the system.
 */
(function () {
  "use strict";
  var theme = "system";
  try {
    var saved = window.localStorage.getItem("tls:theme");
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (err) {
    theme = "system";
  }
  document.documentElement.setAttribute("data-theme", theme);
})();
