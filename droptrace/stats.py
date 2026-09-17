"""Consolidated statistics for the /stats page.

Pure functions over rows the store already aggregated, so the arithmetic is easy
to test and the SQL stays in one place. Everything here spans both storage tiers
because it works on :meth:`Store.hourly_latency` and :meth:`Store.hourly_rounds`,
which merge raw probes with the hourly rollups.

Deliberately no prose: the page shows numbers and labels, and the person reading
it explains them.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Iterable, Sequence

DURATION_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<2s", 0.0, 2.0),
    ("2-5s", 2.0, 5.0),
    ("5-15s", 5.0, 15.0),
    ("15-60s", 15.0, 60.0),
    (">60s", 60.0, float("inf")),
)


def _day(ts: float) -> str:
    return dt.date.fromtimestamp(ts).isoformat()


def _hour(ts: float) -> int:
    return dt.datetime.fromtimestamp(ts).hour


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 3) if whole else None


def _avg(total: float | None, n: int) -> float | None:
    return round(total / n, 3) if n and total is not None else None


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def daily(hours: Sequence[dict], rounds: Sequence[dict]) -> list[dict]:
    """One row per local day, newest last.

    Probe health is scoped to the *internet* role on purpose: averaging the
    router's 1ms and the resolver's 4ms with the internet targets' 10ms (and
    their failures) would describe nothing in particular. The router and the
    resolver have their own rows in the per-target table.
    """
    days: dict[str, dict[str, Any]] = {}

    def bucket(day: str) -> dict[str, Any]:
        return days.setdefault(day, {
            "day": day, "rounds": 0, "down_rounds": 0, "dns_rounds": 0,
            "scope_isp": 0, "scope_local": 0, "scope_internet": 0,
            "probes": 0, "ok": 0, "fail": 0, "sum_ms": 0.0, "sum_jitter": 0.0,
            "min_ms": None, "max_ms": None, "sum_loss": 0.0, "failures": 0,
        })

    for row in rounds:
        d = bucket(_day(row["hour"]))
        for key in ("rounds", "down_rounds", "dns_rounds",
                    "scope_isp", "scope_local", "scope_internet"):
            d[key] += int(row.get(key) or 0)
    for row in hours:
        if row.get("role") != "internet":
            continue
        d = bucket(_day(row["hour"]))
        d["probes"] += int(row.get("probes") or 0)
        d["ok"] += int(row.get("ok_probes") or 0)
        d["fail"] += int(row.get("fail") or 0)
        d["sum_ms"] += float(row.get("sum_ms") or 0.0)
        d["sum_jitter"] += float(row.get("sum_jitter") or 0.0)
        d["sum_loss"] += float(row.get("sum_loss") or 0.0)
        if row.get("min_ms") is not None:
            d["min_ms"] = row["min_ms"] if d["min_ms"] is None else min(d["min_ms"], row["min_ms"])
        if row.get("max_ms") is not None:
            d["max_ms"] = row["max_ms"] if d["max_ms"] is None else max(d["max_ms"], row["max_ms"])

    out = []
    for day in sorted(days):
        d = days[day]
        out.append({
            "day": d["day"],
            "rounds": d["rounds"],
            "down_rounds": d["down_rounds"],
            "dns_rounds": d["dns_rounds"],
            "uptime_pct": _pct(d["rounds"] - d["down_rounds"], d["rounds"]),
            "scope_isp": d["scope_isp"],
            "scope_local": d["scope_local"],
            "scope_internet": d["scope_internet"],
            "probes": d["probes"],
            "failed": d["fail"],
            "fail_pct": _pct(d["fail"], d["probes"]),
            "avg_ms": _avg(d["sum_ms"], d["ok"]),
            "min_ms": _round(d["min_ms"]),
            "max_ms": _round(d["max_ms"]),
            "jitter_ms": _avg(d["sum_jitter"], d["ok"]),
            "loss_pct": _avg(d["sum_loss"], d["probes"]),
        })
    return out


def targets(hours: Sequence[dict]) -> list[dict]:
    """Per-target league table for the whole window, flakiest first."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in hours:
        key = (row["target"], row["role"])
        t = rows.setdefault(key, {
            "target": row["target"], "role": row["role"], "probes": 0, "ok": 0,
            "fail": 0, "sum_ms": 0.0, "sum_jitter": 0.0, "sum_loss": 0.0,
            "min_ms": None, "max_ms": None, "worst_loss": None, "worst_hour": None,
        })
        t["probes"] += int(row.get("probes") or 0)
        t["ok"] += int(row.get("ok_probes") or 0)
        t["fail"] += int(row.get("fail") or 0)
        t["sum_ms"] += float(row.get("sum_ms") or 0.0)
        t["sum_jitter"] += float(row.get("sum_jitter") or 0.0)
        t["sum_loss"] += float(row.get("sum_loss") or 0.0)
        # Loss is a rate, so it has to be read per hour as well as on average: a
        # 2% weekly average hides the hour that lost a third of its handshakes.
        hour_loss = _avg(row.get("sum_loss"), row.get("probes"))
        if hour_loss is not None and (t["worst_loss"] is None or hour_loss > t["worst_loss"]):
            t["worst_loss"] = hour_loss
            t["worst_hour"] = row.get("hour")
        if row.get("min_ms") is not None:
            t["min_ms"] = row["min_ms"] if t["min_ms"] is None else min(t["min_ms"], row["min_ms"])
        if row.get("max_ms") is not None:
            t["max_ms"] = row["max_ms"] if t["max_ms"] is None else max(t["max_ms"], row["max_ms"])

    out = []
    for t in rows.values():
        out.append({
            "target": t["target"],
            "role": t["role"],
            "probes": t["probes"],
            "failed": t["fail"],
            "fail_pct": _pct(t["fail"], t["probes"]),
            "avg_ms": _avg(t["sum_ms"], t["ok"]),
            "min_ms": _round(t["min_ms"]),
            "max_ms": _round(t["max_ms"]),
            "jitter_ms": _avg(t["sum_jitter"], t["ok"]),
            "loss_pct": _avg(t["sum_loss"], t["probes"]),
            "worst_loss_pct": _round(t["worst_loss"], 2),
            "worst_hour": t["worst_hour"],
        })
    out.sort(key=lambda r: (-(r["fail_pct"] or 0), r["target"]))
    return out


