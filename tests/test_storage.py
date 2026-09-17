"""Storage layer: samples, per-target series, aggregates, incidents, exports."""

from __future__ import annotations

import time

import pytest

from droptrace.config import SAMPLE_FIELDS
from droptrace.storage import Store


def latency_sample(
    ts: float,
    probe: float,
    target: str = "cloudflare",
    role: str = "internet",
    ok: bool = True,
    round_id: int | None = None,
    error: str | None = None,
) -> dict:
    return {
        "ts": ts,
        "kind": "latency",
        "target": target,
        "role": role,
        "round_id": round_id if round_id is not None else int(ts * 1000),
        "ok": ok,
        "error": error,
        "probe_ms": probe if ok else None,
        "tcp_avg_ms": probe if ok else None,
        "jitter_ms": 0.5,
        "loss_pct": 0.0 if ok else 100.0,
        "sent": 2,
        "recv": 2 if ok else 0,
    }


def speed_sample(ts: float, down: float, up: float, ok: bool = True,
                 tier: str = "sustained") -> dict:
    return {
        "ts": ts,
        "kind": "speed",
        "tier": tier,
        "target": "speedtest",
        "role": "internet",
        "ok": ok,
        "download_mbps": down,
        "upload_mbps": up,
        "download_bytes": 10 * 1024 * 1024,
        "upload_bytes": 5 * 1024 * 1024,
        "download_ttfb_ms": 30.0,
        "streams": 1,
    }


@pytest.fixture
async def store(tmp_path):
    instance = Store(tmp_path / "test.db")
    await instance.connect()
    yield instance
    await instance.close()


async def test_add_and_count(store):
    await store.add(latency_sample(1000.0, 12.0))
    await store.add(speed_sample(1001.0, 90.0, 40.0))
    assert await store.count() == 2
    assert await store.count("latency") == 1
    assert await store.count("speed") == 1
    assert await store.first_ts() == 1000.0


async def test_add_many_writes_a_round(store):
    samples = [
        latency_sample(2000.0, 5.0, target="resolver", role="local"),
        latency_sample(2000.0, 12.0, target="cloudflare"),
        latency_sample(2000.0, 19.0, target="google"),
    ]
    assert await store.add_many(samples) == 3
    assert await store.count("latency") == 3
    assert await store.add_many([]) == 0


async def test_add_ignores_unknown_fields(store):
    saved = await store.add({"ts": 5.0, "kind": "latency", "bogus": "nope", "probe_ms": 3.0})
    assert saved["id"] >= 1
    rows = await store.recent(limit=1)
    assert "bogus" not in rows[0]
    assert rows[0]["probe_ms"] == 3.0
    assert set(SAMPLE_FIELDS).issubset(rows[0].keys())


async def test_recent_is_newest_first_and_limited(store):
    for i in range(6):
        await store.add(latency_sample(1000.0 + i, 10.0 + i, round_id=i))
    rows = await store.recent("latency", limit=3)
    assert len(rows) == 3
    assert [r["ts"] for r in rows] == [1005.0, 1004.0, 1003.0]


async def test_recent_rounds_groups_by_round(store):
    for round_id in (1, 2, 3):
        await store.add_many([
            latency_sample(1000.0 + round_id, 12.0, "cloudflare", round_id=round_id),
            latency_sample(1000.0 + round_id, 5.0, "resolver", "local", round_id=round_id),
        ])
    rounds = await store.recent_rounds(2)
    assert [r["round_id"] for r in rounds] == [3, 2]
    assert {s["target"] for s in rounds[0]["samples"]} == {"cloudflare", "resolver"}


