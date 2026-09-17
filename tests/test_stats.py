"""Consolidated statistics: the aggregation the /stats page renders.

The arithmetic is pinned here rather than in the browser, and the two-tier part
is checked explicitly: a window that spans the raw cutoff must give the same
answers as one that does not.
"""

from __future__ import annotations

import time

import pytest

from droptrace import stats as S
from droptrace.sampler import Sampler
from droptrace.config import Settings
from droptrace.storage import Store

HOUR = 3600.0


@pytest.fixture
async def store(tmp_path):
    instance = Store(tmp_path / "stats.db")
    await instance.connect()
    yield instance
    await instance.close()


def probe(ts, target, role, ok, round_id, ms=None, error=None):
    return {
        "ts": ts, "kind": "latency", "target": target, "role": role, "round_id": round_id,
        "ok": ok, "probe_ms": ms, "error": error, "jitter_ms": 1.0 if ok else None,
        "loss_pct": 0.0 if ok else 100.0, "sent": 2, "recv": 2 if ok else 0,
    }


def speed(ts, tier, down, up):
    return {
        "ts": ts, "kind": "speed", "tier": tier, "trigger": "scheduled",
        "target": "speedtest", "role": "internet", "ok": True,
        "download_mbps": down, "upload_mbps": up,
        "download_bytes": 1_000_000, "upload_bytes": 500_000,
    }


def round_at(ts, rid, internet_ok=True, lan_ok=True, dns_ok=True):
    return [
        probe(ts, "gateway", "lan", lan_ok, rid, ms=1.0 if lan_ok else None,
              error=None if lan_ok else "timeout on 443"),
        probe(ts, "cloudflare", "internet", internet_ok, rid,
              ms=10.0 if internet_ok else None,
              error=None if internet_ok else "timeout on 443"),
        probe(ts, "dns", "dns", dns_ok, rid, ms=4.0 if dns_ok else None,
              error=None if dns_ok else "DNS timeout"),
    ]