def incidents(rows: Sequence[dict]) -> dict:
    """Counts, downtime and the two patterns worth seeing: size and time of day."""
    durations = [float(r.get("duration_s") or 0.0) for r in rows]
    histogram = []
    for label, low, high in DURATION_BUCKETS:
        histogram.append({
            "label": label,
            "count": sum(1 for d in durations if low <= d < high),
        })
    scope: dict[str, int] = {}
    by_hour = [0] * 24
    by_weekday = [0] * 7
    for row in rows:
        scope[row.get("scope") or "?"] = scope.get(row.get("scope") or "?", 0) + 1
        by_hour[_hour(row["started_at"])] += 1
        by_weekday[dt.datetime.fromtimestamp(row["started_at"]).weekday()] += 1
    return {
        "count": len(rows),
        "downtime_s": round(sum(durations), 1),
        "longest_s": round(max(durations), 1) if durations else 0.0,
        "mean_s": round(statistics.fmean(durations), 1) if durations else 0.0,
        "histogram": histogram,
        "by_scope": scope,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
    }


def throughput(rows: Sequence[dict]) -> dict:
    """Per tier, because burst and sustained answer different questions."""
    out: dict[str, dict] = {}
    for tier in ("quick", "sustained"):
        down = [float(r["download_mbps"]) for r in rows
                if r.get("tier") == tier and r.get("download_mbps") is not None]
        up = [float(r["upload_mbps"]) for r in rows
              if r.get("tier") == tier and r.get("upload_mbps") is not None]
        bytes_down = sum(int(r.get("download_bytes") or 0) for r in rows if r.get("tier") == tier)
        bytes_up = sum(int(r.get("upload_bytes") or 0) for r in rows if r.get("tier") == tier)
        out[tier] = {
            "count": len(down) or len(up),
            "manual": sum(1 for r in rows if r.get("tier") == tier and r.get("trigger") == "manual"),
            "down_avg": _round(statistics.fmean(down), 1) if down else None,
            "down_best": _round(max(down), 1) if down else None,
            "down_worst": _round(min(down), 1) if down else None,
            "up_avg": _round(statistics.fmean(up), 1) if up else None,
            "up_best": _round(max(up), 1) if up else None,
            "up_worst": _round(min(up), 1) if up else None,
            "bytes_down": bytes_down,
            "bytes_up": bytes_up,
        }
    return out


def failures(rows: Sequence[dict]) -> dict:
    """How many, from where, what the stack said, and when."""
    errors: dict[str, int] = {}
    by_target: dict[str, int] = {}
    by_hour = [0] * 24
    for row in rows:
        errors[row.get("error") or "(no error)"] = errors.get(row.get("error") or "(no error)", 0) + 1
        by_target[row["target"]] = by_target.get(row["target"], 0) + 1
        by_hour[_hour(row["ts"])] += 1
    top = sorted(errors.items(), key=lambda kv: -kv[1])[:8]
    return {
        "total": len(rows),
        "top_errors": [{"error": e, "count": n} for e, n in top],
        "by_target": sorted(
            ({"target": t, "count": n} for t, n in by_target.items()),
            key=lambda r: -r["count"],
        ),
        "by_hour": by_hour,
    }