async def test_series_is_per_target_and_downsamples(store):
    now = time.time()
    for i in range(100):
        await store.add(latency_sample(now - 100 + i, 10.0 + i, "cloudflare", round_id=i))
        await store.add(latency_sample(now - 100 + i, 5.0, "resolver", "local", round_id=i))

    series = await store.series("latency", since=now - 1000, max_points=10)
    assert sorted(series["targets"]) == ["cloudflare", "resolver"]
    assert series["total"] == 200
    cloudflare = series["series"]["cloudflare"]
    assert 10 <= len(cloudflare) <= 11
    assert cloudflare[-1]["ts"] == pytest.approx(now - 1)
    # Downsampling is applied per target, not across the mixed stream, and each
    # point carries only the columns the charts need.
    resolver = series["series"]["resolver"]
    assert 10 <= len(resolver) <= 11
    assert set(resolver[0]) == {"ts", "probe_ms", "ok"}
    # The resolver series holds the resolver's values, not cloudflare's.
    assert resolver[0]["probe_ms"] == pytest.approx(5.0)
    assert cloudflare[0]["probe_ms"] != resolver[0]["probe_ms"]


async def test_series_without_downsampling_returns_everything(store):
    for i in range(5):
        await store.add(latency_sample(2000.0 + i, 10.0, round_id=i))
    series = await store.series("latency", since=0.0, max_points=600)
    assert series["step"] == 1
    assert len(series["series"]["cloudflare"]) == 5

    assert (await store.series("latency", since=time.time() + 100))["total"] == 0


async def test_round_states_marks_outages_and_keeps_them(store):
    now = time.time()
    for round_id in range(1, 11):
        down = round_id in (7, 8)
        await store.add_many([
            latency_sample(now + round_id, 12.0, "cloudflare", ok=not down, round_id=round_id,
                           error="timeout" if down else None),
            latency_sample(now + round_id, 5.0, "resolver", "local", round_id=round_id),
        ])
    states = await store.round_states(since=0.0, max_points=4)
    assert states["total"] == 10
    assert states["step"] > 1
    downs = [r for r in states["rounds"] if not r["up"]]
    # Downsampling must never hide an outage round.
    assert [r["round_id"] for r in downs] == [7, 8]
    assert "cloudflare" in downs[0]["failed"]
    assert downs[0]["avg_ms"] is None  # no internet target answered


async def test_summary_uses_probe_ms_for_internet_targets(store):
    now = time.time()
    await store.add_many([
        latency_sample(now, 10.0, "cloudflare", round_id=1),
        latency_sample(now, 5.0, "resolver", "local", round_id=1),   # excluded: local
        latency_sample(now + 1, 20.0, "cloudflare", round_id=2),
        latency_sample(now + 2, 30.0, "cloudflare", round_id=3),
        latency_sample(now + 3, 40.0, "cloudflare", round_id=4),
    ])
    await store.add(speed_sample(now + 10, 100.0, 50.0))
    await store.add(speed_sample(now + 11, 200.0, 25.0, ok=False))

    summary = await store.summary(since=0.0)
    assert summary["counts"]["latency"] == 5
    assert summary["counts"]["speed"] == 2
    # Only the four internet-role samples feed the headline latency numbers.
    assert summary["probe_ms"]["n"] == 4
    assert summary["probe_ms"]["avg"] == pytest.approx(25.0)
    assert summary["probe_ms"]["min"] == pytest.approx(10.0)
    assert summary["probe_ms"]["max"] == pytest.approx(40.0)
    # Burst and sustained are never averaged together.
    assert summary["speed"]["sustained"]["download_mbps"]["avg"] == pytest.approx(150.0)
    assert summary["speed"]["sustained"]["upload_mbps"]["avg"] == pytest.approx(37.5)
    assert summary["speed"]["quick"]["download_mbps"] is None
    assert summary["download_mbps"]["avg"] == pytest.approx(150.0)
    assert summary["bytes"]["downloaded"] == 20 * 1024 * 1024
    assert summary["bytes"]["uploaded"] == 10 * 1024 * 1024


