/* The "something is happening" indicator.
 *
 * The dashboard and the statistics page both re-fetch a window of data on
 * demand, and on a wide window that takes a moment. Without feedback the page
 * looks broken -- you click a range, nothing moves, and the numbers change a
 * few seconds later. So: a thin bar at the top of the page, the panes dimmed,
 * and the range chips refusing clicks while a fetch is in flight.
 *
 * Two rules keep it honest rather than twitchy:
 *
 *   * it waits `DELAY_MS` before appearing, so a fast fetch never flashes;
 *   * a background poll (the dashboard's live refresh) only shows it when it is
 *     genuinely slow, while something you just clicked shows it as soon as the
 *     delay passes.
 */
(() => {
  "use strict";

  const DELAY_MS = 120;
  const SLOW_POLL_MS = 1200;

  let timer = null;
  let shown = false;

  const bar = () => document.getElementById("load-bar");
  const note = () => document.getElementById("load-note");

  function paint(on) {
    shown = on;
    document.body.classList.toggle("is-loading", on);
    const element = bar();
    if (element) element.hidden = !on;
    const label = note();
    if (label) label.hidden = !on;
    const group = document.getElementById("range-group") || document.getElementById("stats-range");
    if (group) group.classList.toggle("is-busy", on);
  }

  /** Start waiting. `explicit` marks something the user just asked for. */
  function begin({ explicit = false } = {}) {
    clearTimeout(timer);
    timer = setTimeout(() => paint(true), explicit ? DELAY_MS : SLOW_POLL_MS);
  }

  function end() {
    clearTimeout(timer);
    timer = null;
    if (shown) paint(false);
  }

  window.dropTraceLoading = { begin, end, visible: () => shown };
})();