def devices(payload: dict, since: float, until: float) -> dict:
    """Every vantage point that reported, and when each one was failing.

    The point of the feature: if the phone on Wi-Fi and this machine on Ethernet
    went dark in the same hours, the fault is not the computer. If only one of
    them did, it is that device's own path -- and the answer needs no arguing.
    """
    known = payload.get("agents", {})
    rows = []
    for row in payload.get("sources", []):
        probes = int(row.get("probes") or 0)
        failed = int(row.get("failed") or 0)
        meta = known.get(row["source"]) or {}
        rows.append({
            "source": row["source"],
            "platform": meta.get("platform") or "",
            "agent": meta.get("agent") or "",
            "probes": probes,
            "failed": failed,
            "fail_pct": _pct(failed, probes),
            "internet_probes": int(row.get("internet_probes") or 0),
            "internet_failed": int(row.get("internet_failed") or 0),
            "first_ts": row.get("first_ts"),
            "last_ts": row.get("last_ts"),
            # Age in seconds at the moment of the response. The page adds the time
            # since it loaded, so the on/off flag stays right between refreshes and
            # does not depend on the browser's clock agreeing with the server's.
            "last_seen_ago_s": (
                round(max(0.0, until - float(row["last_ts"])), 1)
                if row.get("last_ts") else None
            ),
        })
    rows.sort(key=lambda r: r["source"])

    # Per hour, per device: how many probes were seen and how many failed. The
    # probe count is what separates "clean" from "not there": a device that was
    # off for an hour and a device that was up and failing zero probes both have
    # zero failures, and reading the second as the first is how a comparison
    # quietly turns into a lie ("my phone was fine" -- it was not reporting).
    hours: dict[int, dict[str, dict[str, int]]] = {}
    for row in payload.get("source_hours", []):
        bucket = hours.setdefault(int(row["hour"]), {})
        bucket[row["source"]] = {
            "probes": int(row.get("probes") or 0),
            "failed": int(row.get("failed") or 0),
        }
    grid = [{"hour": h, "by_source": hours[h]} for h in sorted(hours)]
    online = {
        row["source"]: sum(
            1 for bucket in hours.values()
            if (bucket.get(row["source"]) or {}).get("probes")
        )
        for row in rows
    }
    for row in rows:
        row["hours_online"] = online.get(row["source"], 0)
    return {
        "devices": rows,
        "hours": grid,
        "hours_total": len(grid),
        "sources": [r["source"] for r in rows],
    }


def build(payload: dict, since: float, until: float) -> dict:
    """Assemble the whole page payload."""
    hours = payload["hours"]
    rounds = payload["rounds"]
    internet = [h for h in hours if h.get("role") == "internet"]
    days = daily(hours, rounds)
    inc = incidents(payload["incidents"])
    fail = failures(payload["failures"])
    speed = throughput(payload["speed"])

    total_rounds = sum(int(r.get("rounds") or 0) for r in rounds)
    down_rounds = sum(int(r.get("down_rounds") or 0) for r in rounds)
    probes = sum(int(h.get("probes") or 0) for h in internet)
    ok = sum(int(h.get("ok_probes") or 0) for h in internet)
    sum_ms = sum(float(h.get("sum_ms") or 0.0) for h in internet)
    sum_jitter = sum(float(h.get("sum_jitter") or 0.0) for h in internet)

    for day_row in days:
        same_day = [r for r in payload["incidents"]
                    if _day(r["started_at"]) == day_row["day"]]
        durations = [float(r.get("duration_s") or 0.0) for r in same_day]
        day_row["incidents"] = len(same_day)
        day_row["incident_s"] = round(sum(durations), 1)
        day_row["longest_s"] = round(max(durations), 1) if durations else 0.0

    return {
        "window": {"since": since, "until": until},
        "coverage": payload["coverage"],
        "totals": {
            "rounds": total_rounds,
            "down_rounds": down_rounds,
            "uptime_pct": _pct(total_rounds - down_rounds, total_rounds),
            "dns_rounds": sum(int(r.get("dns_rounds") or 0) for r in rounds),
            "probes": probes,
            "failed_probes": probes - ok,
            "fail_pct": _pct(probes - ok, probes),
            "avg_ms": _avg(sum_ms, ok),
            "jitter_ms": _avg(sum_jitter, ok),
            "incidents": inc["count"],
            "downtime_s": inc["downtime_s"],
            "longest_s": inc["longest_s"],
            "mean_s": inc["mean_s"],
            "data_down": speed["sustained"]["bytes_down"] + speed["quick"]["bytes_down"],
            "data_up": speed["sustained"]["bytes_up"] + speed["quick"]["bytes_up"],
        },
        "daily": days,
        "targets": targets(hours),
        "incidents": inc,
        "throughput": speed,
        "failures": fail,
        "devices": devices(payload, since, until),
        "paths": paths(payload.get("traces") or []),
        "loss": loss(payload.get("bursts") or []),
    }