async def test_summary_keeps_the_tiers_apart(store):
    """A burst number must never be mixed into the sustained average."""
    now = time.time()
    await store.add(speed_sample(now, 480.0, 150.0, tier="quick"))
    await store.add(speed_sample(now + 1, 300.0, 100.0, tier="sustained"))
    await store.add(speed_sample(now + 2, 320.0, 110.0, tier="sustained"))

    summary = await store.summary(0.0)
    quick = summary["speed"]["quick"]
    sustained = summary["speed"]["sustained"]
    assert quick["download_mbps"]["avg"] == pytest.approx(480.0)
    assert quick["download_mbps"]["n"] == 1
    assert sustained["download_mbps"]["avg"] == pytest.approx(310.0)
    assert sustained["download_mbps"]["n"] == 2
    # The headline keys follow the sustained tier.
    assert summary["download_mbps"]["avg"] == pytest.approx(310.0)


async def test_summary_by_target(store):
    now = time.time()
    for i in range(4):
        await store.add(latency_sample(now + i, 10.0 + i, "cloudflare", round_id=i))
        await store.add(latency_sample(now + i, 2.0, "resolver", "local", round_id=i))
    summary = await store.summary(0.0)
    assert set(summary["by_target"]) == {"cloudflare", "resolver"}
    assert summary["by_target"]["cloudflare"]["avg"] == pytest.approx(11.5)
    assert summary["by_target"]["resolver"]["avg"] == pytest.approx(2.0)


async def test_uptime_counts_rounds_not_samples(store):
    now = time.time()
    # 4 rounds: 3 fine, 1 with no internet answer at all.
    for round_id in range(1, 5):
        down = round_id == 4
        await store.add_many([
            latency_sample(now + round_id, 12.0, "cloudflare", ok=not down, round_id=round_id),
            latency_sample(now + round_id, 19.0, "google", ok=not down, round_id=round_id),
            latency_sample(now + round_id, 5.0, "resolver", "local", round_id=round_id),
        ])
    uptime = await store.uptime(0.0)
    # Two internet targets per round must not double-count.
    assert uptime["rounds"] == 4
    assert uptime["rounds_up"] == 3
    assert uptime["rounds_down"] == 1
    assert uptime["up_pct"] == pytest.approx(75.0)


async def test_summary_on_empty_store_is_safe(store):
    summary = await store.summary(since=0.0)
    assert summary["counts"]["total"] == 0
    assert summary["probe_ms"] is None
    assert summary["uptime"]["up_pct"] is None
    assert summary["incidents"]["count"] == 0


# --------------------------------------------------------------- incidents
async def test_incident_lifecycle(store):
    start = time.time() - 6
    incident_id = await store.open_incident(
        kind="internet", scope="isp", started_at=start,
        targets_total=5, targets_failed=2,
        failed_targets=["cloudflare", "google"], detail={"errors": {"cloudflare": "timeout"}},
    )
    assert incident_id > 0

    ongoing = await store.current_incidents()
    assert len(ongoing) == 1
    assert ongoing[0]["kind"] == "internet"
    assert ongoing[0]["ongoing"] is True
    assert ongoing[0]["failed_targets"] == "cloudflare,google"

    await store.extend_incident(incident_id, rounds=3, targets_failed=2,
                               failed_targets=["cloudflare", "google"])
    closed = await store.close_incident(incident_id, start + 6)
    assert closed["ongoing"] is False
    assert closed["duration_s"] == pytest.approx(6.0)
    assert closed["rounds"] == 3
    assert await store.current_incidents() == []


async def test_incident_stats(store):
    now = time.time()
    first = await store.open_incident("internet", "isp", now - 100, 5, 2, ["cloudflare"])
    await store.close_incident(first, now - 95)     # 5s
    second = await store.open_incident("internet", "local", now - 50, 5, 4, ["cloudflare", "google"])
    await store.close_incident(second, now - 30)    # 20s
    third = await store.open_incident("dns", "dns", now - 10, 1, 1, ["dns"])

    stats = await store.incident_stats(0.0)
    assert stats["count"] == 2          # the open one is not counted as completed
    assert stats["ongoing"] == 1
    assert stats["downtime_s"] == pytest.approx(25.0)
    assert stats["longest_s"] == pytest.approx(20.0)
    assert stats["average_s"] == pytest.approx(12.5)
    assert stats["by_kind"]["internet"]["count"] == 2
    assert stats["by_kind"]["dns"]["count"] == 0
    assert stats["by_scope"] == {"isp": 1, "local": 1}


