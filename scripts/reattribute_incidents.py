#!/usr/bin/env python3
"""Re-derive the scope of already-recorded incidents from the stored samples.

Attribution got stricter: a claim that "the LAN was fine" now has to come from a
probe that reaches the router. Before, any target with the ``local`` role could
make that claim, and under WSL the resolver is a proxy *inside* the VM (it sits
on loopback), so it answered while nothing crossed the wire -- and a drop could
be labelled "ISP or upstream down" that was never actually attributed.

Only the derived scope stored on each incident row changes; every sample is left
exactly as recorded. Run without --apply to see the list, and with it to write
(a copy of the database is taken first).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from droptrace.probes import OUTAGE_LABELS, classify_outage, evaluate_round  # noqa: E402


def verdicts_for(con: sqlite3.Connection, started: float, ended: float) -> list[dict]:
    """The per-round verdicts inside one incident's window, in time order."""
    rows = con.execute(
        "SELECT * FROM samples WHERE kind = 'latency' AND ts BETWEEN ? AND ? ORDER BY ts",
        (started - 1.0, ended + 1.0),
    ).fetchall()
    groups: dict[object, list[dict]] = {}
    for row in rows:
        data = dict(row)
        # round_id is authoritative; fall back to the timestamp for rows written
        # before it existed.
        groups.setdefault(data.get("round_id") or round(data["ts"], 1), []).append(data)
    return [evaluate_round(group) for _, group in sorted(groups.items(), key=lambda kv: kv[0])]


def recompute_scope(con: sqlite3.Connection, incident: sqlite3.Row) -> str:
    """What this incident's scope should be, judged by the samples on record.

    Mirrors the sampler: the incident carries the attribution of the last round
    that saw it down.
    """
    if incident["kind"] != "internet":
        return incident["scope"]
    scopes = [
        classify_outage(verdict)
        for verdict in verdicts_for(con, incident["started_at"], incident["ended_at"])
    ]
    down = [scope for scope in scopes if scope]
    return down[-1] if down else incident["scope"]


def plan(con: sqlite3.Connection) -> list[tuple[sqlite3.Row, str]]:
    """Every incident whose stored scope disagrees with the samples."""
    changes = []
    for incident in con.execute("SELECT * FROM outages ORDER BY started_at"):
        want = recompute_scope(con, incident)
        if want != incident["scope"]:
            changes.append((incident, want))
    return changes


def backup(path: Path) -> Path:
    """A consistent copy of the database, WAL and all."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_suffix(path.suffix + f".backup-{stamp}")
    source = sqlite3.connect(path)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".local/share/droptrace/droptrace.db"),
        type=Path,
    )
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        changes = plan(con)
        if not changes:
            print("every incident already matches its samples")
            return 0
        for incident, want in changes:
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(incident["started_at"]))
            print(
                f"  {when}  {incident['scope']:9s} -> {want:9s}  "
                f"{OUTAGE_LABELS.get(want, '')}"
            )
        print(f"\n{len(changes)} of {con.execute('SELECT count(*) FROM outages').fetchone()[0]} incidents")
        if not args.apply:
            print("dry run: pass --apply to write these")
            return 0
        copy = backup(args.db)
        print(f"backup: {copy}")
        con.executemany(
            "UPDATE outages SET scope = ? WHERE id = ?",
            [(want, incident["id"]) for incident, want in changes],
        )
        con.commit()
        print("updated")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
