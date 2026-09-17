#!/usr/bin/env node
/**
 * Headless smoke test for the dashboard.
 *
 * There is no build step, so a typo in app.js only shows up in a browser. This
 * loads the real app.js against a *running* server with a minimal DOM shim and
 * asserts that every render function survives real API data. It catches the
 * failures a browser console would: null element lookups, bad property access,
 * unexpected response shapes.
 *
 * Usage:  node scripts/check_frontend.mjs [baseUrl]
 * Exits non-zero on the first failure.
 */

const BASE = process.argv[2] || "http://127.0.0.1:8777";
const realSetTimeout = globalThis.setTimeout;
const realFetch = globalThis.fetch;

/* ---------------------------------------------------------------- DOM shim */
const elements = new Map();
const charts = [];
const listeners = new Map();
const failures = [];
const notes = [];

const documentListeners = {};

function makeElement(id) {
  const classes = new Set();
  const el = {
    id,
    textContent: "",
    value: "",
    hidden: false,
    href: "",
    dataset: {},
    style: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    addEventListener: (type, handler) => {
      if (!listeners.has(id)) listeners.set(id, {});
      listeners.get(id)[type] = handler;
    },
    // Geometry: ticks get a synthetic box each so a drag can be mapped to a
    // time, everything else a single wide box.
    getBoundingClientRect: () => ({
      left: 0, right: 1000, top: 0, bottom: 10, width: 1000, height: 10,
    }),
    querySelectorAll: (selector) =>
      selector && selector.includes(".tick") ? el._ticks || [] : [],
    click: async () => {
      const handler = listeners.get(id)?.click;
      if (handler) await handler({ target: { closest: () => null } });
    },
    getContext: () => ({ save() {}, restore() {}, fillRect() {}, strokeRect() {} }),
    closest: () => null,
  };
  el._ticks = [];
  // Setting the strip's markup creates one fake tick per rendered block, so the
  // brush can be exercised exactly as the browser would drive it.
  let markup = "";
  Object.defineProperty(el, "innerHTML", {
    get: () => markup,
    set: (value) => {
      markup = String(value);
      const count = (markup.match(/class="tick/g) || []).length;
      el._ticks = Array.from({ length: count }, (_, index) => ({
        getBoundingClientRect: () => ({
          left: index * 2, right: index * 2 + 2, top: 0, bottom: 10, width: 2, height: 10,
        }),
      }));
    },
  });
  return el;
}

/** Deliver a synthesised event to a listener the app registered. */
function fire(id, type, event) {
  const handler = listeners.get(id)?.[type];
  if (handler) handler(event);
  return !!handler;
}

const element = (id) => {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
};

globalThis.document = {
  readyState: "complete",
  hidden: false,
  body: makeElement("body"),
  activeElement: null,
  getElementById: element,
  querySelectorAll: () => [],
  addEventListener: (type, handler) => {
    documentListeners[type] = handler;
  },
};
globalThis.window = globalThis;
// The app reads its chart colours from custom properties, so the shim has to
// answer like a browser that defines none: the built-in dark theme.
globalThis.getComputedStyle = () => ({
  fontFamily: "sans-serif",
  getPropertyValue: () => "",
});
globalThis.confirm = () => false;
// NOTE: setTimeout/setInterval are deliberately left alone. Stubbing them
// breaks undici's internal timers, which makes Node's own fetch hang. The
// app's intervals keep the loop alive, and the explicit process.exit() at the
// end shuts everything down.
const fetched = [];
// Commands are answered here rather than forwarded: clicking "Speed test" in
// this script used to start a real 10s test and spend ~700MB of the connection
// being measured, every single run. Only reads reach the server.
const MUTATING = [/\/api\/probe/, /\/api\/control\//, /\/api\/reset/];
globalThis.fetch = (url, options) => {
  const resolved = new URL(String(url), BASE).toString();
  fetched.push(resolved);
  const method = (options?.method || "GET").toUpperCase();
  if (method !== "GET" && MUTATING.some((pattern) => pattern.test(resolved))) {
    return Promise.resolve(new Response(JSON.stringify({ ok: true, dry_run: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }
  return realFetch(resolved, options);
};

globalThis.Chart = class Chart {
  static defaults = { font: {}, color: "" };
  constructor(context, config) {
    this.data = config.data;
    this.options = config.options;
    this.plugins = config.plugins;
    this.updates = 0;
    this.$outages = [];
    charts.push(this);
  }
  update() { this.updates += 1; }
};

const eventSources = [];
globalThis.EventSource = class EventSource {
  constructor(url) {
    this.url = url;
    this.handlers = {};
    eventSources.push(this);
  }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  close() {}
};

process.on("unhandledRejection", (error) => {
  failures.push(`unhandled rejection: ${error && error.stack ? error.stack : error}`);
});

/* ------------------------------------------------------------- run app.js */
const fs = await import("node:fs/promises");
const path = await import("node:path");
const here = path.dirname(new URL(import.meta.url).pathname);
const source = await fs.readFile(path.join(here, "..", "droptrace", "static", "app.js"), "utf8");

console.log(`\n  DropTrace frontend smoke test → ${BASE}\n  ${"—".repeat(58)}`);

// The health check first: a clear message beats a confusing fetch error.
try {
  const health = await realFetch(`${BASE}/api/health`);
  if (!health.ok) throw new Error(`HTTP ${health.status}`);
  const body = await health.json();
  console.log(`  server ok · running=${body.running} samples=${body.samples}`);
} catch (error) {
  console.error(`  ✗ cannot reach ${BASE}: ${error.message}`);
  console.error("    start the server first: python -m droptrace serve\n");
  process.exit(2);
}

try {
  // Indirect eval so the IIFE runs in this scope and sees the shims.
  (0, eval)(source);
} catch (error) {
  failures.push(`app.js threw while loading: ${error.stack || error}`);
}

// Let boot()'s awaits (config + summary + series) settle. Polled rather than a
// fixed sleep: the aggregates scan every stored probe, so on a busy machine --
// or with a database that has grown -- 2.5 seconds is simply not enough, and
// the result used to be twenty bogus failures that looked like broken renders.
{
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline && element("v-uptime").textContent === "") {
    await new Promise((resolve) => realSetTimeout(resolve, 100));
  }
  // One more turn for the renders that follow the first await to land.
  await new Promise((resolve) => realSetTimeout(resolve, 500));
}

/* --------------------------------------------------------------- assert */
function check(label, condition, detail = "") {
  if (condition) {
    console.log(`  ✓ ${label}`);
  } else {
    failures.push(`${label}${detail ? ` — ${detail}` : ""}`);
    console.log(`  ✗ ${label}${detail ? ` — ${detail}` : ""}`);
  }
}

const text = (id) => element(id).textContent;
const html = (id) => element(id).innerHTML;

check("charts were constructed", charts.length === 3, `got ${charts.length}`);
check("no unhandled promise rejections", failures.length === 0, failures.join("; "));

// Values that must have been computed from live data.
const isNumber = (value) => value !== "" && value !== "—" && !Number.isNaN(Number(value));
check("uptime % rendered", isNumber(text("v-uptime")), `v-uptime="${text("v-uptime")}"`);
check("outage count rendered", text("v-outages") !== "", `v-outages="${text("v-outages")}"`);
check("ping rendered", isNumber(text("v-ping")), `v-ping="${text("v-ping")}"`);
check("jitter rendered", text("v-jitter") !== "", `v-jitter="${text("v-jitter")}"`);
check("round count rendered", Number(text("f-rounds")) > 0, `f-rounds="${text("f-rounds")}"`);
check("stored samples rendered", Number(text("f-stored")) > 0, `f-stored="${text("f-stored")}"`);
check("elapsed rendered", text("elapsed-text") !== "—", `elapsed="${text("elapsed-text")}"`);

// The connection timeline strip is the core evidence view.
const strip = html("strip");
check("timeline strip has ticks", strip.includes('class="tick'), `strip length ${strip.length}`);
check("timeline range caption rendered", text("strip-range").includes("→"), `"${text("strip-range")}"`);

// Tables.
check("targets table has rows", (html("targets-body").match(/<tr/g) || []).length > 0);
check("incidents table rendered", html("incidents-body").length > 0);

// Charts must actually carry data, not just exist.
const latency = charts.find((c) => c.data.datasets.some((d) => d.label === "no answer"))
  || charts.find((c) => c.data.datasets.length > 1);
const throughput = charts.find((c) =>
  ["burst down", "sustained down"].some((l) => c.data.datasets.some((d) => d.label === l)));
check(
  "latency chart has per-target series",
  !!latency && latency.data.datasets.filter((d) => !d.$isFailure).length > 0,
  `datasets=${latency ? latency.data.datasets.map((d) => d.label).join(",") : "none"}`
);
check(
  "latency chart has data points",
  !!latency && latency.data.datasets.some((d) => (d.data || []).length > 0)
);
check("outage shading fed from incidents", !!latency && Array.isArray(latency.$outages));

// The per-second view of the last test is the whole point of the duration-based
// speed test, so make sure it is wired and fed.
const lastTest = charts.find((c) => c.data.datasets.some((d) => d.label === "upload")
  && c.data.datasets.length === 2)
  || charts[2];
check("last-test chart has both directions",
  !!lastTest && ["download", "upload"].every((l) => lastTest.data.datasets.some((d) => d.label === l)),
  `datasets=${lastTest ? lastTest.data.datasets.map((d) => d.label).join(",") : "none"}`);
// The per-second panel only has data once a sustained test has run since the
// server started, so require buckets only when the status has them.
let sustainedBuckets = 0;
try {
  const state = await (await realFetch(`${BASE}/api/status`)).json();
  sustainedBuckets = (state.last_sustained?.download_intervals || []).length;
} catch (error) { /* reported below */ }
if (sustainedBuckets === 0) {
  console.log("  · last-test chart: no sustained test since startup (nothing to assert)");
} else {
  check("last-test chart is fed per-second buckets",
    !!lastTest && lastTest.data.datasets.some((d) => (d.data || []).length > 0),
    `${sustainedBuckets} buckets available`);
}
check(
  "throughput chart separates manual runs",
  !!throughput && ["manual down", "manual up"].every((l) =>
    throughput.data.datasets.some((d) => d.label === l)),
  `datasets=${throughput ? throughput.data.datasets.map((d) => d.label).join(",") : "none"}`
);
check(
  "speed tests table lists runs",
  html("speedtests-body").length > 0
    && !html("speedtests-body").includes("No speed tests yet")
);
check("the tagline does not quote a stale interval",
  !/300s/.test(text("target-line")) && /round every/.test(text("target-line")),
  `"${text("target-line")}"`);
check("data projection rendered", /GB\/day|MB\/day|depends on your speed|off/.test(element("data-projection").textContent),
  `"${element("data-projection").textContent}"`);

// Throughput data only exists once a speed test has run, which may not have
// happened yet on a freshly reset database — so require points only when the
// API actually has speed samples in range.
let speedPoints = null;
try {
  // Match the window the page itself is showing (default 1h), not all time.
  const series = await (await realFetch(`${BASE}/api/series?window=1h&max_points=600`)).json();
  speedPoints = Object.values(series.speed?.series || {}).reduce((n, rows) => n + rows.length, 0);
} catch (error) { /* reported below */ }
check(
  "throughput chart splits burst from sustained",
  !!throughput && ["burst down", "burst up", "sustained down", "sustained up"]
    .every((l) => throughput.data.datasets.some((d) => d.label === l)),
  `datasets=${throughput ? throughput.data.datasets.map((d) => d.label).join(",") : "none"}`
);
if (speedPoints === null) {
  check("throughput series is reachable", false, "could not read /api/series");
} else if (speedPoints === 0) {
  console.log("  · throughput chart: no speed test in range yet (nothing to assert)");
} else {
  check(
    "throughput chart has data points",
    !!throughput && throughput.data.datasets.some((d) => (d.data || []).length > 0),
    `${speedPoints} speed samples available`
  );
}

// Brushing the timeline: drag across the strip and the dashboard should refetch
// an explicit since/until range, the same way the preset chips do.
{
  const before = fetched.length;
  const pressed = fire("strip", "mousedown", {
    button: 0, clientX: 10, preventDefault() {},
  });
  // These two live on `document`, not on the strip element.
  documentListeners.mousemove?.({ clientX: 40 });
  documentListeners.mouseup?.({ clientX: 40 });
  // The refresh is async; give it a moment.
  await new Promise((resolve) => realSetTimeout(resolve, 400));
  const after = fetched.slice(before).filter((u) => u.includes("/api/"));
  const ranged = after.filter((u) => u.includes("since=") && u.includes("until="));
  check("dragging the timeline zooms to an explicit range",
    pressed && ranged.length > 0,
    `press=${pressed} ranged requests=${ranged.length} of ${after.length}`);
  if (ranged.length) {
    const sample = new URL(ranged[0]);
    check("the zoomed window has both bounds",
      Number(sample.searchParams.get("until")) > Number(sample.searchParams.get("since")),
      `${sample.searchParams.get("since")} -> ${sample.searchParams.get("until")}`);
  }
  check("a clear-selection control appears once zoomed",
    element("btn-clear-range").hidden === false);
}

// The speed-test modal: clicking the button must open it, and a progress frame
// from the stream must fill it in. A sustained test runs 20s, so "did it start"
// cannot be answered by the pill alone.
{
  const clicked = fire("btn-speed", "click", {});
  await new Promise((resolve) => realSetTimeout(resolve, 100));
  check("clicking Speed test opens the progress modal",
    clicked && element("speed-modal").hidden === false);

  const stream = eventSources[eventSources.length - 1];
  const frame = {
    phase: "download", elapsed_s: 3.0, expected_s: 10.0,
    bytes: 180_000_000, mbps: 452.3,
    intervals: [{ t: 0, mbps: 430 }, { t: 1, mbps: 470 }, { t: 2, mbps: 452 }],
    tier: "sustained", trigger: "manual", phases: ["download", "upload"], results: {},
  };
  stream.handlers.speed_progress?.({ data: JSON.stringify(frame) });
  check("a live frame shows the running rate",
    element("speed-down-rate").textContent.includes("452"),
    `rate="${element("speed-down-rate").textContent}"`);
  check("the progress bar tracks elapsed time",
    element("speed-down-bar").style.width === "30.0%",
    `width="${element("speed-down-bar").style.width}"`);
  check("per-second bars are drawn as the test runs",
    html("speed-down-ticks").includes("<span"),
    `ticks length ${html("speed-down-ticks").length}`);

  // Finishing hands over to a result view, then it closes itself.
  stream.handlers.sample?.({ data: JSON.stringify({ type: "sample", sample: {
    kind: "speed", tier: "sustained", trigger: "manual",
    download_mbps: 463.8, upload_mbps: 171.2, download_bytes: 580_000_000,
    upload_bytes: 214_000_000, elapsed_ms: 20_100, download_decay_pct: 0.0,
    download_intervals: [{ t: 0, mbps: 430 }, { t: 1, mbps: 470 }],
    upload_intervals: [{ t: 0, mbps: 170 }],
  } }) });
  check("finishing shows both results",
    element("speed-down-rate").textContent.includes("464")
      && element("speed-up-rate").textContent.includes("171"),
    `down=${element("speed-down-rate").textContent} up=${element("speed-up-rate").textContent}`);
  check("the result mentions the data moved",
    element("speed-modal-sub").textContent.includes("GB")
      || element("speed-modal-sub").textContent.includes("MB"),
    `"${element("speed-modal-sub").textContent}"`);
  element("speed-modal-hide").click?.();
  fire("speed-modal-hide", "click", {});
  check("Hide closes the modal", element("speed-modal").hidden === true);
}

// Only a run you asked for may cover the screen. The same events for a scheduled
// run have to stay in the Speed tests header, or the dashboard pops a modal over
// the charts for a test nobody started by hand.
{
  const stream = eventSources[eventSources.length - 1];
  const note = () => element("note-speedtests").textContent;

  stream.handlers.probing?.({ data: JSON.stringify({
    type: "probing", kind: "speed", tier: "sustained", trigger: "scheduled" }) });
  check("a scheduled test does not open the modal", element("speed-modal").hidden === true);
  check("a scheduled test still says it is running",
    note().includes("scheduled"), `note="${note()}"`);

  stream.handlers.speed_progress?.({ data: JSON.stringify({
    phase: "upload", elapsed_s: 4.0, expected_s: 10.0, bytes: 90_000_000, mbps: 71.4,
    intervals: [{ t: 0, mbps: 70 }], tier: "sustained", trigger: "scheduled",
    phases: ["download", "upload"], results: { download_mbps: 480.2 } }) });
  check("...with the live rate in the header",
    note().includes("71.4") && note().includes("upload") && note().includes("40%"),
    `note="${note()}"`);
  check("...and still nothing over the charts", element("speed-modal").hidden === true);

  stream.handlers.sample?.({ data: JSON.stringify({ type: "sample", sample: {
    kind: "speed", tier: "sustained", trigger: "scheduled",
    download_mbps: 480.2, upload_mbps: 71.4, download_bytes: 600_000_000,
    upload_bytes: 90_000_000, elapsed_ms: 20_000 } }) });
  check("a scheduled result leaves the screen alone too",
    element("speed-modal").hidden === true);
  check("...and is summarised in the header",
    note().includes("480") && note().includes("scheduled"),
    `note="${note()}"`);

  // The 10-minute burst is automatic as well, and used to flash a result modal.
  stream.handlers.probing?.({ data: JSON.stringify({
    type: "probing", kind: "speed", tier: "quick", trigger: "scheduled" }) });
  stream.handlers.sample?.({ data: JSON.stringify({ type: "sample", sample: {
    kind: "speed", tier: "quick", trigger: "scheduled", download_mbps: 511.0,
    upload_mbps: 88.0, download_bytes: 5_000_000, upload_bytes: 2_000_000,
    elapsed_ms: 1_400 } }) });
  check("a scheduled burst does not open the modal either",
    element("speed-modal").hidden === true);
  check("...and reports itself in the header",
    note().includes("burst") && note().includes("511"), `note="${note()}"`);

  // A manual run keeps the modal, and it still says who asked for it.
  stream.handlers.probing?.({ data: JSON.stringify({
    type: "probing", kind: "speed", tier: "sustained", trigger: "manual" }) });
  check("a manual test opens the modal", element("speed-modal").hidden === false);
  check("the manual modal says who started it",
    element("speed-modal-title").textContent.includes("started by you"),
    `title="${element("speed-modal-title").textContent}"`);

  // A scheduled run finishing while you watch your own must not write into it.
  stream.handlers.sample?.({ data: JSON.stringify({ type: "sample", sample: {
    kind: "speed", tier: "sustained", trigger: "scheduled",
    download_mbps: 480.2, upload_mbps: 71.4, download_bytes: 600_000_000,
    upload_bytes: 90_000_000, elapsed_ms: 20_000 } }) });
  check("a scheduled result cannot overwrite a manual modal",
    element("speed-modal-title").textContent.includes("started by you")
      && !element("speed-modal-sub").textContent.includes("GB"),
    `title="${element("speed-modal-title").textContent}" sub="${element("speed-modal-sub").textContent}"`);

  // Hiding it mid-run hands the numbers to the header rather than losing them.
  fire("speed-modal-hide", "click", {});
  stream.handlers.speed_progress?.({ data: JSON.stringify({
    phase: "download", elapsed_s: 2.0, expected_s: 10.0, bytes: 40_000_000, mbps: 160.0,
    intervals: [], tier: "sustained", trigger: "manual", phases: ["download", "upload"], results: {} }) });
  check("a hidden manual run falls back to the header",
    element("speed-modal").hidden === true && note().includes("160"),
    `note="${note()}"`);
}

// The point of the modal is reading the result of the run you just started, so a
// refresh landing straight after it must not sweep it away. It used to close
// within half a second, long before anyone could read the numbers.
{
  const stream = eventSources[eventSources.length - 1];
  const result = {
    kind: "speed", tier: "sustained", trigger: "manual",
    download_mbps: 463.8, upload_mbps: 171.2, download_bytes: 580_000_000,
    upload_bytes: 214_000_000, elapsed_ms: 20_100,
  };
  stream.handlers.probing?.({ data: JSON.stringify({
    type: "probing", kind: "speed", tier: "sustained", trigger: "manual" }) });
  stream.handlers.sample?.({ data: JSON.stringify({ type: "sample", sample: result }) });
  check("a finished manual run shows its numbers",
    element("speed-modal").hidden === false
      && element("speed-down-rate").textContent.includes("464")
      && element("speed-up-rate").textContent.includes("171")
      && /(MB|GB)/.test(element("speed-modal-sub").textContent),
    `down=${element("speed-down-rate").textContent} up=${element("speed-up-rate").textContent} sub="${element("speed-modal-sub").textContent}"`);

  stream.handlers.state?.({ data: JSON.stringify({ type: "state", state: {} }) });
  await new Promise((resolve) => realSetTimeout(resolve, 600));
  check("the result is still readable after a refresh",
    element("speed-modal").hidden === false,
    "the refresh sweep closed the result view");

  fire("speed-modal-hide", "click", {});
  check("Hide closes a finished result too", element("speed-modal").hidden === true);
}

// Controls must be wired, not just present.
check("pause button wired", !!listeners.get("btn-toggle")?.click);
check("probe button wired", !!listeners.get("btn-ping")?.click);
check("speed button wired", !!listeners.get("btn-speed")?.click);
check("apply button wired", !!listeners.get("btn-apply")?.click);
check("reset button wired", !!listeners.get("btn-reset")?.click);
check("range chips wired", !!listeners.get("range-group")?.click);

// The rendered values should be plausible, not placeholders.
notes.push(`  uptime=${text("v-uptime")}%  ping=${text("v-ping")}ms  down=${text("v-down")}Mbps  rounds=${text("f-rounds")}`);
notes.push(`  tagline: ${text("target-line")}`);
notes.push(`  outages card: ${text("v-outages")} — ${text("m-outages")}`);
notes.push(`  uptime card:  ${text("v-uptime")}% — ${text("m-uptime")}`);
notes.push(`  route: ${text("f-next-ping")} / ${text("f-next-quick")} / ${text("f-next-sustained")}`);
notes.push(`  ${text("strip-range")}`);
notes.push(`  charts: ${charts.map((c) => `${c.data.datasets.length} datasets`).join(", ")}`);

console.log(`\n  ${"—".repeat(58)}`);
notes.forEach((line) => console.log(line));
if (failures.length) {
  console.log(`\n  ${failures.length} failure(s):`);
  failures.forEach((line) => console.log(`   • ${line}`));
  console.log();
  process.exit(1);
}
console.log("\n  all frontend checks passed\n");
process.exit(0);