async def test_close_dangling_flags_interrupted(store):
    await store.open_incident("internet", "isp", time.time() - 120, 5, 2, ["cloudflare"])
    assert await store.close_dangling(time.time()) == 1
    rows = await store.incidents(0.0)
    assert rows[0]["interrupted"] is True
    assert rows[0]["ongoing"] is False
    assert await store.current_incidents() == []


# ------------------------------------------------------------------ export
async def test_csv_export(store):
    await store.add(latency_sample(3000.0, 12.5, round_id=1))
    await store.add(speed_sample(3001.0, 88.25, 41.5))
    body = await store.to_csv()
    lines = body.strip().splitlines()
    assert lines[0].startswith("id,ts,kind,target,role,ok")
    assert len(lines) == 3
    assert "cloudflare" in lines[1]
    assert "speedtest" in lines[2]
    assert "12.5" in lines[1]
    assert "88.25" in lines[2]


async def test_csv_export_filters(store):
    await store.add(latency_sample(3000.0, 12.5, round_id=1))
    await store.add(speed_sample(3001.0, 88.25, 41.5))
    assert len((await store.to_csv(kind="speed")).strip().splitlines()) == 2
    assert len((await store.to_csv(since=time.time() + 100)).strip().splitlines()) == 1


async def test_incidents_csv_has_readable_times(store):
    incident_id = await store.open_incident("internet", "isp", 1000.0, 5, 2, ["cloudflare"])
    await store.close_incident(incident_id, 1006.0)
    body = await store.incidents_to_csv(0.0)
    lines = body.strip().splitlines()
    assert lines[0].endswith("started_iso,ended_iso")
    assert "6" in lines[1]
    assert "isp" in lines[1]


async def test_series_payload_only_carries_what_the_charts_need(store):
    """The open dashboard refetches this every few seconds, so it must be lean.

    Selecting the full row (28 mostly-NULL columns) made the response ~380 KB.
    """
    now = time.time()
    await store.add_many([
        latency_sample(now + i, 10.0, "cloudflare", round_id=i) for i in range(3)
    ])
    await store.add(speed_sample(now, 90.0, 40.0))

    latency_series = await store.series("latency", 0.0, 600)
    point = latency_series["series"]["cloudflare"][0]
    assert set(point) == {"ts", "probe_ms", "ok"}
    assert point["ok"] is True

    speed_series = await store.series("speed", 0.0, 600)
    speed_point = speed_series["series"]["speedtest"][0]
    assert set(speed_point) == {"ts", "tier", "trigger", "download_mbps", "upload_mbps", "ok"}
    assert speed_point["tier"] == "sustained"


async def test_series_still_downsamples_with_trimmed_columns(store):
    now = time.time()
    for i in range(60):
        await store.add(latency_sample(now + i, 10.0 + i, round_id=i))
    series = await store.series("latency", 0.0, max_points=10)
    points = series["series"]["cloudflare"]
    assert 10 <= len(points) <= 11
    assert set(points[0]) == {"ts", "probe_ms", "ok"}
    assert points[-1]["ts"] == pytest.approx(now + 59)


async def test_checkpoint_folds_the_wal(store):
    for i in range(50):
        await store.add(latency_sample(2000.0 + i, 10.0, round_id=i))
    # Must not raise, and must leave the data readable.
    await store.checkpoint("TRUNCATE")
    assert await store.count() == 50


async def test_clear_and_prune(store):
    now = time.time()
    await store.add(latency_sample(now - 90 * 86400, 10.0, round_id=1))
    await store.add(latency_sample(now, 11.0, round_id=2))
    await store.add(speed_sample(now, 50.0, 20.0))
    await store.open_incident("internet", "isp", now - 90 * 86400, 5, 2, ["cloudflare"])

    assert await store.prune(retention_days=30) == 1
    assert await store.count() == 2
    assert await store.incidents(0.0) == []   # the old incident was pruned too
    assert await store.prune(retention_days=0) == 0

    assert await store.clear("speed") == 1
    assert await store.count("speed") == 0
    assert await store.clear() == 1
    assert await store.count() == 0