def loss(rows: Sequence[dict]) -> dict:
    """The counted bursts: how much of the traffic actually arrived at a drop.

    A round probe can only say 0% or 100%; this is the measured percentage in
    between, which is the number to send when the complaint is "it stops for a
    few seconds" and the answer would otherwise be "but it is up".
    """
    if not rows:
        return {"count": 0, "rows": [], "worst_pct": None, "worst_ts": None,
                "avg_pct": None, "handshakes": 0, "lost": 0}

    ordered = sorted(rows, key=lambda r: float(r.get("ts") or 0), reverse=True)
    handshakes = sum(int(r.get("sent") or 0) for r in ordered)
    lost = sum(max(0, int(r.get("sent") or 0) - int(r.get("recv") or 0)) for r in ordered)
    worst = max(ordered, key=lambda r: float(r.get("loss_pct") or 0.0))
    return {
        "count": len(ordered),
        "rows": [
            {
                "ts": row.get("ts"),
                "target": row.get("target") or "",
                "sent": int(row.get("sent") or 0),
                "recv": int(row.get("recv") or 0),
                "lost": max(0, int(row.get("sent") or 0) - int(row.get("recv") or 0)),
                "loss_pct": row.get("loss_pct"),
                "min_ms": row.get("tcp_min_ms"),
                "avg_ms": row.get("tcp_avg_ms"),
                "max_ms": row.get("tcp_max_ms"),
                "error": row.get("error"),
            }
            for row in ordered[:60]
        ],
        "worst_pct": worst.get("loss_pct"),
        "worst_ts": worst.get("ts"),
        "worst_target": worst.get("target") or "",
        "avg_pct": _avg(sum(float(r.get("loss_pct") or 0.0) for r in ordered), len(ordered)),
        "handshakes": handshakes,
        "lost": lost,
    }


def paths(rows: list[dict]) -> dict:
    """The path at the drop, next to the path when the line was healthy.

    One trace on its own is hard to read -- a hop list through private
    addresses only means something compared with the same list from a working
    moment. So this pairs the newest drop trace with the newest baseline trace
    (falling back to the newest of anything if there is no baseline yet) and
    counts where each of them stopped.
    """
    if not rows:
        return {"drop": None, "baseline": None, "count": 0}

    def brief(row: dict) -> dict:
        return {
            "id": row.get("id"),
            "ts": row.get("ts"),
            "trigger": row.get("trigger"),
            "host": row.get("host"),
            "tracer": row.get("tracer"),
            "reached": bool(row.get("reached")),
            "hops": int(row.get("hops") or 0),
            "answered": int(row.get("answered") or 0),
            "max_hops": int(row.get("max_hops") or 0),
            "last_hop": row.get("last_hop") or "",
            "duration_ms": row.get("duration_ms"),
            "error": row.get("error"),
            "incident_id": row.get("incident_id"),
            "hop_list": [
                {
                    "ttl": int(hop.get("ttl") or 0),
                    "host": hop.get("host") or "",
                    "rtt_ms": hop.get("rtt_ms"),
                    "note": hop.get("note") or "",
                }
                for hop in (row.get("hop_list") or [])
            ],
        }

    newest_first = sorted(rows, key=lambda r: float(r.get("ts") or 0), reverse=True)
    drops = [r for r in newest_first if r.get("trigger") == "drop"]
    baselines = [r for r in newest_first if r.get("trigger") == "baseline"]
    # With no drop traced yet, the newest trace stands in as the one to look at
    # -- but never as both halves of the pair, which would read as "the drop
    # looks exactly like the healthy path".
    drop = drops[0] if drops else None
    baseline = baselines[0] if baselines else (newest_first[0] if drop is None else None)
    return {
        "drop": brief(drop) if drop else None,
        "baseline": brief(baseline) if baseline else None,
        "count": len(rows),
    }
