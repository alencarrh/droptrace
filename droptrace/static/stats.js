/* Statistics page: numbers the owner reads and explains. No prose, no narration.
 *
 * Everything is plain DOM: the bars are divs, so the page needs no chart
 * library and screenshots cleanly at any scale.
 */
(() => {
  "use strict";

  const WINDOWS = [
    { key: "24h", label: "24h" },
    { key: "7d", label: "7 days" },
    { key: "30d", label: "30 days" },
    { key: "all", label: "everything" },
  ];

  // ?summary=1 and ?window=30d are honoured so a URL can be bookmarked for
  // screenshotting without touching the controls first.
  const params = new URLSearchParams(location.search);
  // Auto-refresh choices. "off" is the default so the page does not fetch behind
  // your back; ?refresh=30s makes the choice book-markable like the window is.
  const REFRESH = [
    { key: "off", label: "off", ms: 0 },
    { key: "10s", label: "10s", ms: 10_000 },
    { key: "30s", label: "30s", ms: 30_000 },
    { key: "1m", label: "1m", ms: 60_000 },
    { key: "10m", label: "10m", ms: 600_000 },
  ];
  const requested = (params.get("refresh") || "off").toLowerCase();
  // A vantage point counts as on when it reported within this many seconds.
  const DEVICE_ONLINE_S = 60;

  const state = {
    window: params.get("window") || "7d",
    summary: params.get("summary") === "1",
    refresh: REFRESH.some((r) => r.key === requested) ? requested : "off",
    data: null,
    loadedAt: null,
    loading: false,
  };
  let refreshTimer = null;

  const $ = (id) => document.getElementById(id);

  // Feedback while a window is being re-read; no-op if loading.js is missing.
  const loading = window.dropTraceLoading || { begin() {}, end() {} };

  const num = (v, digits = 0) =>
    v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });

  const ms = (v) => (v === null || v === undefined ? "—" : `${num(v, v < 10 ? 1 : 0)}`);

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const s = Math.round(Number(seconds));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ${m % 60}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  }

  function fmtBytes(bytes) {
    const b = Number(bytes) || 0;
    if (b < 1024) return `${b} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = b / 1024;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
  }

  const pct = (v, digits = 1) => (v === null || v === undefined ? "—" : `${num(v, digits)}%`);

  /** "14:00" for an hour bucket, so a worst-hour cell says which hour it was. */
  /** "09h" for an hour bucket. Deliberately short: the cell already carries the
   *  percentage, and the table is half a screen wide. The exact timestamp is in
   *  the cell's title. */
  const hourLabel = (ts) => (ts ? `${String(new Date(ts * 1000).getHours()).padStart(2, "0")}h` : "");

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  function bars(el, values, labels, options = {}) {
    const max = Math.max(1, ...values);
    el.innerHTML = values.map((v, i) => {
      const height = Math.round((v / max) * 100);
      const hot = options.hot && v > 0 && i >= (options.hotFrom ?? 0);
      const title = `${labels[i]}: ${num(v)}`;
      return `<span class="bar${hot ? " hot" : ""}" style="height:${Math.max(v ? 6 : 1, height)}%"
                    title="${esc(title)}"></span>`;
    }).join("");
    if (options.axis) {
      el.insertAdjacentHTML("afterend",
        `<div class="bar-axis">${labels.map((l) => `<span>${esc(l)}</span>`).join("")}</div>`);
    }
  }

  function renderCards(t) {
    const uptime = t.uptime_pct === null ? "—" : `${num(t.uptime_pct, 2)}%`;
    $("stat-cards").innerHTML = [
      ["Uptime", uptime, `${num(t.rounds - t.down_rounds)} of ${num(t.rounds)} rounds`],
      ["Drops", num(t.incidents), `${fmtDuration(t.downtime_s)} total`],
      ["Longest drop", fmtDuration(t.longest_s), `mean ${fmtDuration(t.mean_s)}`],
      ["Internet probes", num(t.probes), `${num(t.failed_probes)} failed`],
      ["Failed probes", t.fail_pct === null ? "—" : `${num(t.fail_pct, 2)}%`,
        "of internet connection attempts"],
      ["Avg latency", `${ms(t.avg_ms)} ms`, `jitter ${ms(t.jitter_ms)} ms`],
      ["DNS-failed rounds", num(t.dns_rounds), "resolution, not the link"],
      ["Data used", fmtBytes(t.data_down + t.data_up),
        `${fmtBytes(t.data_down)} down / ${fmtBytes(t.data_up)} up`],
    ].map(([label, value, sub]) => `
      <article class="card accent-cyan">
        <header><h3>${esc(label)}</h3></header>
        <p class="metric">${esc(value)}</p>
        <p class="meta">${esc(sub)}</p>
      </article>
    `).join("");
  }

  function renderDaily(rows) {
    const body = $("daily-body");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="13" class="empty">no probes in this window</td></tr>';
      return;
    }
    body.innerHTML = rows.map((d) => {
      const poor = (d.uptime_pct ?? 100) < 99;
      const att = [];
      if (d.scope_isp) att.push(`isp ${d.scope_isp}`);
      if (d.scope_local) att.push(`local ${d.scope_local}`);
      if (d.scope_internet) att.push(`net ${d.scope_internet}`);
      return `<tr${poor ? ' class="row-bad"' : ""}>
        <td>${esc(d.day)}</td>
        <td class="num">${d.uptime_pct === null ? "—" : `${num(d.uptime_pct, 2)}%`}</td>
        <td class="num">${num(d.rounds)}</td>
        <td class="num">${num(d.down_rounds)}</td>
        <td class="num">${num(d.incidents)}</td>
        <td class="num">${fmtDuration(d.incident_s)}</td>
        <td class="num">${fmtDuration(d.longest_s ?? 0)}</td>
        <td class="num">${num(d.probes)}</td>
        <td class="num">${num(d.failed)}</td>
        <td class="num">${d.fail_pct === null ? "—" : `${num(d.fail_pct, 1)}%`}</td>
        <td class="num">${pct(d.loss_pct, 1)}</td>
        <td class="num">${ms(d.avg_ms)}</td>
        <td class="num">${ms(d.max_ms)}</td>
        <td class="num dim">${esc(att.join(" · ") || "—")}</td>
      </tr>`;
    }).join("");

    // A strip of coloured cells, one per day: dark = good, red = drops.
    const withUptime = rows.filter((d) => d.uptime_pct !== null);
    $("day-strip").innerHTML = withUptime.map((d) => {
      const u = d.uptime_pct;
      const hue = Math.max(0, Math.min(120, (u - 90) * 12));
      const title = `${d.day}: ${num(u, 2)}% uptime, ${d.incidents} drops, ${fmtDuration(d.incident_s)}`;
      return `<span class="day-cell" style="--h:${hue}" title="${esc(title)}"></span>`;
    }).join("");
  }

  function renderIncidents(inc) {
    $("inc-count").textContent = num(inc.count);
    $("inc-total").textContent = fmtDuration(inc.downtime_s);
    $("inc-longest").textContent = fmtDuration(inc.longest_s);
    $("inc-mean").textContent = fmtDuration(inc.mean_s);
    bars($("inc-histogram"), inc.histogram.map((h) => h.count), inc.histogram.map((h) => h.label));
    bars($("inc-hours"), inc.by_hour, [...Array(24).keys()].map((h) => `${h}h`));
    const top = inc.top_error;
    $("inc-error").textContent = top
      ? `most common error: ${top.error} (${num(top.count)}x)`
      : "no failures recorded";
    const scopeNames = { isp: "ISP / upstream", local: "local network", internet: "no router evidence", dns: "DNS" };
    $("inc-scopes").innerHTML = Object.entries(inc.by_scope).length
      ? Object.entries(inc.by_scope).map(([k, v]) =>
          `<div class="stat-line"><span>${esc(scopeNames[k] || k)}</span><strong>${num(v)}</strong></div>`
        ).join("")
      : '<div class="stat-line"><span>no drops in this window</span><strong></strong></div>';
  }

  function renderTargets(rows) {
    const body = $("target-body");
    body.innerHTML = rows.length ? rows.map((t) => {
      const bad = (t.fail_pct ?? 0) > 1;
      return `<tr${bad ? ' class="row-bad"' : ""}>
        <td>${esc(t.target)}</td>
        <td><span class="pill ${{
          lan: "local", local: "local", internet: "net",
          dns: "dns", "dns-public": "dns", "dns-upstream": "dns",
        }[t.role] || "local"}">${esc(t.role)}</span></td>
        <td class="num">${num(t.probes)}</td>
        <td class="num">${num(t.failed)}</td>
        <td class="num">${t.fail_pct === null ? "—" : `${num(t.fail_pct, 2)}%`}</td>
        <td class="num">${pct(t.loss_pct, 2)}</td>
        <td class="num" title="${t.worst_hour ? esc(new Date(t.worst_hour * 1000).toLocaleString()) : ""}">${
          pct(t.worst_loss_pct, 1)}<span class="dim">${t.worst_hour ? ` ${hourLabel(t.worst_hour)}` : ""}</span></td>
        <td class="num">${ms(t.avg_ms)}</td>
        <td class="num">${ms(t.max_ms)}</td>
        <td class="num">${ms(t.jitter_ms)}</td>
      </tr>`;
    }).join("") : '<tr><td colspan="10" class="empty">nothing measured yet</td></tr>';
  }

  function renderFailures(fail) {
    $("error-body").innerHTML = fail.top_errors.length
      ? fail.top_errors.map((e) => `<tr><td class="wrap">${esc(e.error)}</td><td class="num">${num(e.count)}</td></tr>`).join("")
      : '<tr><td colspan="2" class="empty">no failures in this window</td></tr>';
    $("failtarget-body").innerHTML = fail.by_target.length
      ? fail.by_target.map((t) => `<tr><td>${esc(t.target)}</td><td class="num">${num(t.count)}</td></tr>`).join("")
      : '<tr><td colspan="2" class="empty">—</td></tr>';
  }

  /**
   * The device rows, one hour per cell, and three states that must not be
   * confused with each other:
   *
   *   online, no failures   a short green tick
   *   online, failing       a red bar, scaled against the worst hour on screen
   *   not reporting         a full-height hatched column
   *
   * The third one is the whole point: a phone that was switched off for an hour
   * and a phone that was up and lost nothing both have zero failures, and
   * reading the first as the second turns a comparison into a lie.
   */
  function renderDevices(devices) {
    const rows = devices.devices || [];
    const hoursTotal = devices.hours_total || devices.hours.length;
    $("devices-body").innerHTML = rows.length ? rows.map((d) => `
      <tr data-source="${esc(d.source)}">
        <td>${esc(d.source)}<span class="pill device-state"
              data-device-age="${d.last_seen_ago_s ?? ""}" data-device-at="${Date.now()}"></span>${
          d.source === "local" ? ' <span class="pill local">this machine</span>' : ""}</td>
        <td class="dim">${esc(d.platform || (d.source === "local" ? "this machine" : "—"))}${
          d.agent ? ` <span class="pill">${esc(d.agent)}</span>` : ""}</td>
        <td class="num" title="Hours in which this device reported at least one probe">${
          num(d.hours_online ?? 0)}<span class="dim">/${num(hoursTotal)}h</span></td>
        <td class="num">${num(d.probes)}</td>
        <td class="num">${num(d.failed)}</td>
        <td class="num">${d.fail_pct === null ? "—" : `${num(d.fail_pct, 1)}%`}</td>
        <td class="num dim">${d.first_ts ? new Date(d.first_ts * 1000).toLocaleTimeString() : "—"}</td>
        <td class="num dim">${d.last_ts ? new Date(d.last_ts * 1000).toLocaleTimeString() : "—"}</td>
      </tr>`).join("")
      : '<tr><td colspan="8" class="empty">only this machine so far — open /agent on a phone to add one</td></tr>';
    refreshDeviceStates();

    if (!rows.length || !devices.hours.length) {
      $("device-hours").innerHTML = "";
      return;
    }
    const hours = devices.hours.map((h) => h.hour);
    const peak = Math.max(1, ...devices.hours.flatMap((h) =>
      Object.values(h.by_source || {}).map((s) => s.failed)));
    $("device-hours").innerHTML = devices.sources.map((source) => {
      const cells = devices.hours.map((h) => {
        const cell = (h.by_source || {})[source];
        const at = new Date(h.hour * 1000).toLocaleString();
        if (!cell || !cell.probes) {
          return `<span class="bar off" title="${esc(source)} · ${at} — no probes: not reporting"></span>`;
        }
        if (!cell.failed) {
          return `<span class="bar up" title="${esc(source)} · ${at} — ${num(cell.probes)} probes, none failed"></span>`;
        }
        const level = Math.max(18, Math.round((cell.failed / peak) * 100));
        return `<span class="bar hot" style="height:${level}%"
                      title="${esc(source)} · ${at} — ${num(cell.failed)} of ${num(cell.probes)} probes failed"></span>`;
      }).join("");
      const online = devices.hours.filter((h) => ((h.by_source || {})[source] || {}).probes).length;
      return `<div class="device-row"><span class="device-name">${esc(source)}
                <span class="dim">${num(online)}/${num(devices.hours.length)}h</span></span>
                <div class="bars hours">${cells}</div></div>`;
    }).join("") + `<div class="bar-axis"><span>${new Date(hours[0] * 1000).toLocaleTimeString()}</span>
        <span>${new Date(hours[hours.length - 1] * 1000).toLocaleTimeString()}</span></div>`;
    // The panel note belongs to the on/off pills (refreshDeviceStates owns it),
    // so nothing here overwrites the live count with a static sentence.
  }

  /** How long ago each device last reported, counting on from when we were told. */
  function deviceAge(el) {
    const seconds = el.dataset.deviceAge;
    if (seconds === null || seconds === undefined || seconds === "") return null;
    const told = Number(el.dataset.deviceAt) || Date.now();
    return Number(seconds) + (Date.now() - told) / 1000;
  }

  function fmtAge(seconds) {
    if (seconds === null) return "never";
    if (seconds < 90) return `${Math.round(seconds)}s`;
    if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
    return `${(seconds / 3600).toFixed(1)}h`;
  }

  /**
   * Flip the on/off pill without refetching the statistics: a device that has
   * gone quiet should read "off" a minute after it stopped, not when the next
   * refresh lands (which could be ten minutes away, or never with auto refresh
   * off). Ages are counted on from the moment we were told them.
   */
  function refreshDeviceStates() {
    document.querySelectorAll(".device-state").forEach((el) => {
      const age = deviceAge(el);
      const on = age !== null && age <= DEVICE_ONLINE_S;
      el.textContent = on ? "on" : "off";
      el.classList.toggle("ok", on);
      el.classList.toggle("bad", !on);
      el.title = age === null
        ? "never reported"
        : `last report ${fmtAge(age)} ago${on ? "" : " — off for the last minute"}`;
    });
    const unknown = [...document.querySelectorAll(".device-state")].filter((el) => !el.dataset.deviceAge).length;
    const devices = state.data ? state.data.devices.devices.length : 0;
    if (devices) {
      const on = [...document.querySelectorAll(".device-state.ok")].length;
      $("devices-note").textContent =
        `${on} of ${devices} reporting now${unknown ? ` · ${unknown} never reported` : ""}`;
    }
  }

  /**
   * Ask the devices themselves rather than trusting the last statistics load:
   * a few rows over the loopback keep the pill honest however rarely the page
   * refreshes. If the poll fails the counted-on ages stand, so an offline
   * server shows devices drifting to "off" instead of freezing them "on".
   */
  async function pollDeviceStates() {
    if (document.visibilityState !== "visible" || !document.querySelector(".device-state")) return;
    let payload = null;
    try {
      const response = await fetch("/api/devices/live", { cache: "no-store" });
      if (response.ok) payload = await response.json();
    } catch { /* offline: the counted-on ages keep ticking down */ }
    if (!payload) return;
    const at = Date.now();
    const seen = new Map((payload.devices || []).map((d) => [d.source, d.last_seen_ago_s]));
    document.querySelectorAll(".device-state").forEach((el) => {
      const row = el.closest("tr");
      const source = row ? row.dataset.source : null;
      if (!source || !seen.has(source)) return;
      el.dataset.deviceAge = seen.get(source);
      el.dataset.deviceAt = at;
    });
    refreshDeviceStates();
  }

  function tickDevices() {
    refreshDeviceStates();
    pollDeviceStates();
  }

  function renderThroughput(speed) {
    $("speed-body").innerHTML = ["quick", "sustained"].map((tier) => {
      const t = speed[tier];
      const name = tier === "quick" ? "burst (10m)" : "sustained (1h)";
      return `<tr>
        <td>${name}</td>
        <td class="num">${num(t.count)}</td>
        <td class="num">${num(t.manual)}</td>
        <td class="num">${ms(t.down_avg)}</td>
        <td class="num">${ms(t.down_best)}</td>
        <td class="num">${ms(t.down_worst)}</td>
        <td class="num">${ms(t.up_avg)}</td>
        <td class="num">${ms(t.up_best)}</td>
        <td class="num">${ms(t.up_worst)}</td>
      </tr>`;
    }).join("");
  }

  function renderCoverage(cov) {
    const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");
    $("coverage").innerHTML = [
      ["Oldest probe", fmtDate(cov.first_ts)],
      ["Newest probe", fmtDate(cov.last_ts)],
      ["Individual probes kept since", fmtDate(cov.raw_cutoff)],
      ["Rows on disk", num(cov.samples)],
      ["Hours summarised", num(cov.rolled_hours)],
      ["Database size", fmtBytes(cov.db_bytes)],
    ].map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
  }

  function applySummaryMode() {
    document.body.classList.toggle("summary-mode", state.summary);
    const rows = state.data ? state.data.daily.slice(-(state.summary ? 7 : 400)) : [];
    if (state.data) renderDaily(rows.reverse());
  }

  function renderAll() {
    const d = state.data;
    if (!d) return;
    const since = new Date(d.window.since * 1000).toLocaleString();
    const until = new Date(d.window.until * 1000).toLocaleString();
    $("stats-window").textContent = `${since} → ${until}`;
    const topError = d.failures.top_errors[0];
    d.incidents.top_error = topError || null;
    $("stats-foot").textContent = d.failures.total
      ? `${num(d.failures.total)} failed probes on record · raw probes kept ${num(
          Math.round((d.window.until - d.coverage.raw_cutoff) / 86400))} days back`
      : "no failed probes in this window";
    renderCards(d.totals);
    renderDaily(d.daily.slice(-400).reverse());
    renderIncidents(d.incidents);
    renderTargets(d.targets);
    renderFailures(d.failures);
    renderThroughput(d.throughput);
    renderDevices(d.devices || { devices: [], hours: [], sources: [] });
    renderPaths(d.paths || { drop: null, baseline: null, count: 0 });
    renderLoss(d.loss || { count: 0, rows: [] });
    renderCoverage(d.coverage);
  }

  /**
   * The counted bursts: what the loss actually was while the link was "up".
   *
   * A round probe can only report 0% or 100%, so this is the panel that turns
   * "it stops for a few seconds" into a number an ISP can be handed.
   */
  function renderLoss(loss) {
    const when = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "—");
    // The newest few, not all of them: a bad week is hundreds of bursts, and a
    // panel that pushes the traced path three screens down is not evidence, it
    // is a wall. The header states the totals; the CSV has every row.
    const all = loss.rows || [];
    const rows = all.slice(0, 12);
    $("loss-body").innerHTML = rows.length ? rows.map((b) => {
      const bad = (b.loss_pct ?? 0) > 0;
      return `<tr${bad ? ' class="row-bad"' : ""}>
        <td class="dim">${when(b.ts)}</td>
        <td>${esc(b.target)}</td>
        <td class="num">${num(b.sent)}</td>
        <td class="num">${num(b.lost)}</td>
        <td class="num">${pct(b.loss_pct, 1)}</td>
        <td class="num">${ms(b.min_ms)}</td>
        <td class="num">${ms(b.avg_ms)}</td>
        <td class="num">${ms(b.max_ms)}</td>
        <td>${b.sent && b.lost === b.sent
          ? `<span class="pill bad">${esc((b.error || "all lost").slice(0, 60))}</span>`
          : '<span class="pill ok">partial</span>'}</td>
      </tr>`;
    }).join("") : '<tr><td colspan="9" class="empty">no burst yet</td></tr>';

    $("loss-note").textContent = loss.count
      ? `worst ${pct(loss.worst_pct, 0)} at ${when(loss.worst_ts)} · ${num(loss.lost)} of `
        + `${num(loss.handshakes)} handshakes lost over ${num(loss.count)} bursts · `
        + `avg ${pct(loss.avg_pct, 1)}${all.length > rows.length ? ` · newest ${num(rows.length)}` : ""}`
      : "counted, not inferred: a burst of handshakes fired when a round found nothing";
  }

  /**
   * The trace at the drop, above the same trace when the line was healthy.
   *
   * Read as a pair: "the path stopped after hop 4" only means something next to
   * "it normally reaches hop 18", and the hop list below is what names the
   * equipment it stopped at.
   */
  function renderPaths(paths) {
    const rows = [paths.baseline, paths.drop].filter(Boolean);
    const when = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "—");
    $("path-body").innerHTML = rows.length ? rows.map((t) => {
      const stop = t.last_hop || "—";
      const result = t.reached
        ? `<span class="pill ok">reached</span>`
        : `<span class="pill bad">${esc(t.error || "no reply")}</span>`;
      return `<tr>
        <td><span class="pill ${t.trigger === "drop" ? "bad" : "ok"}">${esc(t.trigger)}</span>
          <span class="dim">${esc(t.tracer)} · ${esc(t.host)}</span></td>
        <td class="dim">${when(t.ts)}</td>
        <td class="num">${num(t.hops)}${t.max_hops ? `<span class="dim">/${num(t.max_hops)}</span>` : ""}</td>
        <td class="num">${num(t.answered)}</td>
        <td>${esc(stop)}</td>
        <td>${result}</td>
      </tr>`;
    }).join("") : '<tr><td colspan="6" class="empty">no trace yet</td></tr>';

    if (paths.drop && paths.baseline) {
      $("path-note").textContent = paths.drop.reached
        ? "the traced target answered during the drop"
        : `healthy path reaches ${paths.baseline.last_hop || "—"} in ${num(paths.baseline.hops)} hops`;
    } else if (paths.baseline) {
      $("path-note").textContent =
        `healthy path reaches ${paths.baseline.last_hop || "—"} in ${num(paths.baseline.hops)} hops · no drop traced yet`;
    } else if (paths.drop) {
      $("path-note").textContent = "no healthy trace to compare with yet";
    } else {
      $("path-note").textContent = "traced when a drop starts, and hourly while healthy";
    }

    const drop = paths.drop || paths.baseline;
    const hops = drop ? drop.hop_list || [] : [];
    $("path-hops-note").textContent = drop
      ? `hop by hop at ${when(drop.ts)} · ${esc(drop.tracer)}`
      : "hop by hop, at the drop";
    $("path-hops").innerHTML = hops.length ? hops.map((hop) => `<tr>
      <td class="num">${num(hop.ttl)}</td>
      <td>${hop.host ? esc(hop.host) : '<span class="dim">no reply</span>'}</td>
      <td class="num">${hop.rtt_ms === null || hop.rtt_ms === undefined ? "—" : num(hop.rtt_ms, 1)}</td>
      <td class="dim">${esc(hop.note || "")}</td>
    </tr>`).join("") : '<tr><td colspan="4" class="empty">—</td></tr>';
  }

  function renderRangeChips() {
    $("stats-range").innerHTML = WINDOWS.map((w) =>
      `<button class="chip${w.key === state.window ? " is-active" : ""}" data-window="${w.key}" type="button">${esc(w.label)}</button>`
    ).join("");
  }

  async function load({ quiet = false, explicit = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    loading.begin({ explicit });
    if (!quiet) $("daily-body").innerHTML = '<tr><td colspan="13" class="empty">loading…</td></tr>';
    try {
      const response = await fetch(`/api/stats?window=${encodeURIComponent(state.window)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      state.data = await response.json();
      state.loadedAt = new Date();
      renderAll();
      applySummaryMode();
    } catch (error) {
      if (!quiet) {
        $("daily-body").innerHTML =
          `<tr><td colspan="13" class="empty">could not load statistics: ${esc(error.message)}</td></tr>`;
      }
    } finally {
      state.loading = false;
      loading.end();
      updateLoadedLabel();
    }
  }

  function updateLoadedLabel() {
    const at = state.loadedAt ? state.loadedAt.toLocaleTimeString() : "—";
    const every = REFRESH.find((r) => r.key === state.refresh);
    $("stats-updated").textContent = state.loadedAt
      ? every && every.ms
        ? `updated ${at} · refreshing every ${every.label}`
        : `updated ${at} · auto refresh off`
      : "not loaded yet";
  }

  function renderRefreshChips() {
    $("stats-refresh").innerHTML =
      '<span class="countdown-label">auto</span>' +
      REFRESH.map((r) => `<button class="chip${r.key === state.refresh ? " is-active" : ""}"
        data-refresh="${r.key}" type="button">${esc(r.label)}</button>`).join("");
  }

  /** A chained timeout rather than an interval: nothing piles up if a fetch is slow. */
  function scheduleAutoRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = null;
    const every = REFRESH.find((r) => r.key === state.refresh);
    if (!every || !every.ms) { updateLoadedLabel(); return; }
    refreshTimer = setTimeout(async () => {
      if (document.visibilityState === "visible") await load({ quiet: true });
      scheduleAutoRefresh();
    }, every.ms);
    updateLoadedLabel();
  }

  $("stats-refresh").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-refresh]");
    if (!chip) return;
    state.refresh = chip.dataset.refresh;
    renderRefreshChips();
    scheduleAutoRefresh();
  });

  // No point polling a page nobody is looking at; catch up on return.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      const every = REFRESH.find((r) => r.key === state.refresh);
      if (every && every.ms) load({ quiet: true }).then(scheduleAutoRefresh);
      else scheduleAutoRefresh();
    }
  });

  $("stats-range").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-window]");
    if (!chip) return;
    state.window = chip.dataset.window;
    renderRangeChips();
    load({ explicit: true });
  });

  $("in-summary").addEventListener("change", (event) => {
    state.summary = event.target.checked;
    applySummaryMode();
  });

  $("in-summary").checked = state.summary;
  document.body.classList.toggle("summary-mode", state.summary);
  if (window.dropTraceKeepAwake) {
    // A dedicated element: the footer timestamp is rewritten on every refresh.
    const status = $("awake-status");
    if (status) status.hidden = false;
    window.dropTraceKeepAwake.attach($("btn-awake"), status);
  }
  setInterval(tickDevices, 5000);
  renderRangeChips();
  renderRefreshChips();
  updateLoadedLabel();
  load({ explicit: true }).then(scheduleAutoRefresh);
})();