async def test_migration_adds_missing_columns(tmp_path):
    """A database created by an older version gains the new columns."""
    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " kind TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 1)"
        )
        await db.execute("INSERT INTO samples (ts, kind, ok) VALUES (1.0, 'latency', 1)")
        await db.commit()

    store = Store(path)
    await store.connect()
    try:
        await store.add(latency_sample(time.time(), 12.0, round_id=1))
        assert await store.count() == 2
        rows = await store.recent(limit=1)
        assert rows[0]["target"] == "cloudflare"
        assert rows[0]["probe_ms"] == 12.0
    finally:
        await store.close()


async def test_traces_round_trip_and_are_never_pruned(store):
    """A hop list is the one row that names equipment, so it is kept forever.

    It also survives the raw-window folding that eventually deletes its own
    samples: the incident it belongs to is still readable years later.
    """
    base = time.time() - 86400 * 90           # well past any retention setting
    hops = [
        {"ttl": 1, "host": "192.168.1.1", "rtt_ms": 1.0, "note": ""},
        {"ttl": 2, "host": "100.64.0.1", "rtt_ms": 12.5, "note": ""},
        {"ttl": 3, "host": "", "rtt_ms": None, "note": "no reply"},
    ]
    trace_id = await store.add_trace(
        {
            "ts": base, "trigger": "drop", "host": "1.1.1.1", "tracer": "tracepath",
            "reached": False, "hops": 3, "answered": 2, "max_hops": 20,
            "last_hop": "100.64.0.1", "duration_ms": 3200.0, "error": None,
            "hop_list": hops,
        },
        incident_id=7,
    )
    assert trace_id > 0
    await store.add_trace(
        {"ts": base - 60, "trigger": "baseline", "host": "1.1.1.1", "tracer": "tracepath",
         "reached": True, "hops": 18, "answered": 17, "max_hops": 20,
         "last_hop": "1.1.1.1", "hop_list": []}
    )

    rows = await store.traces(base - 3600)
    assert [r["trigger"] for r in rows] == ["drop", "baseline"], "newest first"
    drop = rows[0]
    assert drop["reached"] is False and drop["hop_list"] == hops
    assert drop["incident_id"] == 7 and drop["last_hop"] == "100.64.0.1"
    assert [r["trigger"] for r in await store.traces(trigger="baseline")] == ["baseline"]
    assert (await store.latest_trace("drop"))["id"] == trace_id
    assert await store.count_traces() == 2

    # Pruning by age must not touch them; a full reset must.
    await store.prune(retention_days=1)
    assert await store.count_traces() == 2
    await store.clear()
    assert await store.count_traces() == 0


async def test_last_seen_answers_for_the_recent_past_only(store):
    """The on/off indicator asks one question: did it report in the last minute.

    A device quiet for longer than the bound must come back missing rather than
    with an old timestamp dressed up as current, and the search is capped so a
    growing table does not make a five-second poll expensive.
    """
    now = time.time()
    await store.add_many([
        latency_sample(now - 20, 10.0, "cloudflare", round_id=1),
        latency_sample(now - 20, 3.0, "resolver", "local", round_id=1),
        speed_sample(now - 5, 90.0, 40.0),
    ])
    fresh = await store.last_seen()
    assert set(fresh) == {"local"}, "a speed sample is not a device reporting in"
    assert fresh["local"] == pytest.approx(now - 20, abs=2)

    await store.add_many([{**latency_sample(now - 7200, 10.0, "cloudflare", round_id=2),
                           "source": "phone-wifi"}])
    assert "phone-wifi" not in await store.last_seen(), "two hours ago is not 'on'"

    await store.add_many([{**latency_sample(now - 4, 10.0, "cloudflare", round_id=3),
                           "source": "phone-wifi"}])
    assert (await store.last_seen())["phone-wifi"] == pytest.approx(now - 4, abs=2)


