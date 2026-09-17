"""Two-tier storage: raw probes for a week, hourly statistics after that.

The rules that matter and are pinned here:

* nothing is deleted before its hour has been summarised;
* a partial hour is never summarised (it would be replaced by a different
  fraction of itself on the next pass);
* combining the two tiers gives the same answer as if nothing had been rolled
  up -- that is what the sums in the rollup table are for;
* failures survive verbatim, however old.
"""

from __future__ import annotations

import time

import pytest

from droptrace.sampler import Sampler
from droptrace.config import Settings
from droptrace.storage import Store

HOUR = 3600.0


@pytest.fixture
async def store(tmp_path):
    instance = Store(tmp_path / "rollup.db")
    await instance.connect()
    yield instance
    await instance.close()


def probe(ts, target, role, ok, round_id, ms=None, error=None, jitter=0.5, loss=0.0):
    return {
        "ts": ts, "kind": "latency", "target": target, "role": role, "round_id": round_id,
        "ok": ok, "probe_ms": ms, "error": error, "jitter_ms": jitter, "loss_pct": loss,
        "sent": 2, "recv": 2 if ok else 0,
    }


def speed(ts, down, up):
    return {
        "ts": ts, "kind": "speed", "tier": "sustained", "trigger": "scheduled",
        "target": "speedtest", "role": "internet", "ok": True,
        "download_mbps": down, "upload_mbps": up, "download_bytes": 1000, "upload_bytes": 500,
        "download_intervals": [{"t": 0, "mbps": down}], "upload_intervals": [{"t": 0, "mbps": up}],
    }


