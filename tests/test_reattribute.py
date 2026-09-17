"""Re-deriving the scope of incidents already on record."""

from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

from droptrace.storage import Store

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reattribute_incidents.py"


def load_script():
    spec = importlib.util.spec_from_file_location("reattribute_incidents", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repair = load_script()


def latency(ts, target, role, ok, round_id, error=None):
    return {
        "ts": ts, "kind": "latency", "target": target, "role": role, "round_id": round_id,
        "ok": ok, "error": None if ok else (error or "timeout on 443"),
        "probe_ms": 9.0 if ok else None, "sent": 2, "recv": 2 if ok else 0,
        "loss_pct": 0.0 if ok else 100.0,
    }


@pytest.fixture
async def db(tmp_path):
    """A database with one incident per attribution case."""
    store = Store(tmp_path / "repair.db")
    await store.connect()
    base = time.time() - 600

    # 1. The case that motivated this: internet down, the resolver (a proxy on
    #    loopback under WSL) answered, and nothing ever tested the wire.
    for index in range(2):
        ts = base + index
        await store.add(latency(ts, "resolver", "local", True, 100 + index))
        await store.add(latency(ts, "cloudflare", "internet", False, 100 + index))
        await store.add(latency(ts, "google", "internet", False, 100 + index))
    await store.open_incident(
        kind="internet", scope="isp", started_at=base, targets_total=4,
        targets_failed=2, failed_targets=["cloudflare", "google"], rounds=2,
    )
    await store.close_incident(
        (await store.current_incidents())[0]["id"], base + 2, end_uncertainty_s=0.0
    )

    # 2. A router that answered while the internet was down: genuinely the ISP.
    for index in range(2):
        ts = base + 10 + index
        await store.add(latency(ts, "gateway", "lan", True, 200 + index))
        await store.add(latency(ts, "cloudflare", "internet", False, 200 + index))
    await store.open_incident(
        kind="internet", scope="isp", started_at=base + 10, targets_total=3,
        targets_failed=1, failed_targets=["cloudflare"], rounds=2,
    )
    await store.close_incident(
        (await store.current_incidents())[0]["id"], base + 12, end_uncertainty_s=0.0
    )

    # 3. The router gone as well: the local network, correctly attributed.
    ts = base + 20
    await store.add(latency(ts, "gateway", "lan", False, 300))
    await store.add(latency(ts, "cloudflare", "internet", False, 300))
    await store.open_incident(
        kind="internet", scope="local", started_at=base + 20, targets_total=2,
        targets_failed=2, failed_targets=["gateway", "cloudflare"], rounds=1,
    )
    await store.close_incident(
        (await store.current_incidents())[0]["id"], base + 21, end_uncertainty_s=0.0
    )

    # 4. A DNS incident keeps its own scope.
    ts = base + 30
    await store.add(latency(ts, "dns", "dns", False, 400))
    await store.open_incident(
        kind="dns", scope="dns", started_at=base + 30, targets_total=1,
        targets_failed=1, failed_targets=["dns"], rounds=1,
    )
    await store.close_incident(
        (await store.current_incidents())[0]["id"], base + 31, end_uncertainty_s=0.0
    )
    await store.close()
    return tmp_path / "repair.db"


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def test_only_the_unprovable_attribution_changes(db):
    con = connect(db)
    try:
        changes = {row["started_at"]: (row["scope"], want) for row, want in repair.plan(con)}
        scopes = {row["started_at"]: row["scope"] for row in con.execute("SELECT * FROM outages")}
        starts = sorted(scopes)
    finally:
        con.close()

    # 1: isp -> internet, because no probe reached the router.
    assert changes[starts[0]] == ("isp", "internet")
    # 2: the router answered, so the ISP claim stands and is not reported.
    assert starts[1] not in changes
    # 3: already correct.
    assert starts[2] not in changes
    # 4: DNS incidents are not attribution cases at all.
    assert starts[3] not in changes
    assert len(changes) == 1


def test_apply_writes_the_new_scope_and_keeps_a_backup(db):
    con = connect(db)
    try:
        assert repair.plan(con)
    finally:
        con.close()

    assert repair.main(["--db", str(db), "--apply"]) == 0

    con = connect(db)
    try:
        assert repair.plan(con) == []
        scopes = [row["scope"] for row in con.execute("SELECT * FROM outages ORDER BY started_at")]
    finally:
        con.close()
    assert scopes[0] == "internet" and scopes[1] == "isp"

    backups = list(db.parent.glob("repair.db.backup-*"))
    assert backups, "no backup was taken before writing"
    assert Path(backups[0]).stat().st_size > 0


def test_a_dry_run_changes_nothing(db):
    before = db.read_bytes()
    assert repair.main(["--db", str(db)]) == 0
    assert db.read_bytes() == before
