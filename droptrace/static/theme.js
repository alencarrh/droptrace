/* Style switcher: one style family, two modes.
 *
 * `data-style` marks the family (modern) and `data-theme` the mode (light or
 * dark); the stylesheet keys every colour off those two attributes. Loaded
 * synchronously from <head> so the choice is applied *before* the first paint:
 * swapping after paint is a flash of the wrong palette, which is worse than not
 * offering the choice at all.
 *
 * Nothing here knows what a panel is; the CSS does all the work. The one
 * exception is Chart.js -- a canvas keeps the colours it was drawn with, so a
 * charted page has to be built again.
 */
(() => {
  "use strict";

  const KEY = "droptrace.theme";
  const STYLE = "modern";
  const THEMES = [
    { key: "light", label: "light", title: "White surfaces, soft shadows, tinted tags" },
    { key: "dark", label: "dark", title: "The same, on a deep navy background" },
  ];
  const DEFAULT = "light";
  // Names from earlier versions of the switch, mapped onto the two modes.
  const ALIASES = { modern: "light", report: "light", system: "light" };
  const known = (key) => THEMES.some((theme) => theme.key === key);
  const resolve_name = (value) => {
    const key = (value || "").toLowerCase();
    return ALIASES[key] || key;
  };

  /** The chosen style: ?theme= wins and is remembered, so links carry it. */
  function resolve() {
    let requested = "";
    try {
      requested = (new URLSearchParams(location.search).get("theme") || "").toLowerCase();
    } catch { /* no URLSearchParams: keep the default */ }
    const wanted = resolve_name(requested);
    if (known(wanted)) {
      store(wanted);
      return wanted;
    }
    let stored = "";
    try {
      stored = localStorage.getItem(KEY) || "";
    } catch { /* private mode: the default is fine */ }
    const remembered = resolve_name(stored);
    return known(remembered) ? remembered : DEFAULT;
  }

  function store(key) {
    try {
      localStorage.setItem(KEY, key);
    } catch { /* private mode: the choice just will not be remembered */ }
  }

  const current = resolve();
  document.documentElement.dataset.style = STYLE;
  document.documentElement.dataset.theme = current;

  function apply(key) {
    if (!known(key) || key === document.documentElement.dataset.theme) return;
    store(key);
    document.documentElement.dataset.theme = key;
    // Only the charts need rebuilding; every other colour lives in the sheet.
    if (window.Chart) location.reload();
    else paint(key);
  }

  /** The chips live in the page's own toolbar, so this file edits no markup. */
  function paint(key) {
    const host = document.getElementById("theme-chips");
    if (!host) return;
    for (const chip of host.querySelectorAll("[data-theme]")) {
      chip.classList.toggle("is-active", chip.dataset.theme === key);
    }
  }

  function build() {
    const host = document.getElementById("theme-chips");
    if (!host) return;
    host.innerHTML = THEMES.map((theme) => `<button class="chip${
      theme.key === current ? " is-active" : ""}" data-theme="${theme.key}"
      type="button" title="${theme.title}">${theme.label}</button>`).join("");
    host.addEventListener("click", (event) => {
      const chip = event.target.closest("[data-theme]");
      if (chip) apply(chip.dataset.theme);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build, { once: true });
  } else {
    build();
  }

  window.dropTraceTheme = {
    current: () => document.documentElement.dataset.theme,
    style: () => document.documentElement.dataset.style,
    themes: THEMES.map((theme) => theme.key),
    apply,
  };
})();