async def seed_three_hours(store):
    """Three complete hours well outside the raw window, plus the current one.

    The hours are far enough back that no plausible "now" inside the hour can
    pull them into the raw window -- the window is floored to whole hours, so a
    naive (3, 2, 1) seed makes the newest hour's fate depend on the minute the
    test happens to run.
    """
    now = time.time()
    this_hour = int(now // HOUR) * HOUR
    rows = []
    for hours_ago in (5, 4, 3):
        base = this_hour - hours_ago * HOUR
        for step in range(3):                       # three rounds per hour
            ts = base + step * 60
            down = hours_ago == 4 and step == 1     # one round with no internet
            rows += [
                probe(ts, "gateway", "lan", True, int(ts), ms=1.0),
                probe(ts, "cloudflare", "internet", not down, int(ts), ms=None if down else 10.0,
                      error="timeout on 443" if down else None),
                probe(ts, "google", "internet", not down, int(ts), ms=None if down else 20.0,
                      error="timeout on 443" if down else None),
                probe(ts, "dns", "dns", True, int(ts), ms=4.0),
            ]
    # One round in the current (incomplete) hour.
    ts = this_hour + 30
    rows += [
        probe(ts, "gateway", "lan", True, int(ts), ms=1.0),
        probe(ts, "cloudflare", "internet", True, int(ts), ms=11.0),
        probe(ts, "google", "internet", True, int(ts), ms=21.0),
        probe(ts, "dns", "dns", True, int(ts), ms=4.0),
    ]
    await store.add_many(rows)
    await store.add(speed(this_hour - 4 * HOUR + 120, 100.0, 50.0))
    return {"this_hour": this_hour, "rows": len(rows), "samples": rows}


async def test_rollout_summarises_complete_hours_and_keeps_failures(store):
    seeded = await seed_three_hours(store)
    before = await store.count("latency")
    assert before == 40  # 3 hours x 3 rounds x 4 probes, plus the current round

    result = await store.rollout(raw_window_hours=2.0)   # raw window: 2 hours
    assert result["rolled"] == 36, "only the current hour should stay raw"
    assert await store.count("latency") == 4, "the current hour's 4 probes stay"
    # The window is floored to a whole hour, so the boundary is 2 hours back.
    assert await store.raw_cutoff() == seeded["this_hour"] - 2 * HOUR

    # Per-target hourly statistics, with the sums that make averaging correct.
    rows = await store.db.execute_fetchall(
        "SELECT hour, target, role, probes, ok, fail, sum_ms, min_ms, max_ms "
        "FROM rollups ORDER BY hour, target"
    )
    by_key = {(r[0], r[1]): r for r in rows}
    assert len(by_key) == 12, "3 hours x 4 targets (router, 2 internet, dns)"
    cloudflare = [r for (h, t), r in by_key.items() if t == "cloudflare"]
    assert all(r[3] == 3 for r in cloudflare), "3 probes per hour per target"
    assert [r[5] for r in cloudflare] == [0, 1, 0], "the middle hour lost one round"
    # 3 probes at 10ms, except the middle hour which lost one to the outage.
    assert [r[6] for r in cloudflare] == [30.0, 20.0, 30.0], "sum_ms over the successes"
    assert all(r[7] == 10.0 and r[8] == 10.0 for r in cloudflare)

    # Rounds, with the same attribution the live sampler uses.
    rounds = await store.db.execute_fetchall(
        "SELECT hour, rounds, down_rounds, scope_isp FROM round_rollups ORDER BY hour"
    )
    assert [r[1] for r in rounds] == [3, 3, 3]
    assert [r[2] for r in rounds] == [0, 1, 0]
    assert [r[3] for r in rounds] == [0, 1, 0], "the router answered, so it is an ISP-side drop"

    # Failures verbatim, and old speed tests lose only their per-second shape.
    failures = await store.failures()
    assert len(failures) == 2
    assert {f["target"] for f in failures} == {"cloudflare", "google"}
    assert all(f["error"] == "timeout on 443" for f in failures)
    trimmed = await store.db.execute_fetchall(
        "SELECT download_intervals, upload_intervals, download_mbps FROM samples WHERE kind = 'speed'"
    )
    assert trimmed[0][0] is None and trimmed[0][1] is None
    assert trimmed[0][2] == 100.0, "the numbers stay, only the per-second shape goes"


async def test_failures_span_both_tiers(store):
    """The failures query must not go blind to whichever tier is younger."""
    seeded = await seed_three_hours(store)
    # One failure inside the raw window, one old enough to be folded.
    await store.add_many([
        probe(seeded["this_hour"] + 60, "cloudflare", "internet", False, 9000,
              error="timeout on 443"),
        probe(seeded["this_hour"] - 5 * HOUR + 30, "cloudflare", "internet", False, 9001,
              error="timeout on 443"),
    ])
    before = await _failure_rows(store)
    assert before == 4, "2 from the seeded outage + 1 old + 1 recent"

    await store.rollout(raw_window_hours=2.0)
    assert await _failure_rows(store) == 4, "a fold hid or duplicated failures"


async def _failure_rows(store) -> int:
    rows = await store.failures(limit=9999)
    return len(rows)


async def test_rollout_is_not_run_twice_over_the_same_rows(store):
    await seed_three_hours(store)
    await store.rollout(raw_window_hours=2.0)
    first = await store.db.execute_fetchall("SELECT hour, target, probes FROM rollups ORDER BY hour, target")
    await store.rollout(raw_window_hours=2.0)
    second = await store.db.execute_fetchall("SELECT hour, target, probes FROM rollups ORDER BY hour, target")
    assert first == second, "a second pass doubled the counts"
    assert len(await store.failures()) == 2, "failures were copied twice"


async def test_a_partial_hour_is_left_alone(store):
    """The current hour must stay raw until it is complete."""
    now = time.time()
    this_hour = int(now // HOUR) * HOUR
    await store.add_many([
        probe(this_hour + 10, "cloudflare", "internet", True, 1, ms=10.0),
        probe(this_hour + 20, "cloudflare", "internet", True, 2, ms=12.0),
    ])
    result = await store.rollout(raw_window_hours=0.01)   # 36 seconds: hour is excluded
    assert result["rolled"] == 0
    assert await store.count("latency") == 2


async def test_summary_combines_both_tiers_the_way_raw_would(store):
    seeded = await seed_three_hours(store)
    since = seeded["this_hour"] - 5 * HOUR

    raw_only = await store.summary(since)
    # 3 hours x 3 rounds x 2 internet targets, minus the 2 in the failed round,
    # plus 2 in the current round; 272 ms of latency over those 18 probes.
    assert raw_only["probe_ms"]["n"] == 18
    assert raw_only["probe_ms"]["avg"] == 15.111
    assert raw_only["probe_ms"]["p95"] is not None

    await store.rollout(raw_window_hours=2.0)
    mixed = await store.summary(since)
    assert mixed["probe_ms"]["n"] == raw_only["probe_ms"]["n"], "count changed across the fold"
    assert mixed["probe_ms"]["avg"] == raw_only["probe_ms"]["avg"], "weighted average drifted"
    assert mixed["probe_ms"]["min"] == raw_only["probe_ms"]["min"]
    assert mixed["probe_ms"]["max"] == raw_only["probe_ms"]["max"]
    assert mixed["probe_ms"]["p95"] is None, "p95 cannot be rebuilt, so it is not guessed"
    assert mixed["uptime"]["rounds"] == raw_only["uptime"]["rounds"] == 10
    assert mixed["uptime"]["rounds_down"] == raw_only["uptime"]["rounds_down"] == 1
    assert mixed["uptime"]["up_pct"] == raw_only["uptime"]["up_pct"] == 90.0


async def test_series_switches_to_hourly_and_says_so(store):
    seeded = await seed_three_hours(store)
    since = seeded["this_hour"] - 5 * HOUR
    raw = await store.series("latency", since)
    assert raw.get("resolution") != "hourly"
    assert any(p["ok"] == 0 for p in raw["series"]["cloudflare"]), "failures appear in the raw series"

    await store.rollout(raw_window_hours=2.0)
    hourly = await store.series("latency", since)
    assert hourly["resolution"] == "hourly"
    point = hourly["series"]["cloudflare"][0]
    assert point["hourly"] is True
    assert point["probe_ms"] == 10.0, "the hourly point is the average"
    assert (point["min_ms"], point["max_ms"]) == (10.0, 10.0)
    assert point["probes"] == 3 and point["failed"] == 0

    # A window inside the raw tier still gets individual probes.
    recent = await store.series("latency", seeded["this_hour"])
    assert recent.get("resolution") != "hourly"


async def test_the_strip_keeps_showing_old_outages_as_hourly_blocks(store):
    seeded = await seed_three_hours(store)
    since = seeded["this_hour"] - 5 * HOUR
    await store.rollout(raw_window_hours=2.0)
    strip = await store.round_states(since)
    assert strip["resolution"] == "hourly"
    hourly = [r for r in strip["rounds"] if r.get("hourly")]
    assert len(hourly) == 3
    assert [r["failed_n"] for r in hourly] == [0, 1, 0]
    assert [r["up"] for r in hourly] == [1, 0, 1]
    assert all(r["probes"] == 3 for r in hourly)


async def test_maintenance_summarises_before_it_prunes(tmp_path):
    """Pruning must never be able to delete a probe that was not summarised."""
    store = Store(tmp_path / "order.db")
    await store.connect()
    try:
        old = time.time() - 3 * HOUR
        await store.add_many([
            probe(old, "cloudflare", "internet", True, 1, ms=10.0),
            probe(old, "gateway", "lan", True, 1, ms=1.0),
        ])
        sampler = Sampler(
            Settings(
                db_path=tmp_path / "order.db",
                latency_interval=0.05, quick_interval=0.0, sustained_interval=0.0,
                raw_window_hours=0.5,
                retention_days=0.001,      # ~86 seconds: would delete the rows outright
            ),
            store,
        )
        sampler._last_maintenance = 0.0
        await sampler._maintain()
        assert await store.count("latency") == 0, "the old probes should be gone from raw"
        rows = await store.db.execute_fetchall("SELECT probes, ok FROM rollups")
        assert rows and rows[0][0] == 1 and rows[0][1] == 1, "but they must remain as statistics"
    finally:
        await store.close()
