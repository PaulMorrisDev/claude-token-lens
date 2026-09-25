/* claude-token-lens service UI: page-glossary.js
 *
 * The Glossary page: Terms (links.js's GLOSSARY, the same wording as the
 * README's glossary) and How costs work (links.js's COST_CARDS).
 */

import { clear, el } from "./core.js";
import { GLOSSARY, viewIntro } from "./links.js";

// ======================================================================
// Glossary > Terms
// ======================================================================


export function renderGlossary(panel) {
  clear(panel);
  viewIntro(panel, "glossary/terms");
  var list = el("dl", { class: "glossary" });
  GLOSSARY.forEach(function (pair) {
    list.appendChild(el("dt", { text: pair[0] }));
    list.appendChild(el("dd", { text: pair[1] }));
  });
  panel.appendChild(list);
}

// ======================================================================
// Glossary > How costs work
// ======================================================================

export function renderCostCards(panel) {
  clear(panel);
  viewIntro(panel, "glossary/how-costs-work");
}