async def test_until_bounds_every_query(store):
    """A brushed selection has to filter the cards and incidents, not just the
    charts, so `until` is honoured by every aggregate."""
    base = time.time() - 3600
    for i in range(60):
        ts = base + i * 60           # one minute apart across the whole hour
        await store.add_many([
            latency_sample(ts, 10.0 + i, "cloudflare", round_id=i),
            latency_sample(ts, 5.0, "resolver", "local", round_id=i),
        ])
    await store.add(speed_sample(base + 1200, 100.0, 50.0))
    incident_id = await store.open_incident("internet", "isp", base + 900, 3, 1, ["cloudflare"])
    await store.close_incident(incident_id, base + 930)
    incident_late = await store.open_incident("internet", "isp", base + 2400, 3, 1, ["cloudflare"])
    await store.close_incident(incident_late, base + 2430)

    end = base + 1800                # first half only
    full = await store.summary(0.0)
    half = await store.summary(0.0, until=end)
    assert half["counts"]["latency"] < full["counts"]["latency"]
    assert half["counts"]["speed"] == 1
    assert half["probe_ms"]["n"] < full["probe_ms"]["n"]
    assert half["incidents"]["count"] == 1
    assert full["incidents"]["count"] == 2

    assert (await store.series("latency", 0.0, 600, until=end))["total"] < \
           (await store.series("latency", 0.0, 600))["total"]
    assert (await store.round_states(0.0, 600, until=end))["total"] < \
           (await store.round_states(0.0, 600))["total"]
    assert (await store.uptime(0.0, end))["rounds"] < (await store.uptime(0.0))["rounds"]
    assert len(await store.incidents(0.0, until=end)) == 1
    assert await store.count("latency", 0.0, end) < await store.count("latency", 0.0)
    assert len((await store.to_csv(0.0, until=end)).strip().splitlines()) < \
           len((await store.to_csv(0.0)).strip().splitlines())
    assert len((await store.incidents_to_csv(0.0, until=end)).strip().splitlines()) == 2


async def test_until_excludes_an_incident_that_started_after_it(store):
    now = time.time()
    early = await store.open_incident("internet", "isp", now - 600, 3, 1, ["cloudflare"])
    await store.close_incident(early, now - 570)
    late = await store.open_incident("internet", "isp", now - 60, 3, 1, ["cloudflare"])
    await store.close_incident(late, now - 30)

    window = await store.incidents(now - 900, until=now - 300)
    assert [row["id"] for row in window] == [early]


async def test_bursts_are_stored_apart_from_the_round_probes(store):
    """A burst measures a drop; it is not another probe that happened to fail.

    Keeping it in its own sample kind means the latency averages, the jitter and
    the failure counts of the round probes cannot be skewed by twenty handshakes
    fired on purpose.
    """
    now = time.time()
    await store.add_many([
        latency_sample(now - 5, 12.0, "cloudflare", round_id=1),
        {
            "ts": now - 4, "kind": "burst", "target": "cloudflare", "role": "internet",
            "round_id": 1, "ok": False, "error": "14/20 handshakes lost: timeout on 443",
            "sent": 20, "recv": 6, "loss_pct": 70.0, "refused": 0,
            "tcp_min_ms": 9.0, "tcp_avg_ms": 11.0, "tcp_max_ms": 14.0,
        },
    ])

    rows = await store.bursts(now - 60)
    assert len(rows) == 1
    assert rows[0]["loss_pct"] == 70.0 and rows[0]["sent"] == 20 and rows[0]["recv"] == 6
    assert rows[0]["tcp_avg_ms"] == 11.0
    assert [r["target"] for r in await store.bursts(now - 60, limit=1)] == ["cloudflare"]

    # The round probe's own aggregates see only the round probe.
    assert await store.count("latency") == 1
    failures = await store.failures(now - 60)
    assert [f["target"] for f in failures] == [], "a burst is not a failed probe"
