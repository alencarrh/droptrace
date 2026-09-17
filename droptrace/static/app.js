/* DropTrace dashboard — vanilla JS + Chart.js (vendored, no CDN needed). */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const status = {
    window: "1h",
    config: null,
    live: false,
    refreshing: false,
    refreshTimer: null,
    statusAt: 0,
    status: null,
    summary: null,
    series: null,
    seriesAt: 0,
    range: null,          // {since, until} set by brushing the timeline
    incidents: [],
    ongoing: [],
    hiddenLabels: {},
  };

  /* ------------------------------------------------------------ helpers */
  const num = (v, d = 1) =>
    v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(d);

  const fmtMs = (v, d = 1) => {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    return n < 100 ? n.toFixed(d) : String(Math.round(n));
  };

  const fmtMbps = (v) => {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (n >= 100) return n.toFixed(0);
    if (n >= 10) return n.toFixed(1);
    return n.toFixed(2);
  };

  function bytesParts(b) {
    if (!b) return { v: "0", u: "B" };
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = Number(b);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return { v: i === 0 ? String(Math.round(v)) : v >= 100 ? v.toFixed(0) : v.toFixed(1), u: units[i] };
  }
  const fmtBytes = (b) => { const p = bytesParts(b); return `${p.v} ${p.u}`; };

  const fmtTime = (ts) =>
    ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : "—";

  const fmtDayTime = (ts) =>
    ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "—";

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const s = Math.max(0, Number(seconds));
    if (s < 1) return `${Math.round(s * 1000)}ms`;
    if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.round(s % 60);
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    return `${m}m ${String(sec).padStart(2, "0")}s`;
  }

  const ago = (ts) => {
    if (!ts) return "never";
    const d = Math.max(0, Date.now() / 1000 - Number(ts));
    if (d < 2) return "just now";
    if (d < 60) return `${Math.round(d)}s ago`;
    if (d < 3600) return `${Math.round(d / 60)}m ago`;
    return `${Math.round(d / 3600)}h ago`;
  };

  const escapeHtml = (text) =>
    String(text ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function getJSON(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`${url} → HTTP ${response.status}`);
    return response.json();
  }

  /* ------------------------------------------------------------- charts */
  /* Colours come from the style sheet when it defines them, so a theme can
     recolour the charts without this file knowing about it. The fallbacks are
     the built-in dark theme, so nothing changes when it defines nothing. */
  const cssVar = (name, fallback) => {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  };
  const DARK_PALETTE = ["#22d3ee", "#34d399", "#a78bfa", "#fbbf24", "#60a5fa", "#fb7185", "#f472b6", "#4ade80"];
  const PALETTE = DARK_PALETTE.map((fallback, i) => cssVar(`--chart-c${i + 1}`, fallback));
  const TARGET_COLORS = {
    cloudflare: PALETTE[0], google: PALETTE[1], resolver: PALETTE[3],
    gateway: PALETTE[2], dns: PALETTE[4], speedtest: PALETTE[5],
  };
  const colorFor = (name, i) => TARGET_COLORS[name] || PALETTE[i % PALETTE.length];
  /** The same colour at a different opacity, for fills and soft series. */
  const withAlpha = (color, alpha) => {
    const hex = String(color).trim().replace("#", "");
    if (!/^[0-9a-f]{6}$/i.test(hex)) return color;
    const n = parseInt(hex, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
  };
  const gridColor = cssVar("--chart-grid", "rgba(148,163,184,.07)");
  const tickColor = cssVar("--chart-tick", "#5f7392");
  const AXIS_COLOR = cssVar("--chart-axis", "rgba(148,163,184,.14)");
  const DANGER_INK = cssVar("--danger-ink", "#fb7185");

  if (window.Chart) {
    Chart.defaults.color = cssVar("--chart-label", "#8ba0bd");
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 11;
  }

  /* Shades recorded outages behind the latency line — the visual proof. */
  const outageShading = {
    id: "outageShading",
    beforeDatasetsDraw(chart) {
      const outages = chart.$outages || [];
      if (!outages.length) return;
      const { ctx, chartArea, scales } = chart;
      if (!chartArea || !scales.x) return;
      ctx.save();
      ctx.fillStyle = cssVar("--chart-outage-fill", "rgba(251,113,133,.14)");
      ctx.strokeStyle = cssVar("--chart-outage-stroke", "rgba(251,113,133,.35)");
      ctx.lineWidth = 1;
      outages.forEach((o) => {
        const start = Number(o.started_at);
        const end = Number(o.ended_at || Date.now() / 1000);
        let x0 = scales.x.getPixelForValue(start);
        let x1 = scales.x.getPixelForValue(end);
        if (x1 < chartArea.left || x0 > chartArea.right) return;
        const left = Math.max(chartArea.left, Math.min(x0, x1));
        const right = Math.min(chartArea.right, Math.max(x0, x1));
        const width = Math.max(2, right - left);
        ctx.fillRect(left, chartArea.top, width, chartArea.bottom - chartArea.top);
        ctx.strokeRect(left, chartArea.top, width, chartArea.bottom - chartArea.top);
      });
      ctx.restore();
    },
  };

  function baseOptions(extra = {}) {
    const base = {
      responsive: true,
      maintainAspectRatio: false,
      parsing: false,
      normalized: true,
      animation: { duration: 250 },
      interaction: { mode: "nearest", intersect: false, axis: "x" },
      plugins: {
        legend: {
          labels: { usePointStyle: true, pointStyle: "line", boxWidth: 16, boxHeight: 8, padding: 10, color: cssVar("--chart-label", "#8ba0bd"), font: { size: 10 } },
          onClick: (event, item, legend) => {
            const chart = legend.chart;
            const dataset = chart.data.datasets[item.datasetIndex];
            dataset.hidden = !dataset.hidden;
            status.hiddenLabels[dataset.label] = !!dataset.hidden;
            chart.update();
          },
        },
        tooltip: {
          backgroundColor: cssVar("--chart-tip-bg", "rgba(8,13,25,.96)"),
          borderColor: cssVar("--chart-tip-border", "rgba(148,163,184,.22)"),
          borderWidth: 1, padding: 10, cornerRadius: 8,
          titleColor: cssVar("--chart-tip-title", "#e8eefb"),
          bodyColor: cssVar("--chart-tip-body", "#c3d2e8"), boxPadding: 5,
          callbacks: {
            title: (items) => (items.length ? fmtDayTime(items[0].parsed.x) : ""),
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          // Chart.js defaults to bounds:"ticks", which stretches the axis out to
          // the nearest round tick value -- so a 15m view showed minutes of empty
          // time at the left, before any data existed. "data" ends the axis at
          // the data; the tiny grace keeps the first and last points off the frame.
          bounds: "data",
          grace: "1%",
          grid: { color: gridColor },
          border: { color: AXIS_COLOR },
          ticks: { color: tickColor, maxTicksLimit: 7, font: { size: 10 }, callback: (v) => fmtTime(v) },
        },
        y: {
          beginAtZero: true,
          grid: { color: gridColor },
          border: { display: false },
          ticks: { color: tickColor, font: { size: 10 }, maxTicksLimit: 6 },
        },
      },
    };
    return Object.assign(base, extra);
  }

  function makeChart(canvasId, type, options) {
    const canvas = $(canvasId);
    if (!canvas || !window.Chart) return null;
    return new Chart(canvas.getContext("2d"), {
      type,
      data: { datasets: [] },
      options,
      plugins: [outageShading],
    });
  }

  const latencyChart = makeChart("chart-latency", "line", baseOptions({
    scales: Object.assign(baseOptions().scales, {
      y: Object.assign(baseOptions().scales.y, {
        title: { display: true, text: "ms", color: tickColor, font: { size: 10 } },
      }),
    }),
    plugins: Object.assign(baseOptions().plugins, {
      tooltip: Object.assign(baseOptions().plugins.tooltip, {
        callbacks: {
          title: (items) => (items.length ? fmtDayTime(items[0].parsed.x) : ""),
          label: (ctx) => {
            if (ctx.dataset.$isFailure) return `${ctx.dataset.label}: no answer`;
            const raw = ctx.raw || {};
            if (raw.hourly) {
              const range = raw.min_ms != null && raw.max_ms != null
                ? ` (${fmtMs(raw.min_ms)}–${fmtMs(raw.max_ms)} ms over ${raw.probes} probes)`
                : "";
              return `${ctx.dataset.label}: ${fmtMs(ctx.parsed.y)} ms${range}`;
            }
            return `${ctx.dataset.label}: ${fmtMs(ctx.parsed.y)} ms`;
          },
        },
      }),
    }),
  }));

  const throughputChart = makeChart("chart-throughput", "line", baseOptions({
    scales: Object.assign(baseOptions().scales, {
      y: Object.assign(baseOptions().scales.y, {
        title: { display: true, text: "Mbps", color: tickColor, font: { size: 10 } },
      }),
    }),
    plugins: Object.assign(baseOptions().plugins, {
      tooltip: Object.assign(baseOptions().plugins.tooltip, {
        callbacks: {
          title: (items) => (items.length ? fmtDayTime(items[0].parsed.x) : ""),
          label: (ctx) => `${ctx.dataset.label}: ${fmtMbps(ctx.parsed.y)} Mbps`,
        },
      }),
    }),
  }));

  // Per-second view inside the most recent test: this is what exposes a link
  // that starts fast and then throttles.
  const lastTestChart = makeChart("chart-lasttest", "bar", baseOptions({
    interaction: { mode: "index", intersect: false },
    scales: Object.assign(baseOptions().scales, {
      x: {
        type: "linear",
        grid: { color: gridColor },
        border: { color: AXIS_COLOR },
        ticks: { color: tickColor, font: { size: 10 }, stepSize: 1, precision: 0,
                 callback: (v) => (v > 0 ? `${v}s` : "0") },
        title: { display: true, text: "seconds into the test", color: tickColor, font: { size: 10 } },
      },
      y: Object.assign(baseOptions().scales.y, {
        title: { display: true, text: "Mbps", color: tickColor, font: { size: 10 } },
      }),
    }),
    plugins: Object.assign(baseOptions().plugins, {
      tooltip: Object.assign(baseOptions().plugins.tooltip, {
        callbacks: {
          title: (items) => (items.length ? `${items[0].parsed.x}s into the test` : ""),
          label: (ctx) => `${ctx.dataset.label}: ${fmtMbps(ctx.parsed.y)} Mbps`,
        },
      }),
    }),
  }));

  /* ------------------------------------------------------------ charts */
  function renderLatencyChart() {
    if (!latencyChart) return;
    const bucket = (status.series && status.series.latency) || {};
    const series = bucket.series || {};
    const names = bucket.targets || [];
    const disabled = new Set(
      ((status.status && status.status.targets) || []).filter((t) => t.disabled).map((t) => t.name)
    );

    const datasets = names.map((name, index) => {
      const points = (series[name] || [])
        .filter((p) => p.probe_ms !== null && p.probe_ms !== undefined)
        .map((p) => ({
          x: p.ts, y: p.probe_ms,
          hourly: !!p.hourly, min_ms: p.min_ms, max_ms: p.max_ms, probes: p.probes,
        }));
      const color = colorFor(name, index);
      return {
        $name: name,
        label: name,
        data: points,
        borderColor: color,
        backgroundColor: `${color}22`,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0.25,
        spanGaps: false,
        fill: false,
        hidden: !!status.hiddenLabels[name],
      };
    });

    // A rug of red crosses marks rounds where a target gave no answer at all.
    const failures = [];
    names.forEach((name) => {
      if (disabled.has(name)) return;
      (series[name] || []).forEach((p) => {
        if (!p.ok) failures.push({ x: p.ts, y: 0, target: name });
      });
    });
    if (failures.length) {
      datasets.push({
        $isFailure: true,
        label: "no answer",
        data: failures,
        borderColor: DANGER_INK,
        backgroundColor: DANGER_INK,
        showLine: false,
        pointStyle: "crossRot",
        pointRadius: 4,
        pointHoverRadius: 6,
        borderWidth: 2,
      });
    }

    latencyChart.data.datasets = datasets;
    latencyChart.$outages = (status.series && status.series.incidents) || [];
    latencyChart.update("none");

    const total = (bucket.total || 0);
    if (bucket.resolution === "hourly") {
      $("note-latency").textContent =
        `ms · hourly averages of ${total} probes · hover for the range · shaded = recorded outage`;
    } else {
      $("note-latency").textContent = total
        ? `ms · ${total} probes · gaps = no answer · shaded = recorded outage`
        : "ms · waiting for data";
    }
  }

  function renderThroughputChart() {
    if (!throughputChart) return;
    const bucket = (status.series && status.series.speed) || {};
    const series = bucket.series || {};
    const points = series.speedtest || [];

    // Burst and sustained are different measurements, so they are separate
    // series: a burst number plotted as if it were sustained would flatter a
    // link that throttles.
    const tier = (name, want) => points.filter((p) => (p.tier || "sustained") === name
      && p[want] != null).map((p) => ({ x: p.ts, y: p[want] }));
    const built = (label, data, color, dash, fill, hidden, manual = false) => ({
      label, data, borderColor: color,
      backgroundColor: manual ? PALETTE[3] : fill,
      borderWidth: manual ? 0 : 2.2,
      borderDash: dash,
      pointRadius: manual ? 5 : 0,
      pointStyle: manual ? "rectRot" : "circle",
      pointHoverRadius: manual ? 7 : 4,
      showLine: !manual,
      tension: 0.25, spanGaps: false, fill: !!fill, hidden: !!hidden,
    });
    const manual = (want) => points
      .filter((p) => p.trigger === "manual" && p[want] != null)
      .map((p) => ({ x: p.ts, y: p[want] }));
    throughputChart.data.datasets = [
      built("sustained down", tier("sustained", "download_mbps"), PALETTE[1], [], withAlpha(PALETTE[1], 0.13), status.hiddenLabels["sustained down"]),
      built("sustained up", tier("sustained", "upload_mbps"), PALETTE[2], [], withAlpha(PALETTE[2], 0.11), status.hiddenLabels["sustained up"]),
      built("burst down", tier("quick", "download_mbps"), PALETTE[1], [4, 3], null, status.hiddenLabels["burst down"]),
      built("burst up", tier("quick", "upload_mbps"), PALETTE[2], [4, 3], null, status.hiddenLabels["burst up"]),
      built("manual down", manual("download_mbps"), PALETTE[3], [], null, status.hiddenLabels["manual down"], true),
      built("manual up", manual("upload_mbps"), PALETTE[3], [], null, status.hiddenLabels["manual up"], true),
    ];
    throughputChart.update("none");
    const counts = {
      quick: points.filter((p) => p.tier === "quick").length,
      sustained: points.filter((p) => p.tier !== "quick").length,
    };
    $("note-throughput").textContent = points.length
      ? `Mbps · ${counts.sustained} sustained, ${counts.quick} burst in range`
      : "Mbps · no speed test in this range yet";
  }

  function renderLastTest() {
    if (!lastTestChart) return;
    const speed = (status.status && status.status.last_sustained) || {};
    const down = (speed.download_intervals || []).map((p) => ({ x: p.t, y: p.mbps }));
    const up = (speed.upload_intervals || []).map((p) => ({ x: p.t, y: p.mbps }));

    lastTestChart.data.datasets = [
      { label: "download", data: down, backgroundColor: withAlpha(PALETTE[1], 0.75),
        borderColor: PALETTE[1], borderWidth: 1, borderRadius: 3,
        hidden: !!status.hiddenLabels["last-download"] },
      { label: "upload", data: up, backgroundColor: withAlpha(PALETTE[2], 0.75),
        borderColor: PALETTE[2], borderWidth: 1, borderRadius: 3,
        hidden: !!status.hiddenLabels["last-upload"] },
    ];
    lastTestChart.update("none");

    const kindLabel = { quick: "burst (10m)", sustained: "sustained (1h)" };
    const parts = [];
    if (down.length || up.length) {
      const when = speed.ts ? ago(speed.ts) : "";
      const trigger = speed.trigger === "manual" ? "manual test" : (kindLabel[speed.tier] || "scheduled test");
      parts.push(`${trigger} ${when}`);
      if (down.length) parts.push(`${down.length}s download`);
      if (up.length) parts.push(`${up.length}s upload`);
    }
    const decay = (value, label) => {
      if (value === null || value === undefined) return null;
      if (value > -10) return `${label} held steady`;
      return `${label} fell ${Math.abs(value).toFixed(0)}%`;
    };
    const notes = [
      decay(speed.download_decay_pct, "download"),
      decay(speed.upload_decay_pct, "upload"),
    ].filter(Boolean);
    if (notes.length) parts.push(notes.join(" · "));
    if (speed.capped) parts.push("hit the byte cap");

    $("lasttest-summary").textContent = parts.length ? parts.join(" · ") : "no speed test yet";
    $("note-lasttest").textContent = down.length || up.length
      ? "per-second rate inside one test — a falling tail means your link throttles"
      : "no speed test in this range yet";
  }

  /* --------------------------------------------------------- drop strip */
  function renderStrip() {
    const el = $("strip");
    const bucket = (status.series && status.series.rounds) || {};
    const rounds = bucket.rounds || [];
    if (!rounds.length) {
      el.innerHTML = '<div class="empty">no rounds in this range yet</div>';
      $("strip-range").textContent = "—";
      return;
    }

    const latencies = rounds.map((r) => r.avg_ms).filter((v) => v != null).sort((a, b) => a - b);
    const median = latencies.length ? latencies[Math.floor(latencies.length / 2)] : null;
    const slowAbove = median ? median * 3 : Infinity;

    el.innerHTML = rounds
      .map((r) => {
        const up = Number(r.up) === 1;
        let cls = "up";
        let note = "reachable";
        if (r.hourly) {
          const down = Number(r.failed_n) || 0;
          const of = Number(r.probes) || 0;
          note = down
            ? `hourly: ${down} of ${of} rounds with no internet`
            : `hourly: all ${of} rounds reachable`;
        } else if (!up) {
          cls = "down";
          note = "NO INTERNET";
        } else if (r.avg_ms != null && r.avg_ms > slowAbove) {
          cls = "up-slow";
          note = `reachable but slow (${fmtMs(r.avg_ms)} ms)`;
        }
        const failed = r.failed ? ` · failed: ${r.failed}` : "";
        const title = `${fmtDayTime(r.ts)} — ${note}${failed}`;
        return `<span class="tick ${cls}" title="${escapeHtml(title)}"></span>`;
      })
      .join("");

    if (status.range) {
      const host = $("strip-host").getBoundingClientRect();
      const ticks = Array.from(el.querySelectorAll(".tick"));
      const indexOf = (ts) => {
        let picked = 0;
        rounds.forEach((round, index) => {
          if (round.ts <= ts) picked = index;
        });
        return picked;
      };
      const first = ticks[indexOf(status.range.since)];
      const last = ticks[indexOf(status.range.until)] || ticks[ticks.length - 1];
      if (first && last) {
        const a = first.getBoundingClientRect();
        const b = last.getBoundingClientRect();
        paintSelection(a.left, b.right);
        $("btn-clear-range").hidden = false;
      }
      void host;
    } else {
      $("strip-selection").hidden = true;
      $("btn-clear-range").hidden = true;
    }

    const step = bucket.step || 1;
    const down = rounds.filter((r) => Number(r.up) !== 1).length;
    const zoom = status.range
      ? `zoomed to ${fmtTime(status.range.since)} → ${fmtTime(status.range.until)} (${fmtDuration(status.range.until - status.range.since)}) · `
      : "";
    $("strip-range").textContent =
      zoom +
      `${fmtDayTime(rounds[0].ts)} → ${fmtDayTime(rounds[rounds.length - 1].ts)} · ` +
      `${rounds.length} of ${bucket.total} rounds${step > 1 ? ` (1 in ${step}, outages always shown)` : ""}` +
      (down ? ` · ${down} red` : "");
  }

  /* ----------------------------------------------------- timeline brush */
  let brush = null;

  /** The timestamp under a client X, using the ticks actually on screen. */
  function timestampAt(clientX) {
    const rounds = (status.series && status.series.rounds && status.series.rounds.rounds) || [];
    const ticks = Array.from($("strip").querySelectorAll(".tick"));
    if (!ticks.length || !rounds.length) return null;
    let best = null;
    let bestDistance = Infinity;
    ticks.forEach((tick, index) => {
      const round = rounds[index];
      if (!round) return;
      const box = tick.getBoundingClientRect();
      if (clientX >= box.left && clientX <= box.right) {
        best = round.ts;
        bestDistance = 0;
        return;
      }
      const distance = Math.min(Math.abs(clientX - box.left), Math.abs(clientX - box.right));
      if (distance < bestDistance) {
        bestDistance = distance;
        best = round.ts;
      }
    });
    return best;
  }

  function paintSelection(fromX, toX) {
    const host = $("strip-host").getBoundingClientRect();
    const box = $("strip-selection");
    const left = Math.max(0, Math.min(fromX, toX) - host.left);
    const right = Math.min(host.width, Math.max(fromX, toX) - host.left);
    box.hidden = false;
    box.style.left = `${left}px`;
    box.style.width = `${Math.max(2, right - left)}px`;
  }

  function clearRange() {
    status.range = null;
    status.seriesAt = 0;
    $("btn-clear-range").hidden = true;
    $("strip-selection").hidden = true;
    const query = `window=${encodeURIComponent(status.window)}`;
    $("btn-csv").href = `/api/export.csv?${query}`;
    $("btn-csv-incidents").href = `/api/incidents.csv?${query}`;
    refresh();
  }

  function applyRange(from, to) {
    if (from == null || to == null) return;
    let since = Math.min(from, to);
    let until = Math.max(from, to);
    // A one-block drag is still a useful zoom; give it a minimum width so the
    // view cannot end up empty.
    if (until - since < 5) until = since + 5;
    status.range = { since, until };
    status.seriesAt = 0;      // force a refetch for the new period
    $("btn-clear-range").hidden = false;
    const query = `since=${since}&until=${until}`;
    $("btn-csv").href = `/api/export.csv?${query}`;
    $("btn-csv-incidents").href = `/api/incidents.csv?${query}`;
    refresh();
  }

  function bindBrush() {
    const strip = $("strip");
    strip.addEventListener("mousedown", (event) => {
      if (event.button !== 0) return;
      const rounds = (status.series && status.series.rounds && status.series.rounds.rounds) || [];
      if (!rounds.length) return;
      event.preventDefault();
      brush = { fromX: event.clientX, toX: event.clientX, moved: false };
      $("strip-host").classList.add("is-brushing");
      paintSelection(brush.fromX, brush.toX);
    });

    document.addEventListener("mousemove", (event) => {
      if (!brush) return;
      brush.toX = event.clientX;
      brush.moved = brush.moved || Math.abs(brush.toX - brush.fromX) > 3;
      paintSelection(brush.fromX, brush.toX);
      const from = timestampAt(brush.fromX);
      const to = timestampAt(brush.toX);
      if (from != null && to != null) {
        const lo = Math.min(from, to);
        const hi = Math.max(from, to);
        $("strip-range").textContent =
          `${fmtTime(lo)} → ${fmtTime(hi)} · ${fmtDuration(Math.max(0, hi - lo))} selected — release to zoom`;
      }
    });

    document.addEventListener("mouseup", (event) => {
      if (!brush) return;
      const { fromX, toX, moved } = brush;
      brush = null;
      $("strip-host").classList.remove("is-brushing");
      if (!moved) {
        $("strip-selection").hidden = true;   // a plain click is not a zoom
        renderStrip();
        return;
      }
      applyRange(timestampAt(fromX), timestampAt(toX));
    });

    // Touch: same idea, first and last touch point.
    strip.addEventListener("touchstart", (event) => {
      const rounds = (status.series && status.series.rounds && status.series.rounds.rounds) || [];
      if (!rounds.length || event.touches.length !== 1) return;
      brush = { fromX: event.touches[0].clientX, toX: event.touches[0].clientX, moved: false };
      paintSelection(brush.fromX, brush.toX);
    }, { passive: true });
    strip.addEventListener("touchmove", (event) => {
      if (!brush || event.touches.length !== 1) return;
      brush.toX = event.touches[0].clientX;
      brush.moved = brush.moved || Math.abs(brush.toX - brush.fromX) > 3;
      paintSelection(brush.fromX, brush.toX);
    }, { passive: true });
    strip.addEventListener("touchend", () => {
      if (!brush) return;
      const { fromX, toX, moved } = brush;
      brush = null;
      if (moved) applyRange(timestampAt(fromX), timestampAt(toX));
      else $("strip-selection").hidden = true;
    });

    $("btn-clear-range").addEventListener("click", clearRange);
  }

  /* -------------------------------------------------- speed test progress */
  /**
   * A sustained test runs 10s per direction and moves hundreds of megabytes, so
   * it gets a live view rather than a spinner: which phase is running, how far
   * in it is, the rate so far, and the per-second shape building up.
   */
  let speedAutoClose = null;
  // Which run the open modal belongs to. Without this, a scheduled test that
  // happens to finish while you are watching your own run writes its result
  // over the modal you opened by hand.
  let speedModalTrigger = null;
  // A finished run keeps its numbers on screen for a few seconds, so the
  // "stale modal" sweep must not take it away the moment a refresh lands.
  let speedModalResult = false;

  // The modal answers a button you pressed: opening it for a run that came round
  // on the clock would cover the charts you left the page to watch. Scheduled
  // runs report themselves in the Speed tests header instead, which is visible
  // without stealing the screen.
  const SPEEDTESTS_NOTE = "every run, whatever started it";
  const speedNote = { text: "", summary: "", live: false, timer: null };

  function isManualRun(trigger) {
    return trigger === "manual";
  }

  function renderSpeedNote() {
    const note = $("note-speedtests");
    if (!note) return;
    note.textContent = speedNote.text || speedNote.summary || SPEEDTESTS_NOTE;
  }

  function setSpeedNote(text, { live = false, revertIn = 0 } = {}) {
    clearTimeout(speedNote.timer);
    speedNote.text = text || "";
    speedNote.live = live;
    speedNote.timer = null;
    renderSpeedNote();
    if (revertIn > 0) {
      speedNote.timer = setTimeout(() => {
        speedNote.text = "";
        speedNote.live = false;
        speedNote.timer = null;
        renderSpeedNote();
      }, revertIn * 1000);
    }
  }

  /** The header line the table owns; a live note outranks it while it lasts. */
  function setSpeedSummary(text) {
    speedNote.summary = text || "";
    renderSpeedNote();
  }

  /** Non-blocking stand-in for the modal, used by runs nobody asked for. */
  function renderSpeedNoteProgress(frame) {
    if (!frame) {
      setSpeedNote("");
      return;
    }
    if (!frame.phase) {
      setSpeedNote(`${frame.trigger === "manual" ? "manual" : "scheduled"} test · starting…`, {
        live: true,
      });
      return;
    }
    const expected = Number(frame.expected_s) || 0;
    const elapsed = Number(frame.elapsed_s) || 0;
    const parts = [`${frame.trigger === "manual" ? "manual" : "scheduled"} test`, frame.phase];
    if (frame.bytes) parts.push(fmtBytes(frame.bytes));
    parts.push(`${fmtMbps(frame.mbps)} Mbps`);
    if (expected) parts.push(`${Math.min(100, (elapsed / expected) * 100).toFixed(0)}%`);
    setSpeedNote(parts.join(" · "), { live: true });
  }

  /** A short "here is what that run measured" line, for runs with no modal. */
  function noteSpeedResult(sample) {
    const bits = [`${fmtMbps(sample.download_mbps)} down`];
    if (sample.upload_mbps != null) bits.push(`${fmtMbps(sample.upload_mbps)} up`);
    const who = sample.tier === "quick" ? "burst" : "scheduled test";
    setSpeedNote(`${who} finished · ${bits.join(" / ")}`, { revertIn: 12 });
  }

  function openSpeedModal(trigger) {
    clearTimeout(speedAutoClose);
    const modal = $("speed-modal");
    if (!modal) return;
    speedModalTrigger = isManualRun(trigger) ? "manual" : "scheduled";
    speedModalResult = false;
    modal.hidden = false;
    $("speed-modal-title").textContent =
      trigger === "manual" ? "Speed test — started by you" : "Speed test — scheduled";
    $("speed-modal-sub").textContent = "starting…";
    for (const phase of ["download", "upload"]) {
      $(`speed-phase-${phase}`).classList.remove("is-active", "is-done");
      $(`speed-${phase === "download" ? "down" : "up"}-rate`).textContent = "—";
      $(`speed-${phase === "download" ? "down" : "up"}-bar`).style.width = "0%";
      $(`speed-${phase === "download" ? "down" : "up"}-ticks`).innerHTML = "";
    }
    $("speed-modal-note").textContent = "";
  }

  function closeSpeedModal() {
    const modal = $("speed-modal");
    if (modal) modal.hidden = true;
    speedModalTrigger = null;
    speedModalResult = false;
  }

  function renderSpeedProgress(frame) {
    if (!frame || !frame.phase) return;
    // A manual run whose modal you hid still deserves the numbers somewhere.
    if (!isManualRun(frame.trigger) || $("speed-modal").hidden) {
      renderSpeedNoteProgress(frame);
      return;
    }
    const down = frame.phase === "download";
    const key = down ? "down" : "up";
    const expected = Number(frame.expected_s) || 10;
    const elapsed = Number(frame.elapsed_s) || 0;
    const results = frame.results || {};

    // Completed phases keep their final number and a full bar.
    for (const [name, suffix] of [["download", "down"], ["upload", "up"]]) {
      const element = $(`speed-phase-${name}`);
      if (results[`${name}_mbps`] != null && frame.phase !== name) {
        element.classList.add("is-done");
        element.classList.remove("is-active");
        $(`speed-${suffix}-rate`).textContent = `${fmtMbps(results[`${name}_mbps`])} Mbps`;
        $(`speed-${suffix}-bar`).style.width = "100%";
      }
    }

    const phase = $(`speed-phase-${frame.phase}`);
    phase.classList.add("is-active");
    phase.classList.remove("is-done");
    $(`speed-${key}-rate`).textContent = `${fmtMbps(frame.mbps)} Mbps`;
    $(`speed-${key}-bar`).style.width = `${Math.min(100, (elapsed / expected) * 100).toFixed(1)}%`;

    // The per-second shape, scaled to the best second seen so far.
    const ticks = frame.intervals || [];
    const peak = Math.max(1, ...ticks.map((t) => t.mbps));
    $(`speed-${key}-ticks`).innerHTML = ticks.map((t) =>
      `<span style="height:${Math.max(8, (t.mbps / peak) * 100)}%" title="${t.t}s: ${fmtMbps(t.mbps)} Mbps"></span>`
    ).join("");

    const phases = frame.phases || ["download", "upload"];
    const index = phases.indexOf(frame.phase) + 1;
    const moved = frame.bytes ? ` · ${fmtBytes(frame.bytes)} so far` : "";
    $("speed-modal-sub").textContent =
      `${index}/${phases.length} · ${frame.phase} for ${expected}s${moved}` + (elapsed ? ` · ${elapsed.toFixed(0)}s in` : "");
    $("speed-modal-note").textContent =
      frame.trigger === "manual"
        ? "Started by hand. Hiding this does not stop the test."
        : "Scheduled test — it will not disturb anything else.";
  }

  function showSpeedResult(sample) {
    // The modal belongs to the run that opened it, so an automatic test can
    // neither raise it nor write over one already on screen.
    const ownsModal = !$("speed-modal").hidden && speedModalTrigger === sample.trigger;
    if (!ownsModal) {
      noteSpeedResult(sample);
      return;
    }
    openSpeedModal(sample.trigger);
    $("speed-modal-title").textContent = "Speed test finished";
    for (const [name, suffix] of [["download", "down"], ["upload", "up"]]) {
      const value = sample[`${name}_mbps`];
      const element = $(`speed-phase-${name}`);
      element.classList.add("is-done");
      element.classList.remove("is-active");
      $(`speed-${suffix}-rate`).textContent = value == null ? "—" : `${fmtMbps(value)} Mbps`;
      $(`speed-${suffix}-bar`).style.width = value == null ? "0%" : "100%";
      const ticks = sample[`${name}_intervals`] || [];
      const peak = Math.max(1, ...ticks.map((t) => t.mbps));
      $(`speed-${suffix}-ticks`).innerHTML = ticks.map((t) =>
        `<span style="height:${Math.max(8, (t.mbps / peak) * 100)}%" title="${t.t}s: ${fmtMbps(t.mbps)} Mbps"></span>`
      ).join("");
    }
    const moved = (sample.download_bytes || 0) + (sample.upload_bytes || 0);
    const notes = [`${fmtBytes(moved)} in ${fmtDuration((sample.elapsed_ms || 0) / 1000)}`];
    if (sample.download_decay_pct <= -20) {
      notes.push(`download fell ${Math.abs(sample.download_decay_pct).toFixed(0)}% during the test`);
    }
    if (sample.throttled) notes.push("rate limited by the provider");
    if (sample.capped) notes.push("stopped at the byte cap");
    $("speed-modal-sub").textContent = notes.join(" · ");
    $("speed-modal-note").textContent = "Finished — this closes itself in a few seconds.";
    speedModalResult = true;
    clearTimeout(speedAutoClose);
    speedAutoClose = setTimeout(closeSpeedModal, 6000);
  }

  /* -------------------------------------------------------------- cards */
  function renderCards() {
    const summary = status.summary || {};
    const uptime = summary.uptime || {};
    const incidents = summary.incidents || {};
    const state = status.status || {};
    const round = state.last_round || {};
    const buckets = round.samples || [];
    const internet = buckets.filter((s) => s.role === "internet");

    // Uptime
    const upPct = uptime.up_pct;
    $("v-uptime").textContent = upPct === null || upPct === undefined ? "—" : num(upPct, upPct >= 99 ? 2 : 1);
    $("uptime-badge").textContent = uptime.rounds ? `${uptime.rounds} rounds` : "—";
    $("m-uptime").textContent = uptime.rounds
      ? `${uptime.rounds_down} failed round${uptime.rounds_down === 1 ? "" : "s"} · down ${num(100 - upPct, 2)}%`
      : "waiting for the first round…";
    $("card-uptime").classList.toggle("is-alarm", upPct !== null && upPct < 99);

    // Outages
    $("v-outages").textContent = incidents.count === undefined ? "—" : String(incidents.count);
    $("outage-badge").textContent = incidents.ongoing ? `${incidents.ongoing} ongoing` : "in range";
    const byKind = incidents.by_kind || {};
    $("m-outages").textContent = incidents.count
      ? `${fmtDuration(incidents.downtime_s)} lost · longest ${fmtDuration(incidents.longest_s)}` +
        (byKind.dns ? ` · ${byKind.dns.count} DNS` : "")
      : "none recorded — good news";
    $("card-outages").classList.toggle("is-alarm", (incidents.count || 0) > 0 || !!incidents.ongoing);

    // Ping / jitter (internet targets, from the live round)
    const live = internet.filter((s) => s.ok && s.probe_ms != null);
    const bestLive = live.length ? Math.min(...live.map((s) => s.probe_ms)) : null;
    $("v-ping").textContent = bestLive === null ? fmtMs((summary.probe_ms || {}).last) : fmtMs(bestLive);
    $("m-ping").textContent = summary.probe_ms
      ? `avg ${fmtMs(summary.probe_ms.avg)} · p95 ${fmtMs(summary.probe_ms.p95)} · max ${fmtMs(summary.probe_ms.max)}`
      : "—";
    $("ping-badge").textContent = round.ts ? `best of ${internet.length} · ${ago(round.ts)}` : "internet";

    $("v-jitter").textContent = fmtMs(summary.jitter_ms ? summary.jitter_ms.last : null);
    $("m-jitter").textContent = summary.jitter_ms
      ? `avg ${fmtMs(summary.jitter_ms.avg)} · peak ${fmtMs(summary.jitter_ms.max)}`
      : "—";

    // Throughput. A 429 from the speed test provider is not a network fault,
    // so it must not read as "0 Mbps".
    const speed = state.last_speed || {};
    const quick = state.last_quick || {};
    const sustained = state.last_sustained || {};
    const throttled = speed.throttled === true;
    const byTier = summary.speed || {};
    const tierLine = (tierName, value, decay, seconds) => {
      const block = byTier[tierName] || {};
      if (!block[value]) return null;
      return `avg ${fmtMbps(block[value].avg)} · peak ${fmtMbps(block[value].max)}`
        + decayNote(decay, seconds);
    };
    const latest = sustained.download_mbps != null ? sustained : quick;

    $("v-down").textContent = fmtMbps(latest.download_mbps);
    $("m-down").textContent = throttled
      ? "rate limited (HTTP 429) — the link is fine, backing off"
      : [
          tierLine("quick", "download_mbps", quick.download_decay_pct, quick.download_seconds),
          tierLine("sustained", "download_mbps", sustained.download_decay_pct, sustained.download_seconds),
        ].filter(Boolean).join(" · ") || "no speed test yet";
    $("v-up").textContent = fmtMbps(latest.upload_mbps);
    $("m-up").textContent = throttled
      ? "rate limited (HTTP 429) — the link is fine, backing off"
      : [
          tierLine("quick", "upload_mbps", quick.upload_decay_pct, quick.upload_seconds),
          tierLine("sustained", "upload_mbps", sustained.upload_decay_pct, sustained.upload_seconds),
        ].filter(Boolean).join(" · ") || "no speed test yet";
    const tierBadge = (row) => (row.tier === "quick" ? "burst" : "sustained");
    $("down-badge").textContent = throttled
      ? "rate limited"
      : latest.download_bytes ? `${tierBadge(latest)}, ${fmtBytes(latest.download_bytes)}` : "last test";
    $("up-badge").textContent = throttled
      ? "rate limited"
      : latest.upload_bytes ? `${tierBadge(latest)}, ${fmtBytes(latest.upload_bytes)}` : "last test";
    $("card-down").classList.toggle("is-alarm", throttled);

    // Facts
    $("f-rounds").textContent = state.counts ? String(state.counts.rounds) : "0";
    $("f-data").textContent = fmtBytes(state.bytes_used || 0);
    $("f-stored").textContent = state.stored ? String(state.stored.total) : "0";
    $("f-last-error").textContent = state.last_error || "none";
    $("f-last-error").style.color = state.last_error ? DANGER_INK : "";
    $("f-throttled").textContent = state.counts && state.counts.throttled
      ? `${state.counts.throttled}× rate limited`
      : "no";
    tick();
  }

  function tick() {
    const state = status.status;
    if (!state) return;
    const drift = status.statusAt ? (performance.now() - status.statusAt) / 1000 : 0;
    const adjust = (v) => (v === null || v === undefined ? null : Math.max(0, v - drift));

    const nextPing = adjust(state.next_latency_in);
    const nextQuick = adjust(state.next_quick_in);
    const nextSustained = adjust(state.next_sustained_in);
    const backoff = state.speed_backoff || 1;
    const suffix = backoff > 1 ? ` (×${backoff} backoff)` : "";
    $("f-next-ping").textContent = state.running && nextPing !== null ? `in ${Math.ceil(nextPing)}s` : "—";
    $("f-next-quick").textContent =
      state.running && nextQuick !== null ? `in ${Math.ceil(nextQuick)}s${suffix}` : "—";
    $("f-next-sustained").textContent =
      state.running && nextSustained !== null ? `in ${Math.ceil(nextSustained)}s${suffix}` : "—";

    const elapsed = (state.elapsed_s || 0) + drift;
    $("elapsed-text").textContent = fmtDuration(elapsed);
    $("remaining-label").textContent = state.duration_s
      ? `/ ${fmtDuration(Math.max(0, state.duration_s - elapsed))} left`
      : state.running ? "/ no limit" : "";

    // Live outage timer
    const active = currentOutage();
    if (active) {
      const duration = Math.max(0, Date.now() / 1000 - Number(active.started_at));
      $("ob-timer").textContent = fmtDuration(duration);
    }
  }

  /* ------------------------------------------------------- outage banner */
  function currentOutage() {
    const list = (status.status && status.status.current_incidents) || status.ongoing || [];
    if (!list.length) return null;
    return list.find((i) => i.kind === "internet") || list[0];
  }

  function renderBanner() {
    const banner = $("outage-banner");
    const active = currentOutage();
    if (!active) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    const label = active.label || "Connectivity lost";
    $("ob-title").textContent =
      active.kind === "dns" ? "DNS resolution failing" : label;
    const failed = (active.failed_targets || []).join(", ") || "all targets";
    const fast = (status.status && status.status.fast) || {};
    const precision = fast.active
      ? ` · probing every ${fast.interval}s to time the recovery`
      : "";
    const began = active.start_uncertainty_s != null
      ? ` · began within ${fmtDuration(active.start_uncertainty_s)} of ${fmtTime(active.started_at)}`
      : "";
    $("ob-sub").textContent =
      `${active.rounds || 1} failed round${(active.rounds || 1) === 1 ? "" : "s"} · no answer from: ${failed}`
      + began + precision;
    tick();
  }

  /* ------------------------------------------------------------- tables */
  function scopePill(incident) {
    const scope = incident.scope || "";
    const cls = { local: "local", isp: "isp", internet: "net", dns: "dns" }[scope] || "";
    const text = incident.kind === "dns" ? "DNS" : (incident.label || scope || "—");
    return `<span class="pill ${cls}" title="${escapeHtml(text)}">${escapeHtml(shortScope(scope, incident.kind))}</span>`;
  }

  function shortScope(scope, kind) {
    if (kind === "dns") return "DNS failing";
    return { local: "Local network", isp: "ISP / upstream", internet: "Internet" }[scope] || "Connectivity";
  }

  function renderIncidents() {
    const body = $("incidents-body");
    const rows = [...status.ongoing.filter((i) => i.kind === "internet"), ...status.incidents];
    const seen = new Set();
    const unique = rows.filter((r) => {
      if (seen.has(r.id)) return false;
      seen.add(r.id);
      return true;
    });

    if (!unique.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty">No outages recorded in this range.</td></tr>';
      $("note-incidents").textContent = "the evidence — one row per drop";
      return;
    }

    body.innerHTML = unique
      .slice(0, 200)
      .map((i) => {
        const ongoing = !!i.ongoing;
        const failed = (i.failed_targets || []).join(", ");
        const uncertainty = i.start_uncertainty_s != null
          ? `±${Number(i.start_uncertainty_s).toFixed(1)}s`
          : "—";
        return `<tr class="${ongoing ? "row-ongoing" : "row-down"}">
          <td title="${escapeHtml(fmtDayTime(i.started_at))}">${fmtTime(i.started_at)}</td>
          <td>${ongoing ? '<span class="pill bad">ONGOING</span>' : fmtTime(i.ended_at)}</td>
          <td class="num">${fmtDuration(i.duration_s)}</td>
          <td class="num" title="The drop began within this long before the recorded start">${uncertainty}</td>
          <td title="${escapeHtml(failed)}">${escapeHtml(failed || "all targets")}</td>
          <td>${scopePill(i)}</td>
          <td class="num">${i.rounds || 0}</td>
        </tr>`;
      })
      .join("");

    const stats = (status.summary && status.summary.incidents) || {};
    $("note-incidents").textContent =
      `${unique.length} in ${rangeLabel()} · ${fmtDuration(stats.downtime_s || 0)} total downtime`;
  }

  function renderSpeedTests() {
    const body = $("speedtests-body");
    if (!body) return;
    const rows = status.speedTests || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty">No speed tests yet.</td></tr>';
      setSpeedSummary("");
      return;
    }
    const kindOf = (row) => {
      if (row.trigger === "manual") {
        return '<span class="pill local" title="Started by hand from the dashboard">manual</span>';
      }
      return row.tier === "quick"
        ? '<span class="pill dns" title="Scheduled burst test">10m</span>'
        : '<span class="pill" title="Scheduled sustained test">1h</span>';
    };
    body.innerHTML = rows.map((row) => {
      const moved = (row.download_bytes || 0) + (row.upload_bytes || 0);
      const notes = [];
      if (row.throttled) notes.push("rate limited");
      if (row.capped) notes.push("capped");
      if (row.download_decay_pct <= -20) notes.push(`fell ${Math.abs(row.download_decay_pct).toFixed(0)}%`);
      const title = notes.length ? ` title="${escapeHtml(notes.join(", "))}"` : "";
      return `<tr${title}>
        <td title="${escapeHtml(fmtDayTime(row.ts))}">${fmtTime(row.ts)}</td>
        <td>${kindOf(row)}</td>
        <td class="num">${fmtMbps(row.download_mbps)}</td>
        <td class="num">${fmtMbps(row.upload_mbps)}</td>
        <td class="num">${moved ? fmtBytes(moved) : "—"}</td>
      </tr>`;
    }).join("");
    const manualCount = rows.filter((r) => r.trigger === "manual").length;
    setSpeedSummary(`last ${rows.length} · ${manualCount} manual · Mbps`);
  }

  function renderTargets() {
    const body = $("targets-body");
    const targets = (status.status && status.status.targets) || [];
    const summary = status.summary || {};
    const byTarget = summary.by_target || {};
    const round = (status.status && status.status.last_round) || {};
    const live = {};
    (round.samples || []).forEach((s) => { live[s.target] = s; });
    const checks = targets.filter((t) => t.fact_check).length;
    $("targets-note").textContent = checks
      ? `live · ${checks} corroboration hosts, probed when a drop is suspected`
      : "live";

    if (!targets.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty">no targets</td></tr>';
      return;
    }

    body.innerHTML = targets
      .map((t) => {
        const sample = live[t.name];
        const stats = byTarget[t.name];
        const total = (t.success || 0) + (t.failures || 0);
        const okPct = total ? (100 * t.success) / total : null;
        const now = sample ? (sample.ok ? `${fmtMs(sample.probe_ms)}` : '<span class="pill bad">fail</span>') : "—";
        const roleCls = {
          lan: "local", local: "local", internet: "net",
          dns: "dns", "dns-public": "dns", "dns-upstream": "dns",
        }[t.role] || "";
        const roleName = t.role === "lan" ? "router" : t.role;
        const title = t.disabled ? t.disabled_reason : t.address;
        return `<tr class="${t.disabled ? "row-disabled" : ""}" title="${escapeHtml(title || "")}">
          <td>${escapeHtml(t.name)}${t.disabled ? ' <span class="pill">stood down</span>' : ""}${
            t.fact_check ? ' <span class="pill" title="Not probed every round: used to confirm a suspected drop, on another network">fact check</span>' : ""
          }</td>
          <td><span class="pill ${roleCls}">${escapeHtml(roleName)}</span></td>
          <td class="num">${now}</td>
          <td class="num">${stats ? fmtMs(stats.avg) : "—"}</td>
          <td class="num">${okPct === null ? "—" : `${num(okPct, okPct >= 99 ? 1 : 0)}%`}</td>
        </tr>`;
      })
      .join("");

    const disabled = targets.filter((t) => t.disabled).length;
    $("targets-note").textContent = disabled
      ? `${targets.length} targets · ${disabled} stood down`
      : `${targets.length} targets`;
  }

  function renderFooter() {
    const settings = (status.config && status.config.settings) || {};
    // These read the live sampler values, not the boot-time config, so a change
    // applied from the page is reflected here immediately.
    const state = status.status || {};
    const every = (value) => (Number(value) > 0 ? `${Number(value)}s` : "off");
    $("target-line").textContent =
      `round every ${every(state.latency_interval ?? settings.latency_interval)}`
      + ` · burst ${every(state.quick_interval ?? settings.quick_interval)}`
      + ` · sustained ${every(state.sustained_interval ?? settings.sustained_interval)}`;
    $("foot-latency").textContent = settings.latency_interval || 2;
    $("foot-quick").textContent = settings.quick_interval || 0;
    $("foot-sustained").textContent = settings.sustained_interval || 0;
    const quickMb = ((settings.quick_download_bytes || 0) + (settings.quick_upload_bytes || 0)) / 1048576;
    $("foot-payload").textContent =
      `burst ${quickMb.toFixed(0)} MB, sustained ${settings.download_seconds || 0}s down / ${settings.upload_seconds || 0}s up`;
    $("foot-conn").textContent = !LIVE
      ? "polling (live stream off)"
      : status.live ? "live stream connected" : "polling (stream offline)";
  }

  const MIB = 1024 * 1024;

  /**
   * The query string for the current view. A brushed selection (drag across the
   * connection timeline) takes precedence over the preset chips, exactly like a
   * Grafana zoom.
   */
  function rangeQuery() {
    if (status.range) {
      return `since=${status.range.since}&until=${status.range.until}`;
    }
    return `window=${encodeURIComponent(status.window)}`;
  }

  /** Human label for the active view, used in captions and notes. */
  function rangeLabel() {
    if (!status.range) return status.window;
    return `${fmtTime(status.range.since)} → ${fmtTime(status.range.until)}`;
  }

  function renderSettings() {
    const settings = (status.config && status.config.settings) || {};
    const state = status.status || {};

    // Never fight the user for a field they are typing in.
    const set = (id, value) => {
      const input = $(id);
      if (!input || document.activeElement === input) return;
      input.value = value;
    };
    const check = (id, value) => {
      const input = $(id);
      if (!input || document.activeElement === input) return;
      input.checked = !!value;
    };

    set("in-latency", state.latency_interval ?? settings.latency_interval ?? 2);
    set("in-quick", state.quick_interval ?? settings.quick_interval ?? 600);
    set("in-sustained", state.sustained_interval ?? settings.sustained_interval ?? 3600);
    set("in-duration", state.duration_s ?? settings.duration ?? 0);
    set("in-down-seconds", settings.download_seconds ?? 10);
    set("in-up-seconds", settings.upload_seconds ?? 10);

    set("in-targets", settings.public_targets ?? "");
    set("in-extra", settings.extra_targets ?? "");
    set("in-dnsname", settings.dns_probe_name ?? "");
    check("in-gateway", settings.probe_gateway);
    check("in-resolver", settings.probe_resolver);
    set("in-streams", settings.streams ?? 1);
    set("in-timeout", settings.target_timeout ?? 1);
    set("in-minrounds", settings.incident_min_rounds ?? 1);
    set("in-rawwindow", Math.round(((settings.raw_window_hours ?? 168) / 24) * 10) / 10);
    set("in-factcheck", settings.fact_check_targets ?? "");
    set("in-factcheck-int", settings.fact_check_interval ?? 900);
    set("in-fast", settings.fast_interval ?? 1);
    set("in-fasttimeout", settings.fast_timeout ?? 0.5);
    set("in-fasthold", settings.fast_hold_seconds ?? 10);
    set("in-fastmax", settings.fast_max_seconds ?? 300);
    set("in-maxbytes", settings.max_test_bytes
      ? Math.round((settings.max_test_bytes / MIB) * 10) / 10
      : 0);
    set("in-down-chunk", settings.download_chunk_bytes
      ? Math.round(settings.download_chunk_bytes / MIB)
      : 64);
    set("in-up-chunk", settings.upload_chunk_bytes
      ? Math.round((settings.upload_chunk_bytes / MIB) * 10) / 10
      : 1);
    renderProjection();
  }

  /**
   * A duration-based test costs `speed x duration`, which on a fast link is a
   * lot of data. Show the projection next to the knobs rather than letting it
   * be discovered from the bill.
   */
  function renderProjection() {
    const target = $("data-projection");
    if (!target) return;
    const seconds = (id) => {
      const value = Number($(id).value);
      return Number.isFinite(value) && value > 0 ? value : 0;
    };
    const quickEvery = Number($("in-quick").value);
    const sustainedEvery = Number($("in-sustained").value);
    const down = seconds("in-down-seconds");
    const up = seconds("in-up-seconds");
    target.classList.remove("warn", "alarm");
    if ((!quickEvery && !sustainedEvery) || (!down && !up)) {
      target.textContent = "throughput tests are off — only probes run (~89 MB/day)";
      return;
    }
    const speed = (status.status && status.status.last_sustained)
      || (status.status && status.status.last_speed) || {};
    const mbps = (value) => (value && value > 0 ? value : 0);
    const downMb = (mbps(speed.download_mbps) * down) / 8;   // Mbit -> MB
    const upMb = (mbps(speed.upload_mbps) * up) / 8;
    const perTest = downMb + upMb;
    // Bursts are cheap and frequent; sustained tests dominate the cost.
    const quickMb = speed.tier === "quick" ? 0 : 7;          // 5 + 2 MiB fixed
    const perDay = (quickEvery ? (quickMb * 86400) / quickEvery : 0)
      + (sustainedEvery ? (perTest * 86400) / sustainedEvery : 0);
    const asGb = (value) => (value >= 1024 ? `${(value / 1024).toFixed(1)} GB` : `${Math.round(value)} MB`);
    if (perTest <= 0) {
      target.textContent = "≈ data per test depends on your speed — run a test to estimate";
      return;
    }
    const base = Number($("in-latency").value);
    let text = [
      `≈ ${asGb(perTest)} per sustained test at ${fmtMbps(speed.download_mbps || 0)} Mbps`,
      `≈ ${asGb(quickMb)} per burst`,
      `→ ≈ ${asGb(perDay)}/day`,
    ].join(" · ");
    if (base > 5) {
      // Detection cannot be recovered after the fact, so say so plainly.
      text += ` · ⚠ a drop shorter than ${base}s can fall between probes and never be seen`;
      target.classList.add("alarm");
    }
    target.textContent = text;
    // 50 GB/day is where a data cap starts to hurt; 200 GB/day is a real risk.
    if (perDay >= 200 * 1024) target.classList.add("alarm");
    else if (perDay >= 50 * 1024) target.classList.add("warn");
  }

  function collectSettings() {
    const numberOr = (id, fallback) => {
      const value = Number($(id).value);
      return Number.isFinite(value) ? value : fallback;
    };
    return {
      latency_interval: numberOr("in-latency", 2),
      quick_interval: numberOr("in-quick", 600),
      sustained_interval: numberOr("in-sustained", 3600),
      duration: numberOr("in-duration", 0),
      download_seconds: numberOr("in-down-seconds", 10),
      upload_seconds: numberOr("in-up-seconds", 10),
      public_targets: $("in-targets").value.trim(),
      extra_targets: $("in-extra").value.trim(),
      dns_probe_name: $("in-dnsname").value.trim(),
      probe_gateway: $("in-gateway").checked,
      probe_resolver: $("in-resolver").checked,
      streams: Math.round(numberOr("in-streams", 1)),
      target_timeout: numberOr("in-timeout", 1),
      incident_min_rounds: Math.round(numberOr("in-minrounds", 1)),
      raw_window_hours: Math.max(0, numberOr("in-rawwindow", 7)) * 24,
      fact_check_targets: $("in-factcheck").value.trim(),
      fact_check_interval: Math.max(0, numberOr("in-factcheck-int", 900)),
      fast_interval: numberOr("in-fast", 1),
      fast_timeout: numberOr("in-fasttimeout", 0.5),
      fast_hold_seconds: numberOr("in-fasthold", 10),
      fast_max_seconds: numberOr("in-fastmax", 300),
      max_test_bytes: Math.round(numberOr("in-maxbytes", 0) * MIB),
      download_chunk_bytes: Math.round(numberOr("in-down-chunk", 64) * MIB),
      upload_chunk_bytes: Math.round(numberOr("in-up-chunk", 1) * MIB),
    };
  }

  /** "· fell 34% during the test" when a test visibly throttled. */
  function decayNote(decayPct, seconds) {
    if (decayPct === null || decayPct === undefined) return "";
    // One TCP stream varies second to second, so only speak up on a real
    // fall-off rather than ordinary jitter.
    if (decayPct > -20) return "";
    return ` · fell ${Math.abs(decayPct).toFixed(0)}% over ${seconds || "?"}s`;
  }

  function renderState() {
    const state = status.status || {};
    const pill = $("state-pill");
    const active = currentOutage();
    let label = "connecting…";
    let kind = "idle";

    const fast = state.fast || {};
    if (active) {
      kind = "down";
      label = fast.active
        ? `OUTAGE · resolving at ${fast.interval}s`
        : "OUTAGE IN PROGRESS";
    } else if (state.running && fast.active) {
      kind = "running";
      label = `verifying recovery at ${fast.interval}s`;
    } else if (state.running) {
      kind = "running";
      if (state.in_flight === "speed") label = "measuring throughput…";
      else if (state.in_flight === "latency") label = "probing…";
      else label = "watching";
    } else if (state.stop_reason === "duration") {
      kind = "finished";
      label = "run finished";
    } else if (state.stop_reason) {
      kind = "paused";
      label = "paused";
    }

    pill.dataset.state = kind;
    $("state-text").textContent = label;

    const toggle = $("btn-toggle");
    toggle.textContent = state.running ? "Pause" : "Resume";
    toggle.classList.toggle("primary", !state.running);

    document.querySelectorAll(".card.is-live").forEach((el) => el.classList.remove("is-live"));
    if (state.in_flight === "latency") $("card-ping")?.classList.add("is-live");
    if (state.in_flight === "speed") $("card-down")?.classList.add("is-live");
  }

  /* ------------------------------------------------------------- refresh */
  // The cards, banner and status pill want to be current; the charts do not
  // need re-downloading the whole window every 4 seconds. This keeps a
  // dashboard left open for hours from re-fetching ~90 KB four times a minute.
  const SERIES_INTERVAL_MS = 10000;

  async function refresh() {
    if (status.refreshing) return;
    status.refreshing = true;
    try {
      const query = rangeQuery();
      const now = performance.now();
      const wantSeries = !status.series || now - (status.seriesAt || 0) > SERIES_INTERVAL_MS;

      const [state, summary, speedTests] = await Promise.all([
        getJSON("/api/status"),
        getJSON(`/api/summary?${query}`),
        getJSON("/api/samples?kind=speed&limit=20"),
      ]);
      status.speedTests = speedTests.samples || [];
      status.status = state;
      status.statusAt = now;
      status.summary = summary;

      if (wantSeries) {
        const [series, incidents] = await Promise.all([
          getJSON(`/api/series?${query}&max_points=600`),
          getJSON(`/api/incidents?${query}&limit=200`),
        ]);
        status.series = series;
        status.incidents = incidents.incidents || [];
        status.ongoing = incidents.ongoing || [];
        status.seriesAt = now;
      }

      if (state.speed_progress) {
        const progress = state.speed_progress;
        if (isManualRun(progress.trigger) && $("speed-modal").hidden) {
          openSpeedModal(progress.trigger);
        }
        renderSpeedProgress(progress);
      } else {
        if (speedNote.live) setSpeedNote("");
        if (
          !$("speed-modal").hidden &&
          !speedModalResult &&
          !$("speed-modal-note").textContent
        ) {
          closeSpeedModal();
        }
      }
      renderState();
      renderBanner();
      renderCards();
      renderStrip();
      renderLatencyChart();
      renderThroughputChart();
      renderLastTest();
      renderSpeedTests();
      renderIncidents();
      renderTargets();
      renderSettings();
      if (!status.agentLinksShown) {
        status.agentLinksShown = true;
        bindAgentPanel();
        refreshAgents();
      }
      renderFooter();
    } catch (error) {
      $("foot-conn").textContent = `connection problem: ${error.message}`;
    } finally {
      status.refreshing = false;
    }
  }

  function scheduleRefresh(delay = 600) {
    clearTimeout(status.refreshTimer);
    status.refreshTimer = setTimeout(refresh, delay);
  }

  /* ----------------------------------------------------------------- sse */
  /**
   * `?live=0` falls back to polling and never opens the event stream.
   * A permanently-open SSE connection stops a page from ever reaching
   * "network idle", which hangs headless screenshot/print tools; it is also
   * handy when embedding the dashboard somewhere that dislikes long-lived
   * connections.
   */
  function liveStreamEnabled() {
    try {
      return !(
        typeof location !== "undefined" &&
        new URLSearchParams(location.search).get("live") === "0"
      );
    } catch (error) {
      return true;
    }
  }

  const LIVE = liveStreamEnabled();

  function connectStream() {
    if (!LIVE || !window.EventSource) return;
    let source;
    try {
      source = new EventSource("/api/events");
    } catch (error) {
      return;
    }
    const quick = (delay = 400) => scheduleRefresh(delay);

    source.addEventListener("open", () => { status.live = true; renderFooter(); });
    source.addEventListener("hello", (event) => {
      status.live = true;
      try {
        const payload = JSON.parse(event.data);
        if (payload.state) {
          status.status = Object.assign({}, status.status, payload.state);
          status.statusAt = performance.now();
          renderState();
        }
      } catch (error) { /* ignore malformed frames */ }
      renderFooter();
    });
    ["round", "sample", "state", "reset", "config", "target", "outage_start", "outage_end", "outage_update"]
      .forEach((name) => source.addEventListener(name, () => quick()));

    // A sustained test takes 20s, so show it happening rather than a spinner —
    // but only unroll the modal for a run you asked for by hand.
    source.addEventListener("probing", (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.kind !== "speed") return;
        if (isManualRun(payload.trigger)) {
          // Leave the auto-close timer alone for anything else: clearing it here
          // would strand a result that is still counting down.
          clearTimeout(speedAutoClose);
          openSpeedModal(payload.trigger);
        } else {
          renderSpeedNoteProgress({ trigger: payload.trigger, tier: payload.tier });
        }
      } catch (error) { /* ignore */ }
    });
    source.addEventListener("speed_progress", (event) => {
      try {
        renderSpeedProgress(JSON.parse(event.data));
      } catch (error) { /* ignore */ }
    });
    source.addEventListener("sample", (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.sample && payload.sample.kind === "speed") showSpeedResult(payload.sample);
      } catch (error) { /* ignore */ }
    });
    source.addEventListener("error", () => {
      status.live = false;
      renderFooter();
      source.close();
      setTimeout(connectStream, 5000);
    });
  }

  /* ------------------------------------------------- remote vantage points */
  /**
   * The devices you have added, each with its own token and the links to attach it.
   *
   * Fetched from a loopback-only endpoint: these are the tokens that authorise
   * writes, so a browser on the LAN must not be able to read them. That is also
   * why the panel says so plainly when the server is bound to loopback and nothing
   * else can connect yet.
   */
  function renderDevicePanel(info) {
    const host = $("agent-links");
    if (!host) return;
    const banner = info.reachable ? "" :
      '<p class="meta dim">Bound to <code>' + escapeHtml(info.bind) + '</code>, so no other ' +
      "device can connect yet. Restart with <code>--bind 0.0.0.0</code> and these links will " +
      "work from your phone.</p>";

    if (!info.devices.length) {
      host.innerHTML = banner +
        '<p class="meta dim">No devices yet. Name one above — each device gets its own ' +
        "token, so it can only ever file measurements under its own label.</p>";
      return;
    }
    host.innerHTML = banner + info.devices.map((device) => `
      <div class="agent-link">
        <div class="agent-url">
          <strong>${escapeHtml(device.source)}</strong>
          <span class="pill ${device.probes ? "ok" : ""}">${
            device.probes ? `${device.probes} probes` : "never reported"}</span>
          ${device.platform ? `<span class="pill">${escapeHtml(device.platform)}</span>` : ""}
          <button class="btn ghost small" data-revoke="${escapeHtml(device.source)}"
                  type="button">Revoke</button>
        </div>
        ${device.links && device.links.browser_agent ? `
        <div class="agent-url"><span class="pill net">browser</span>
          <code>${escapeHtml(device.links.browser_agent)}</code></div>
        <div class="agent-url"><span class="pill">python</span>
          <code>${escapeHtml(device.links.python_agent)}</code></div>` : ""}
      </div>`).join("");
  }

  async function refreshAgents() {
    try {
      const info = await getJSON("/api/agent/info");
      status.agentInfo = info;
      renderDevicePanel(info);
    } catch (error) {
      $("agent-links").textContent =
        "not available from this address (open the dashboard locally)";
    }
  }

  async function addDevice() {
    const input = $("in-device");
    const label = (input.value || "").trim();
    if (!label) return;
    try {
      await post("/api/agent/devices", { source: label });
      input.value = "";
      await refreshAgents();
    } catch (error) { /* post() reports its own failures */ }
  }

  async function revokeDevice(source) {
    if (!confirm(`Revoke "${source}"? Its token stops working immediately.`)) return;
    try {
      await fetch(`/api/agent/devices/${encodeURIComponent(source)}`, { method: "DELETE" });
      await refreshAgents();
    } catch (error) { /* ignore */ }
  }

  function bindAgentPanel() {
    const add = $("btn-add-device");
    if (add) add.addEventListener("click", addDevice);
    const input = $("in-device");
    if (input) input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") addDevice();
    });
    const host = $("agent-links");
    if (host) host.addEventListener("click", (event) => {
      const button = event.target.closest("[data-revoke]");
      if (button) revokeDevice(button.dataset.revoke);
    });
  }

  /* ------------------------------------------------------------- control */
  // A token in the URL lets the dashboard be controlled from another device on
  // the network: mutations need it, reads do not.
  const CONTROL_TOKEN = (() => {
    try {
      return new URLSearchParams(location.search).get("token") || "";
    } catch (error) {
      return "";
    }
  })();

  async function post(url, body) {
    try {
      const options = { method: "POST" };
      if (body) {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify(body);
      }
      if (CONTROL_TOKEN) {
        options.headers = Object.assign({ "X-Agent-Token": CONTROL_TOKEN }, options.headers || {});
      }
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json().catch(() => ({}));
    } catch (error) {
      $("foot-conn").textContent = `command failed: ${error.message}`;
      return null;
    } finally {
      scheduleRefresh(250);
    }
  }

  function bindControls() {
    // Keeps the display on while the dashboard is being watched. It cannot stop
    // the machine itself from sleeping -- see scripts/keep-awake.ps1 for that.
    if (window.dropTraceKeepAwake) {
      const status = $("awake-status");
      if (status) status.hidden = false;
      // Its own element on purpose: the footer is rewritten by every refresh, so
      // a message parked there disappears a second after it is shown.
      window.dropTraceKeepAwake.attach($("btn-awake"), status);
    }
    $("btn-toggle").addEventListener("click", () => {
      const running = status.status && status.status.running;
      post(running ? "/api/control/stop" : "/api/control/start");
    });
    $("btn-ping").addEventListener("click", () => post("/api/probe?kind=latency"));
    $("btn-speed").addEventListener("click", () => {
      openSpeedModal("manual");
      post("/api/probe?kind=speed");
    });
    $("speed-modal-hide").addEventListener("click", () => {
      clearTimeout(speedAutoClose);
      closeSpeedModal();
    });

    $("btn-reset").addEventListener("click", () => {
      if (confirm("Delete every stored sample and outage record? The evidence will be lost.")) {
        post("/api/reset?confirm=yes");
      }
    });

    $("btn-apply").addEventListener("click", async () => {
      const result = await post("/api/control/config", collectSettings());
      if (!result) return;
      const applied = Object.entries(result.applied || {})
        .map(([key, value]) => `${key}=${value}`)
        .join(", ");
      let note = applied ? `applied: ${applied}` : "nothing applied";
      if (result.rejected && result.rejected.length) {
        note += ` · ignored: ${result.rejected.join(", ")}`;
      }
      $("apply-note").textContent = note;
      renderProjection();
      scheduleRefresh(300);
    });

    // Keep the projected data cost current as the knobs are edited.
    ["in-latency", "in-quick", "in-sustained", "in-down-seconds", "in-up-seconds"].forEach((id) => {
      $(id).addEventListener("input", renderProjection);
    });

    $("range-group").addEventListener("click", (event) => {
      const chip = event.target.closest(".chip");
      if (!chip) return;
      status.window = chip.dataset.window;
      status.range = null;          // a preset replaces a brushed selection
      $("strip-selection").hidden = true;
      $("btn-clear-range").hidden = true;
      document.querySelectorAll("#range-group .chip")
        .forEach((el) => el.classList.toggle("is-active", el === chip));
      const query = `window=${encodeURIComponent(status.window)}`;
      $("btn-csv").href = `/api/export.csv?${query}`;
      $("btn-csv-incidents").href = `/api/incidents.csv?${query}`;
      status.seriesAt = 0;   // a new range must refetch the series immediately
      refresh();
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
    });
  }

  /* ---------------------------------------------------------------- boot */
  async function boot() {
    bindControls();
    bindBrush();
    try {
      status.config = await getJSON("/api/config");
    } catch (error) { /* non fatal */ }
    renderFooter();
    await refresh();
    connectStream();
    setInterval(refresh, 4000);   // safety net even if SSE drops
    setInterval(tick, 500);       // smooth outage + countdown timers
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