async def test_daily_rollup_arithmetic(store):
    """One good hour, one hour with a single down round, one DNS-only round."""
    now = time.time()
    base = int(now // HOUR) * HOUR
    rows = []
    for i in range(3):                                    # hour A: healthy
        rows += round_at(base + i, 100 + i)
    for i in range(3):                                    # hour B: one round down
        rows += round_at(base + 60 + i, 200 + i, internet_ok=(i != 1))
    rows += round_at(base + 120, 300, internet_ok=True, dns_ok=False)
    await store.add_many(rows)

    payload = await store.stats(base, time.time() + 1)
    data = S.build(payload, base, time.time() + 1)

    assert data["totals"]["rounds"] == 7
    assert data["totals"]["down_rounds"] == 1
    assert data["totals"]["dns_rounds"] == 1
    assert data["totals"]["uptime_pct"] == round(100 * 6 / 7, 3)
    assert data["totals"]["probes"] == 7, "the internet role only: router and DNS excluded"
    assert data["totals"]["failed_probes"] == 1, "one failed cloudflare probe"

    # Internet latencies: 6 clean rounds x 10ms; the failed one is excluded.
    assert data["totals"]["avg_ms"] == 10.0
    cloudflare = next(t for t in data["targets"] if t["target"] == "cloudflare")
    assert cloudflare["probes"] == 7 and cloudflare["failed"] == 1
    assert cloudflare["fail_pct"] == round(100 / 7, 3)
    assert cloudflare["max_ms"] == 10.0

    # The attribution split, and the failure text that explains it.
    assert data["incidents"]["by_scope"] == {}
    assert data["failures"]["total"] == 2, "every role is still in the failure list"
    assert {e["error"] for e in data["failures"]["top_errors"]} == {"timeout on 443", "DNS timeout"}


async def test_daily_picks_up_rolled_up_hours(store):
    """A window older than the raw tier must give the same numbers as raw did."""
    now = time.time()
    base = int(now // HOUR) * HOUR - 5 * HOUR
    rows = []
    for hour, broken in enumerate((False, False, True, False, False)):
        for i in range(4):
            rows += round_at(base + hour * HOUR + i * 10, 1000 + hour * 10 + i,
                             internet_ok=not (broken and i < 2))
    await store.add_many(rows)
    window = (base, time.time() + 1)

    before = S.build(await store.stats(*window), *window)
    await store.rollout(raw_window_hours=2.0)
    after = S.build(await store.stats(*window), *window)

    assert after["totals"]["rounds"] == before["totals"]["rounds"]
    assert after["totals"]["down_rounds"] == before["totals"]["down_rounds"] == 2
    assert after["totals"]["uptime_pct"] == before["totals"]["uptime_pct"]
    assert after["totals"]["probes"] == before["totals"]["probes"]
    assert after["totals"]["avg_ms"] == before["totals"]["avg_ms"]
    assert after["targets"] == before["targets"]
    # And the per-day rows survive the fold.
    assert [d["down_rounds"] for d in after["daily"]] == [d["down_rounds"] for d in before["daily"]]


async def test_incident_profile_and_throughput(store):
    now = time.time()
    await store.add_many(round_at(now - 600, 1, internet_ok=False))
    await store.open_incident(
        kind="internet", scope="isp", started_at=now - 600, targets_total=3,
        targets_failed=1, failed_targets=["cloudflare"], rounds=1,
    )
    incident_id = (await store.current_incidents())[0]["id"]
    await store.close_incident(incident_id, now - 570, end_uncertainty_s=1.0)
    await store.add(speed(now - 500, "sustained", 400.0, 120.0))
    await store.add(speed(now - 400, "sustained", 200.0, 80.0))
    await store.add(speed(now - 300, "quick", 450.0, 90.0))

    payload = await store.stats(now - 3600, now)
    data = S.build(payload, now - 3600, now)

    inc = data["incidents"]
    assert inc["count"] == 1 and inc["longest_s"] == 30.0 and inc["mean_s"] == 30.0
    assert inc["by_scope"] == {"isp": 1}
    assert sum(h["count"] for h in inc["histogram"]) == 1
    assert inc["histogram"][3]["label"] == "15-60s" and inc["histogram"][3]["count"] == 1
    assert sum(inc["by_hour"]) == 1
    # Each day carries its own drops, downtime and longest drop.
    day = data["daily"][-1]
    assert day["incidents"] == 1 and day["incident_s"] == 30.0 and day["longest_s"] == 30.0

    sustained = data["throughput"]["sustained"]
    assert sustained["count"] == 2
    assert sustained["down_best"] == 400.0 and sustained["down_worst"] == 200.0
    assert sustained["down_avg"] == 300.0
    assert data["throughput"]["quick"]["count"] == 1
    assert data["totals"]["data_down"] == 3_000_000


async def test_stats_survive_a_rollup_pass_in_maintenance(tmp_path):
    """The page must not go blind after the hourly fold prunes raw probes."""
    store = Store(tmp_path / "maint.db")
    await store.connect()
    try:
        old = time.time() - 6 * HOUR
        await store.add_many(round_at(old, 42, internet_ok=False))
        await store.add_many(round_at(old + 60, 43))
        sampler = Sampler(
            Settings(db_path=tmp_path / "maint.db", latency_interval=0.05,
                     quick_interval=0.0, sustained_interval=0.0,
                     raw_window_hours=2.0, retention_days=30.0),
            store,
        )
        sampler._last_maintenance = 0.0
        await sampler._maintain()
        assert await store.count("latency") == 0, "raw rows were folded"

        window = (old - 60, time.time())
        data = S.build(await store.stats(*window), *window)
        assert data["totals"]["rounds"] == 2
        assert data["totals"]["down_rounds"] == 1
        assert data["totals"]["probes"] == 2, "two rounds, one internet target each"
        assert data["incidents"]["count"] == 0  # no incident row was written here
        assert data["failures"]["total"] == 1, "the one failed cloudflare probe"
    finally:
        await store.close()


def test_device_rows_carry_how_long_ago_they_reported():
    """The on/off flag is the server's arithmetic, not the browser's clock."""
    now = 1_700_000_000.0
    payload = {
        "sources": [
            {"source": "local", "probes": 10, "failed": 0,
             "first_ts": now - 600, "last_ts": now - 5,
             "internet_probes": 10, "internet_failed": 0},
            {"source": "phone-wifi", "probes": 10, "failed": 0,
             "first_ts": now - 600, "last_ts": now - 300,
             "internet_probes": 10, "internet_failed": 0},
        ],
        "source_hours": [],
        "agents": {},
    }
    devices = S.devices(payload, now - 3600, now)["devices"]
    by_name = {d["source"]: d for d in devices}
    assert by_name["local"]["last_seen_ago_s"] == 5.0
    assert by_name["phone-wifi"]["last_seen_ago_s"] == 300.0
    # A device that has never reported has no age rather than a wrong one.
    empty = S.devices({"sources": [{"source": "new", "probes": 0, "failed": 0,
                                    "first_ts": None, "last_ts": None,
                                    "internet_probes": 0, "internet_failed": 0}],
                       "source_hours": [], "agents": {}}, now - 3600, now)["devices"]
    assert empty[0]["last_seen_ago_s"] is None


def test_paths_pair_the_drop_with_the_healthy_baseline():
    """One hop list means nothing alone; the pair is the evidence.

    "The path died after hop 2 at 100.64.0.1" is only readable next to "it
    normally reaches 1.1.1.1 in 4 hops".
    """
    rows = [
        {"id": 1, "ts": 100.0, "trigger": "baseline", "host": "1.1.1.1",
         "tracer": "tracepath", "reached": 1, "hops": 4, "answered": 4, "max_hops": 20,
         "last_hop": "1.1.1.1", "duration_ms": 900.0, "hop_list": []},
        {"id": 2, "ts": 200.0, "trigger": "drop", "host": "1.1.1.1",
         "tracer": "tracepath", "reached": 0, "hops": 3, "answered": 2, "max_hops": 20,
         "last_hop": "100.64.0.1", "duration_ms": 3100.0, "incident_id": 7,
         "hop_list": [{"ttl": 2, "host": "100.64.0.1", "rtt_ms": 12.5, "note": ""}]},
    ]
    paths = S.paths(rows)
    assert paths["count"] == 2
    assert paths["drop"]["id"] == 2 and paths["drop"]["last_hop"] == "100.64.0.1"
    assert paths["drop"]["reached"] is False and paths["drop"]["incident_id"] == 7
    assert paths["drop"]["hop_list"] == [
        {"ttl": 2, "host": "100.64.0.1", "rtt_ms": 12.5, "note": ""}
    ]
    assert paths["baseline"]["id"] == 1 and paths["baseline"]["reached"] is True

    # No baseline yet: the drop is still shown, alone.
    only = S.paths([rows[1]])
    assert only["drop"]["id"] == 2 and only["baseline"] is None
    # And with no drop yet, the baseline is not quietly used as both halves of
    # the pair -- that would read as "the drop looked exactly like the healthy
    # path", which is the opposite of the truth.
    healthy = S.paths([rows[0]])
    assert healthy["drop"] is None and healthy["baseline"]["id"] == 1
    assert S.paths([]) == {"drop": None, "baseline": None, "count": 0}


def test_loss_summary_counts_handshakes_not_bursts():
    """The panel's headline is a measured rate, so it adds up the handshakes."""
    rows = [
        {"ts": 300.0, "target": "cloudflare", "sent": 20, "recv": 9, "loss_pct": 55.0,
         "tcp_min_ms": 0.9, "tcp_avg_ms": 12.4, "tcp_max_ms": 300.0, "error": None},
        {"ts": 100.0, "target": "cloudflare", "sent": 20, "recv": 20, "loss_pct": 0.0,
         "tcp_min_ms": 0.9, "tcp_avg_ms": 1.2, "tcp_max_ms": 4.0, "error": None},
    ]
    out = S.loss(rows)
    assert out["count"] == 2 and out["handshakes"] == 40 and out["lost"] == 11
    assert out["worst_pct"] == 55.0 and out["worst_ts"] == 300.0
    assert out["worst_target"] == "cloudflare"
    assert out["avg_pct"] == 27.5
    assert [r["ts"] for r in out["rows"]] == [300.0, 100.0], "newest first"
    assert out["rows"][0]["lost"] == 11
    assert S.loss([])["count"] == 0 and S.loss([])["worst_pct"] is None


def test_the_per_target_table_carries_the_worst_hour_of_loss():
    """An average hides the hour that lost a third of its handshakes."""
    hours = [
        {"hour": 1000, "target": "cloudflare", "role": "internet", "probes": 100,
         "ok_probes": 100, "fail": 0, "sum_ms": 1000.0, "min_ms": 5.0, "max_ms": 20.0,
         "sum_jitter": 10.0, "sum_loss": 200.0},          # 2% lost
        {"hour": 4600, "target": "cloudflare", "role": "internet", "probes": 100,
         "ok_probes": 60, "fail": 40, "sum_ms": 600.0, "min_ms": 5.0, "max_ms": 900.0,
         "sum_jitter": 10.0, "sum_loss": 4000.0},         # 40% lost
    ]
    row = S.targets(hours)[0]
    assert row["loss_pct"] == 21.0           # the window average
    assert row["worst_loss_pct"] == 40.0     # the hour that matters
    assert row["worst_hour"] == 4600


def test_device_hours_say_who_was_there_and_who_was_not():
    """A device that was off and a device that lost nothing are not the same.

    Both have zero failures in the hour. Reading the first as the second turns
    the whole comparison into a lie -- "only my machine had errors, the phone
    was fine" -- when the phone was actually not reporting at all. So every hour
    carries the probe count, not just the failure count.
    """
    now = 1_700_000_000.0
    hour = 3600
    payload = {
        "sources": [
            {"source": "local", "probes": 200, "failed": 30, "first_ts": now - hour,
             "last_ts": now - 5, "internet_probes": 200, "internet_failed": 30},
            {"source": "phone-wifi", "probes": 40, "failed": 0, "first_ts": now - hour,
             "last_ts": now - hour, "internet_probes": 40, "internet_failed": 0},
        ],
        "source_hours": [
            {"source": "local", "hour": 1000, "probes": 120, "failed": 12},
            {"source": "local", "hour": 4600, "probes": 80, "failed": 18},
            {"source": "phone-wifi", "hour": 1000, "probes": 40, "failed": 0},
        ],
        "agents": {},
    }
    devices = S.devices(payload, now - 7200, now)
    assert devices["hours_total"] == 2
    assert [h["hour"] for h in devices["hours"]] == [1000, 4600]
    # The phone reported in the first hour only, so the second hour is "absent",
    # not "clean".
    assert devices["hours"][0]["by_source"]["phone-wifi"] == {"probes": 40, "failed": 0}
    assert "phone-wifi" not in devices["hours"][1]["by_source"]
    by_name = {row["source"]: row for row in devices["devices"]}
    assert by_name["phone-wifi"]["hours_online"] == 1
    assert by_name["local"]["hours_online"] == 2
    # A device that only appears in one hour is not in the grid's other hours at
    # all, which is exactly what the page hatches.
    assert by_name["phone-wifi"]["failed"] == 0 and by_name["phone-wifi"]["hours_online"] < devices["hours_total"]
