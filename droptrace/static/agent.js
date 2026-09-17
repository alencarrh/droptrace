/* A vantage point in the browser: measure this device's connectivity and report
 * the numbers to DropTrace on the network.
 *
 * Why a page and not an app: nothing to install on a phone or a borrowed laptop,
 * and the requirement was that no measurement data is kept on the device. Each
 * cycle posts its probes straight to the server; the only state here is a running
 * count in memory, which disappears when the tab closes.
 *
 * What it can measure is bounded by the browser: no raw TCP, so these are HTTPS
 * requests. The names match the server's own targets so the comparison lines up
 * per destination as well as per hour.
 */
(() => {
  "use strict";

  const TARGETS = [
    { name: "github-com", label: "github.com", url: "https://github.com/favicon.ico" },
    { name: "wikipedia-org", label: "wikipedia.org", url: "https://www.wikipedia.org/static/favicon/wikipedia.ico" },
    { name: "twitch-tv", label: "twitch.tv", url: "https://www.twitch.tv/favicon.ico" },
    { name: "youtube-com", label: "youtube.com", url: "https://www.youtube.com/favicon.ico" },
    { name: "9gag-com", label: "9gag.com", url: "https://9gag.com/favicon.ico" },
    { name: "cf-speed", label: "speed.cloudflare.com", url: "https://speed.cloudflare.com/__down?bytes=1000" },
  ];
  const TIMEOUT_MS = 4000;

  const params = new URLSearchParams(location.search);
  const token = (params.get("token") || "").trim();
  // The token identifies the device, so the label is the server's answer, not
  // something this page or its URL chooses. `source` is still accepted in the URL
  // as an expectation to check against, which catches a link opened on the wrong
  // device before any data flows.
  const expected = (params.get("source") || "").trim();
  let source = "";
  const intervalS = Math.max(1, Number(params.get("interval") || 5));

  const stats = new Map(TARGETS.map((t) => [t.name, { probes: 0, failed: 0, last: null, sum: 0, worst: null }]));
  let sent = 0;
  let paused = false;
  let timer = null;
  const log = [];

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  const num = (v, d = 0) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
  const ms = (v) => (v === null || v === undefined ? "—" : `${num(v, v < 10 ? 1 : 0)}`);

  /** One HTTPS request, timed. An opaque response still proves the round trip. */
  async function measure(target) {
    const started = performance.now();
    const controller = new AbortController();
    const kill = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      await fetch(`${target.url}${target.url.includes("?") ? "&" : "?"}dt=${Date.now()}`,
        { mode: "no-cors", cache: "no-store", signal: controller.signal, redirect: "follow" });
      return { ok: true, ms: performance.now() - started, error: null };
    } catch (error) {
      return {
        ok: false,
        ms: null,
        error: error && error.name === "AbortError"
          ? `timeout after ${TIMEOUT_MS}ms`
          : `${error && error.name ? error.name : "Error"}: ${error && error.message ? error.message : ""}`.slice(0, 120),
      };
    } finally {
      clearTimeout(kill);
    }
  }

  async function cycle() {
    const ts = Date.now() / 1000;
    const probes = [];
    for (const target of TARGETS) {
      const result = await measure(target);
      const stat = stats.get(target.name);
      stat.probes += 1;
      if (result.ok) {
        stat.last = result.ms;
        stat.sum += result.ms;
        stat.worst = stat.worst === null ? result.ms : Math.max(stat.worst, result.ms);
      } else {
        stat.failed += 1;
        stat.last = null;
      }
      probes.push({
        ts, target: target.name, role: "internet", ok: result.ok,
        probe_ms: result.ms === null ? null : Math.round(result.ms * 10) / 10,
        error: result.error, round_id: Math.round(ts * 1000),
      });
      log.unshift({
        at: new Date().toLocaleTimeString(), target: target.name,
        ok: result.ok, text: result.ok ? `${ms(result.ms)} ms` : result.error,
      });
    }
    if (log.length > 40) log.length = 40;
    render();

    try {
      const response = await fetch("/api/agent", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Agent-Token": token },
        body: JSON.stringify({ probes, platform, agent: "browser" }),
      });
      if (!response.ok) {
        setState("error", response.status === 401
          ? "rejected: wrong or missing token"
          : `rejected: HTTP ${response.status}`);
        return;
      }
      sent += probes.length;
      setState("watching", "reporting");
    } catch (error) {
      // The server is unreachable from here, which is itself worth knowing --
      // but there is nowhere to report it to, so say it on screen.
      setState("error", `cannot reach the server: ${error.message}`);
    }
  }

  function setState(state, text) {
    const pill = $("agent-state");
    pill.dataset.state = state;
    $("agent-state-text").textContent = text;
  }

  function render() {
    const rows = [...stats.entries()].map(([name, s]) => {
      const target = TARGETS.find((t) => t.name === name);
      const failPct = s.probes ? (100 * s.failed) / s.probes : 0;
      const avg = s.probes - s.failed > 0 ? s.sum / (s.probes - s.failed) : null;
      return `<tr${failPct > 1 ? ' class="row-bad"' : ""}>
        <td>${esc(target ? target.label : name)}</td>
        <td class="num">${s.probes}</td>
        <td class="num">${s.failed}</td>
        <td class="num">${s.probes ? `${num(failPct, 1)}%` : "—"}</td>
        <td class="num">${ms(s.last)}</td>
        <td class="num">${ms(avg)}</td>
        <td class="num">${ms(s.worst)}</td>
      </tr>`;
    }).join("");
    $("agent-body").innerHTML = rows;

    $("agent-log").innerHTML = log.length
      ? log.slice(0, 20).map((l) => `<tr${l.ok ? "" : ' class="row-bad"'}>
          <td>${esc(l.at)}</td><td>${esc(l.target)}</td>
          <td class="wrap">${esc(l.text)}</td></tr>`).join("")
      : '<tr><td colspan="3" class="empty">—</td></tr>';

    const total = [...stats.values()].reduce((a, s) => a + s.probes, 0);
    const failed = [...stats.values()].reduce((a, s) => a + s.failed, 0);
    $("agent-cards").innerHTML = [
      ["Device", esc(source), "label reported to the server"],
      ["Probes", total, `${failed} failed`],
      ["Failures", total ? `${num((100 * failed) / total, 1)}%` : "—", "this device"],
      ["Reported", sent, "stored centrally, not here"],
    ].map(([label, value, sub]) => `
      <article class="card accent-cyan">
        <header><h3>${esc(label)}</h3></header>
        <p class="metric">${value}</p>
        <p class="meta">${esc(sub)}</p>
      </article>`).join("");

    $("agent-count").textContent = `${sent} reported`;
    $("agent-last").textContent = log.length ? `last cycle ${log[0].at}` : "—";
    $("agent-foot").textContent = `${source} · every ${intervalS}s · server ${location.host}`;
  }

  function schedule() {
    clearTimeout(timer);
    if (paused) return;
    timer = setTimeout(async () => {
      await cycle();
      schedule();
    }, intervalS * 1000);
  }

  $("btn-toggle").addEventListener("click", () => {
    paused = !paused;
    $("btn-toggle").textContent = paused ? "Resume" : "Pause";
    setState(paused ? "idle" : "watching", paused ? "paused" : "reporting");
    schedule();
  });

  const platform = (() => {
    const ua = navigator.userAgent || "";
    const hints = navigator.userAgentData;
    const name = (hints && hints.platform) || "";
    const looks = [
      [/iPhone/i, "iPhone"], [/iPad/i, "iPad"], [/Android/i, "Android"],
      [/Macintosh|Mac OS X/i, "macOS"], [/Windows/i, "Windows"],
      [/CrOS/i, "ChromeOS"], [/Linux/i, "Linux"],
    ];
    return `${looks.find(([re]) => re.test(ua))?.[1] || name || "unknown browser"}${
      name && !looks.some(([re]) => re.test(ua)) ? ` (${name})` : ""}`;
  })();
  let started = false;

  $("agent-source").textContent = "not reporting yet";
  $("in-source").value = "…";
  $("in-server").value = location.origin;
  $("in-platform").value = platform;
  $("in-source").readOnly = true;

  // Ask the server which device this token is, before anything is measured.
  (async () => {
    if (!token) {
      setState("error", "no token in the URL");
      $("btn-continue").disabled = true;
      $("confirm-hint").textContent =
        "This link has no device token. Add this device from the dashboard and open the link it gives you.";
      return;
    }
    let who;
    try {
      const response = await fetch(`/api/agent/whoami?token=${encodeURIComponent(token)}`);
      if (!response.ok) throw new Error(response.status === 401 ? "unknown token" : `HTTP ${response.status}`);
      who = await response.json();
    } catch (error) {
      setState("error", `cannot identify this device: ${error.message}`);
      $("btn-continue").disabled = true;
      $("confirm-hint").textContent =
        error.message === "unknown token"
          ? "This token is not registered (or was revoked). Add the device again from the dashboard."
          : "Could not reach the server to check the token.";
      return;
    }
    source = who.source;
    $("in-source").value = source;
    $("agent-source").textContent = `device: ${source}`;
    $("confirm-hint").textContent = expected && expected !== source
      ? `This link was made for “${expected}” but this token belongs to “${source}”. `
        + "Open the link for the device in your hand, or continue if that is what you meant."
      : "Check the label against the device in your hand, then continue.";
    setState("idle", "ready");
  })();

  $("btn-continue").addEventListener("click", () => {
    if (!source) return;
    started = true;
    $("agent-confirm").hidden = true;
    $("btn-toggle").hidden = false;
    $("agent-cards").hidden = false;
    $("agent-results").hidden = false;
    $("agent-recent").hidden = false;
    $("agent-source").textContent = `reporting as “${source}” to ${location.host}`;
    setState("watching", "starting");
    render();
    cycle().then(schedule);
  });

  // Screen-awake handling lives in keepawake.js so every page behaves the same.
  window.dropTraceKeepAwake.attach($("btn-awake"), $("awake-status-line"));

})();
