"""The continuous sampling engine.

One asyncio task drives two cadences:

* a **latency round** every ``latency_interval`` seconds (default 2s) that
  probes every target concurrently and writes one sample per target,
* throughput tests on two tiers: a cheap fixed-size **burst** frequently and a
  duration-based **sustained** test rarely.

After each round the samples are reduced to a per-role verdict, and that
verdict drives two independent incident tracks ("internet" and "dns"). An
incident row is opened the first time connectivity is lost and closed when it
comes back, which is the artefact that proves a drop happened and says how long
it lasted.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from typing import Any, Sequence

from .config import Settings
from .probes import (
    OUTAGE_LABELS,
    classify_outage,
    evaluate_round,
    measure_round,
    measure_speed,
    measure_burst,
    now,
    perf,
)
from .storage import Store
from .targets import Target, build_targets
from .trace import trace_path

# Settings that may be changed from the dashboard while running.
UPDATABLE = {    "latency_interval": (float, 0.2, 3600.0),
    "fast_interval": (float, 0.0, 60.0),
    "fast_timeout": (float, 0.1, 30.0),
    "fast_hold_seconds": (float, 0.0, 600.0),
    "fast_max_seconds": (float, 0.0, 86400.0),
    "quick_interval": (float, 0.0, 86400.0),
    "quick_download_bytes": (int, 0, 1024 * 1024 * 1024),
    "quick_upload_bytes": (int, 0, 1024 * 1024 * 1024),
    "sustained_interval": (float, 0.0, 86400.0),
    "duration": (float, 0.0, 86400.0 * 7),
    "download_seconds": (float, 0.0, 600.0),
    "upload_seconds": (float, 0.0, 600.0),
    "download_chunk_bytes": (int, 64 * 1024, 512 * 1024 * 1024),
    "upload_chunk_bytes": (int, 64 * 1024, 512 * 1024 * 1024),
    "max_test_bytes": (int, 0, 100 * 1024 * 1024 * 1024),
    "streams": (int, 1, 16),
    "ping_count": (int, 1, 20),
    "ping_gap": (float, 0.0, 5.0),
    "target_timeout": (float, 0.2, 30.0),
    "incident_min_rounds": (int, 1, 100),
    "enable_latency": (bool, None, None),
    "enable_download": (bool, None, None),
    "enable_upload": (bool, None, None),
    "public_targets": (str, None, None),
    "extra_targets": (str, None, None),
    "fact_check_targets": (str, None, None),
    "fact_check_interval": (float, 0.0, 24 * 3600.0),
    "dns_probe_name": (str, None, None),
    "probe_gateway": (bool, None, None),
    "raw_window_hours": (float, 0.0, 24 * 365.0),
    "probe_resolver": (bool, None, None),
}


# Changing any of these rebuilds the target list, which deserves a fresh
# probation period: a target the user just reconfigured gets another chance.
TARGET_KEYS = frozenset(
    {
        "public_targets", "extra_targets", "fact_check_targets",
        "probe_gateway", "probe_resolver", "dns_probe_name",
    }
)

# How often to prune old samples and checkpoint the WAL during a long run.
MAINTENANCE_INTERVAL = 3600.0


class IncidentTracker:
    """Tracks one incident kind across rounds and persists the transitions."""

    def __init__(self, store: Store, kind: str) -> None:
        self.store = store
        self.kind = kind
        self.id: int | None = None
        self.started_at: float | None = None
        self.fail_since: float | None = None
        self.rounds = 0
        self.pending = 0
        self.scope = ""
        self.failed_targets: list[str] = []
        self.detail: dict = {}

    async def observe(
        self,
        *,
        ts: float,
        down: bool,
        scope: str,
        failed_targets: Sequence[str],
        total: int,
        detail: dict,
        min_rounds: int = 1,
        last_ok_ts: float | None = None,
        last_fail_ts: float | None = None,
    ) -> list[dict]:
        events: list[dict] = []

        if down:
            if self.pending == 0:
                # Remember when the trouble actually started, so a minimum
                # round count does not shave time off the recorded incident.
                self.fail_since = ts
            self.pending += 1
            self.rounds = self.pending
            self.failed_targets = list(failed_targets)
            self.detail = detail
            self.scope = scope or self.scope

            if self.id is None and self.pending >= max(1, min_rounds):
                self.id = await self.store.open_incident(
                    kind=self.kind,
                    scope=self.scope,
                    started_at=self.fail_since or ts,
                    targets_total=total,
                    targets_failed=len(self.failed_targets),
                    failed_targets=self.failed_targets,
                    detail=self.detail,
                    # Include the probation rounds that led up to opening it,
                    # because the incident is backdated to the first failure.
                    rounds=self.pending,
                    # How precisely the boundaries are known: the drop began
                    # somewhere between the last good probe and the first bad
                    # one, so say so rather than implying a stopwatch.
                    start_uncertainty_s=(
                        round(max(0.0, (self.fail_since or ts) - last_ok_ts), 2)
                        if last_ok_ts
                        else None
                    ),
                )
                self.started_at = self.fail_since or ts
                events.append({"type": "outage_start", "incident": self.snapshot(ts)})
            elif self.id is not None:
                await self.store.extend_incident(
                    self.id, self.rounds, len(self.failed_targets), self.failed_targets, self.detail
                )
                events.append({"type": "outage_update", "incident": self.snapshot(ts)})
        else:
            self.pending = 0
            if self.id is not None:
                incident = await self.store.close_incident(
                    self.id,
                    ts,
                    end_uncertainty_s=(
                        round(max(0.0, ts - last_fail_ts), 2) if last_fail_ts else None
                    ),
                )
                events.append({"type": "outage_end", "incident": incident})
                self.reset()
            self.fail_since = None

        return events

    def reset(self) -> None:
        self.id = None
        self.started_at = None
        self.rounds = 0
        self.failed_targets = []
        self.detail = {}
        self.scope = ""

    def snapshot(self, ts: float | None = None) -> dict:
        current = ts if ts is not None else now()
        return {
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "label": OUTAGE_LABELS.get(self.scope, self.scope),
            "started_at": self.started_at,
            "ongoing": self.id is not None,
            "duration_s": round(max(0.0, current - self.started_at), 1) if self.started_at else 0.0,
            "rounds": self.rounds,
            "failed_targets": list(self.failed_targets),
            "detail": dict(self.detail),
        }


class Sampler:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._run_now = asyncio.Event()
        self._subscribers: set[asyncio.Queue] = set()
        self._requested: set[str] = set()

        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.stop_reason: str | None = None
        self.last_error: str | None = None
        self.last_speed: dict | None = None
        self.last_quick: dict | None = None
        self.last_sustained: dict | None = None
        self.last_round: dict | None = None
        self.counts = {"latency": 0, "speed": 0, "errors": 0, "rounds": 0}
        self.bytes_used = 0
        self._next_latency: float | None = None
        self._next_quick: float | None = None
        self._next_sustained: float | None = None
        # Corroboration baseline timer. Declared here, not in start(), because
        # run_once() is used on its own by the CLI and by tests.
        self._next_fact_check: float | None = None
        self.in_flight: str | None = None
        self._t0: float | None = None

        self.targets: list[Target] = []
        self.disabled_targets: dict[str, str] = {}
        self._target_success: dict[str, int] = {}
        self._target_fail: dict[str, int] = {}
        self._speed_backoff = 1
        self._speed_forced: str | None = None
        self._manual_tiers: set[str] = set()
        # Adaptive probing state.
        self.fast_mode = False
        self.fast_since: float | None = None
        self.fast_reason: str | None = None
        self._ok_since: float | None = None
        # Keyed by incident kind, so DNS blips report their own boundary
        # precision rather than borrowing the internet probe's.
        # Live view of a throughput test while it runs, for the dashboard.
        self.speed_progress: dict | None = None
        self._last_ok_ts: dict[str, float] = {}
        self._last_fail_ts: dict[str, float] = {}
        # When each target was last probed, for the ones on their own slower
        # cadence (the uncached DNS queries).
        self._last_probe: dict[str, float] = {}
        # Path tracing: a background task, so a trace that hangs on a broken
        # path never delays the round that noticed the drop.
        self._trace_task: asyncio.Task | None = None
        self._last_trace_ts = 0.0
        self._last_drop_trace_ts = 0.0
        self._last_blackout_trace_ts = 0.0
        self.last_trace: dict | None = None
        # The loss burst, fired at the first failed round of a drop.
        self._burst_task: asyncio.Task | None = None
        self._last_burst_ts = 0.0
        self.last_burst: dict | None = None
        self._last_maintenance = 0.0
        self.maintenance: dict | None = None
        self.trackers: dict[str, IncidentTracker] = {
            "internet": IncidentTracker(store, "internet"),
            "dns": IncidentTracker(store, "dns"),
        }

    # ------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _first_deadline(
        self, tier: str, interval: float, warmup: float = 0.0
    ) -> float | None:
        """When this tier should first run after a start.

        With no recent sample it warms up quickly -- otherwise a fresh install
        shows an empty throughput view for a whole interval, which is exactly
        what the per-second panel did after the database was wiped. With a recent
        sample it waits out the rest of the interval instead of spending the data
        again on every restart.
        """
        if interval <= 0:
            return None
        latest = await self.store.latest_sample_ts("speed", tier=tier)
        if latest is None:
            return self._t0 + min(warmup, interval) if warmup else self._t0
        elapsed = time.time() - latest
        if elapsed >= interval:
            return self._t0 + min(warmup, interval)
        return self._t0 + (interval - elapsed)

    @property
    def latency_interval_now(self) -> float:
        """The cadence the next round will use."""
        if self.fast_mode and self.settings.fast_interval > 0:
            return self.settings.fast_interval
        return self.settings.latency_interval

    def refresh_targets(self) -> list[Target]:
        self.targets = build_targets(self.settings)
        for target in self.targets:
            self._target_success.setdefault(target.name, 0)
            self._target_fail.setdefault(target.name, 0)
        return self.targets

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._run_now.clear()
        self._requested.clear()
        self.refresh_targets()
        with contextlib.suppress(Exception):
            # Anything left open by a crash belongs to a previous process.
            await self.store.close_dangling(now())
        for tracker in self.trackers.values():
            tracker.reset()
        self.started_at = now()
        self.finished_at = None
        self.stop_reason = None
        self.counts = {"latency": 0, "speed": 0, "errors": 0, "rounds": 0}
        self.bytes_used = 0
        self.last_error = None
        self.last_round = None
        self.last_speed = None
        self.last_quick = None
        self.last_sustained = None
        self._speed_forced = None
        self._manual_tiers = set()
        self.fast_mode = False
        self.fast_since = None
        self.fast_reason = None
        self._ok_since = None
        self.speed_progress = None
        self._last_ok_ts = {}
        self._last_fail_ts = {}
        self._last_probe = {}
        self._last_trace_ts = 0.0
        self._last_drop_trace_ts = 0.0
        self._last_blackout_trace_ts = 0.0
        self._last_burst_ts = 0.0
        self._t0 = perf()
        self._next_latency = self._t0
        # The cheap burst runs immediately so the dashboard has a number.
        # The sustained test instead waits a full interval: firing it on every
        # restart would cost hundreds of megabytes each time, and an interval of
        # 0 must mean "never", not "only at startup". Its "Speed test" button
        # runs one on demand.
        self._next_quick = await self._first_deadline("quick", self.settings.quick_interval)
        self._next_sustained = await self._first_deadline(
            "sustained", self.settings.sustained_interval, warmup=60.0
        )
        # Put the last stored test back on the dashboard. Without this, a restart
        # blanks the per-second panel until the next test -- up to an hour, and
        # the samples are right there in the database.
        for tier, attribute in (("quick", "last_quick"), ("sustained", "last_sustained")):
            with contextlib.suppress(Exception):
                setattr(self, attribute, await self.store.latest_sample("speed", tier=tier))
        self._task = asyncio.create_task(self._loop(), name="droptrace-sampler")

    async def stop(self, reason: str = "stopped") -> None:
        self._stop.set()
        self._wake()
        task = self._task
        if task is not None:
            # Let the loop finish whatever probe is in flight (a speed test can
            # take a while on a slow link), then bail out.
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=120)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._task = None
        # A trace in flight is cancelled with the sampler; trace_path kills the
        # child process so no tracer outlives the run that asked for it.
        for attribute in ("_trace_task", "_burst_task"):
            task, setattr(self, attribute, None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.finished_at = now()
        self.stop_reason = reason
        self._next_latency = None
        self._next_quick = None
        self._next_sustained = None
        with contextlib.suppress(Exception):
            # Do not leave an incident hanging open across a restart.
            await self.store.close_dangling(self.finished_at)
        for tracker in self.trackers.values():
            tracker.reset()
        self._publish({"type": "state", "state": self.snapshot()})

    def request_run(self, kind: str = "all") -> None:
        """Ask the loop to probe again right away."""
        self._requested.add(kind)
        self._wake()

    def _wake(self) -> None:
        self._run_now.set()

    # ----------------------------------------------------------- main loop
    async def _loop(self) -> None:
        assert self._t0 is not None
        try:
            while not self._stop.is_set():
                if self.settings.duration and perf() - self._t0 >= self.settings.duration:
                    self.stop_reason = "duration"
                    break

                if self._run_now.is_set():
                    self._run_now.clear()
                    requested = self._requested
                    self._requested = set()
                    if "latency" in requested or "all" in requested:
                        self._next_latency = perf()
                    if "quick" in requested:
                        self._next_quick = perf()
                        self._speed_forced = "quick"
                        self._manual_tiers.add("quick")
                    if "speed" in requested or "sustained" in requested or "all" in requested:
                        # An explicit request must run even when the automatic
                        # schedule is off, otherwise the "Speed test" button
                        # silently does nothing.
                        self._next_sustained = perf()
                        self._speed_forced = "sustained"
                        self._manual_tiers.add("sustained")

                current = perf()
                due_latency = self._next_latency is not None and current >= self._next_latency
                due_quick = self._next_quick is not None and current >= self._next_quick
                due_sustained = (
                    self._next_sustained is not None and current >= self._next_sustained
                )

                if due_latency:
                    await self._probe_round()
                    # Keep an absolute cadence: schedule from the previous
                    # deadline, not from "now", so a slow round does not make
                    # every later round late too.
                    self._next_latency = max(
                        (self._next_latency or current) + self.latency_interval_now,
                        perf() + 0.02,
                    )
                if due_quick and self.fast_mode:
                    # A throughput test would block the fine-grained probing for
                    # tens of seconds and would fail anyway during an outage, so
                    # defer it until the link is back.
                    #
                    # The "you asked for this" mark goes with the deferral: by the
                    # time it runs it is the clock that is running it, and calling
                    # that run manual would pop a "started by you" modal for a test
                    # nobody asked for at that moment.
                    self._manual_tiers.discard("quick")
                    self._next_quick = perf() + max(1.0, self.settings.quick_interval or 60)
                    due_quick = False
                if due_quick:
                    await self._probe_speed("quick", self._take_trigger("quick"))
                    self._next_quick = self._reschedule(
                        self._next_quick, current, self.settings.quick_interval, quick=True
                    )
                if due_sustained and self.fast_mode:
                    self._manual_tiers.discard("sustained")
                    self._next_sustained = perf() + max(
                        1.0, self.settings.sustained_interval or 60
                    )
                    due_sustained = False
                if due_sustained:
                    await self._probe_speed("sustained", self._take_trigger("sustained"))
                    self._next_sustained = self._reschedule(
                        self._next_sustained, current, self.settings.sustained_interval
                    )

                await self._maintain()

                deadlines = [
                    d
                    for d in (self._next_latency, self._next_quick, self._next_sustained)
                    if d is not None
                ]
                if not deadlines:
                    break
                delay = max(0.0, min(deadlines) - perf())
                if delay > 0:
                    await self._sleep(delay)
        except asyncio.CancelledError:
            self.stop_reason = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - keep the loop alive no matter what
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.counts["errors"] += 1
        finally:
            self.finished_at = now()
            if self.stop_reason is None:
                self.stop_reason = "finished"
            self._next_latency = None
            self._next_quick = None
            self._next_sustained = None
            self._publish({"type": "state", "state": self.snapshot()})

    async def _sleep(self, delay: float) -> None:
        """Sleep, but wake early for stop / run-now requests."""
        stop_task = asyncio.ensure_future(self._stop.wait())
        now_task = asyncio.ensure_future(self._run_now.wait())
        try:
            await asyncio.wait(
                {stop_task, now_task}, timeout=delay, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (stop_task, now_task):
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(stop_task, now_task, return_exceptions=True)

    # --------------------------------------------------------------- probes
    def _target_due(self, target: Target, ts: float) -> bool:
        """Whether a target is due, for the ones on their own slower cadence.

        Only the uncached DNS queries use this today, and they must be slow:
        every one of them is a real question to the authoritative servers, and
        the point is to catch a dead upstream, not to hammer the resolvers.
        """
        if target.cadence <= 0:
            return True
        last = self._last_probe.get(target.name)
        return last is None or ts - last >= target.cadence

    # ---------------------------------------------------------------- loss
    def _burst_endpoint(self) -> tuple[str, str, int]:
        """``(target name, host, port)`` for the burst.

        Matched against the public targets so the recorded row carries the name
        the rest of the page uses for that endpoint.
        """
        host = (self.settings.burst_host or "").strip()
        for target in self.targets:
            if target.role == "internet" and target.host == host and target.ports:
                return target.name, target.host, target.ports[0]
        return host, host, 443

    def _maybe_burst(self, samples: Sequence[dict], ts: float) -> None:
        """Fire a counted burst when the endpoints that decide a round fail.

        Deliberately *not* "when the round is down". The corroboration pool can
        hold a round up while the primary endpoints are black-holed -- measured
        on this connection, 47 times in half an hour, with the router answering
        in 1.5 ms throughout -- and those partial blackouts are exactly the
        moments whose loss is worth counting. Waiting for every internet target
        to fail would miss the event the user actually feels.

        This is also the only measurement that says *how much* of the traffic was
        getting through: a round probe can only report 0% or 100%. Backgrounded
        for the same reason as a trace: on a dead path twenty handshakes take
        seconds.
        """
        settings = self.settings
        if settings.burst_handshakes <= 0:
            return
        if self._burst_task is not None and not self._burst_task.done():
            return
        if ts - self._last_burst_ts < settings.burst_cooldown:
            return

        primary = {
            t.name
            for t in self.targets
            if t.role == "internet" and not t.fact_check and t.name not in self.disabled_targets
        }
        in_round = {s.get("target") for s in samples if s.get("target") in primary}
        failed = {s.get("target") for s in samples if s.get("target") in primary and not s.get("ok")}
        if not failed:
            return
        name, _host, _port = self._burst_endpoint()
        if name not in failed and failed != in_round:
            return
        self._last_burst_ts = ts
        self._burst_task = asyncio.create_task(self._burst(ts))

    async def _burst(self, ts: float) -> None:
        settings = self.settings
        try:
            name, host, port = self._burst_endpoint()
            sample = await measure_burst(
                name,
                host,
                port,
                count=settings.burst_handshakes,
                window=settings.burst_window,
                timeout=settings.burst_timeout,
                max_seconds=settings.burst_window + settings.burst_handshakes * settings.burst_timeout,
                round_id=int(ts * 1000),
                ts=ts,
            )
            await self.store.add_many([sample])
            self.last_burst = sample
            self._publish({"type": "burst", "burst": sample})
            if float(sample.get("loss_pct") or 0.0) >= 50.0:
                self._maybe_blackout_trace(sample["ts"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never kill the loop over a burst
            self.last_error = f"burst: {type(exc).__name__}: {exc}"
        finally:
            self._burst_task = None

    def _maybe_blackout_trace(self, ts: float) -> None:
        """Trace a partial blackout, not only a full drop.

        The events measured on this connection -- both primary endpoints "No
        route to host" while the corroboration pool still answers -- never open
        an incident, so without this they would have a loss figure and no path.
        "Where did it stop" is exactly the question they leave open, and the
        path is broken *now*, which is when a trace is worth most.

        Slower than the incident trace on purpose: it is fired from a burst
        (itself rate-limited) and capped at one per ``trace_blackout_interval``,
        because each one is a real tracer run.
        """
        settings = self.settings
        if not settings.trace_host or settings.trace_timeout <= 0:
            return
        if settings.trace_blackout_interval <= 0:
            return
        if ts - self._last_blackout_trace_ts < settings.trace_blackout_interval:
            return
        if self._trace_task is not None and not self._trace_task.done():
            return
        self._last_blackout_trace_ts = ts
        self._last_trace_ts = ts
        self._trace_task = asyncio.create_task(self._trace("drop", None))

    # ---------------------------------------------------------------- tracing
    def _maybe_trace(self, verdict: dict, events: Sequence[dict], ts: float) -> None:
        """Start a background trace: when a drop begins, and once in a while.

        The drop trace is the one that matters -- it is the hop where the
        traffic dies -- and the baseline is what makes it readable, because a
        path through a dozen private addresses only means something next to the
        same path when it worked. Both are fired as tasks: tracert takes ~17s
        here and can hang for its whole timeout on a broken path, and the fast
        probe cadence must not wait for it.
        """
        settings = self.settings
        if not settings.trace_host or settings.trace_timeout <= 0:
            return
        if self._trace_task is not None and not self._trace_task.done():
            return

        trigger = ""
        incident_id = None
        for event in events:
            incident = event.get("incident") or {}
            if event.get("type") == "outage_start" and incident.get("kind") == "internet":
                trigger, incident_id = "drop", incident.get("id")
                break

        if trigger == "drop":
            if ts - self._last_drop_trace_ts < settings.trace_cooldown:
                return
        else:
            if verdict.get("internet_ok") is not True or settings.trace_interval <= 0:
                return
            if ts - self._last_trace_ts < settings.trace_interval:
                return
            trigger = "baseline"

        # Book the slot before the trace starts, not after it lands: otherwise a
        # tracer that hangs or fails would be retried every round.
        self._last_trace_ts = ts
        if trigger == "drop":
            self._last_drop_trace_ts = ts
        self._trace_task = asyncio.create_task(self._trace(trigger, incident_id))

    async def _trace(self, trigger: str, incident_id: int | None) -> None:
        settings = self.settings
        try:
            trace = await trace_path(
                settings.trace_host,
                timeout=settings.trace_timeout,
                hop_timeout=settings.trace_hop_timeout,
                max_hops=settings.trace_max_hops,
            )
            trace["trigger"] = trigger
            await self.store.add_trace(trace, incident_id)
            self.last_trace = trace
            self._publish({"type": "trace", "trace": trace})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never kill the loop over a trace
            self.last_error = f"trace: {type(exc).__name__}: {exc}"
        finally:
            self._trace_task = None

    async def _probe_round(self) -> list[dict] | None:
        if not self.settings.enable_latency:
            return None
        self.in_flight = "latency"
        self._publish({"type": "probing", "kind": "latency"})
        round_id = int(now() * 1000)
        round_ts = now()
        try:
            active = [
                t for t in self.targets
                if t.name not in self.disabled_targets
                and not t.fact_check
                and self._target_due(t, round_ts)
            ]
            samples = await measure_round(self._probe_settings(), active, round_id)
            for target in active:
                self._last_probe[target.name] = round_ts
            samples = await self._corroborate(samples, round_id)
        except Exception as exc:  # noqa: BLE001
            samples = []
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.counts["errors"] += 1
        finally:
            self.in_flight = None

        if not samples:
            return None

        try:
            await self.store.add_many(samples)
        except Exception as exc:  # noqa: BLE001 - never kill the loop over a write
            self.last_error = f"store: {type(exc).__name__}: {exc}"

        # Update target health first: a guess stood down this round should not
        # count as a fault, nor influence the verdict that attributes the round.
        self._update_target_health(samples)
        active = [s for s in samples if s.get("target") not in self.disabled_targets]
        verdict = evaluate_round(active)
        # Failures from a guess that has never once answered are a configuration
        # observation, not a fault: the manual/auto-detected target simply does
        # not speak TCP. The targets table shows it stood down; it should not
        # also masquerade as the connection's last error.
        failures = [s for s in active if not s.get("ok") and not self._is_dead_guess(s.get("target"))]
        self.counts["latency"] += len(samples)
        self.counts["errors"] += len(failures)
        self.counts["rounds"] += 1
        self.last_error = next((s.get("error") for s in failures if s.get("error")), None)

        self.last_round = {
            "round_id": round_id,
            "ts": samples[0].get("ts"),
            "verdict": verdict,
            "scope": classify_outage(verdict),
            "scope_label": OUTAGE_LABELS.get(classify_outage(verdict), ""),
            "samples": samples,
            "internet_ok": verdict.get("internet_ok"),
            "lan_ok": verdict.get("lan_ok"),
            "local_ok": verdict.get("local_ok"),
            "dns_ok": verdict.get("dns_ok"),
        }

        self._update_fast_mode(verdict, samples[0].get("ts") or now())
        events = await self._track_incidents(verdict, samples)
        round_ts = samples[0].get("ts") or now()
        self._maybe_burst(samples, round_ts)
        self._maybe_trace(verdict, events, round_ts)
        self._publish({"type": "round", "round": self.last_round})
        for event in events:
            self._publish(event)
        return samples

    def _probe_settings(self) -> Settings:
        """Settings for the round about to run.

        While resolving an outage, one short attempt per target instead of the
        usual couple of long ones: the round cannot be faster than the timeout
        of the target that is failing, and a dead target would otherwise hold
        every round at ~2s no matter what the fast cadence is set to.
        """
        if not self.fast_mode or self.settings.fast_interval <= 0:
            return self.settings
        return dataclasses.replace(
            self.settings,
            ping_count=1,
            ping_gap=0.0,
            target_timeout=min(self.settings.target_timeout, self.settings.fast_timeout),
        )

    def _update_fast_mode(self, verdict: dict, ts: float) -> None:
        """Speed up probing while the link is down, and slow back down after.

        Detection happens at the configured cadence; this only sharpens the
        boundaries of a drop that has already been seen, which is what turns
        "it broke sometime in the last 10 seconds" into "it broke at 12:03:41".
        """
        interval = self.settings.fast_interval
        internet = verdict.get("internet_ok")
        dns = verdict.get("dns_ok")

        # Remember the last good and last bad round per role, so each incident
        # can say how precisely its own boundaries are known.
        for role, state in (("internet", internet), ("dns", dns)):
            if state is None:
                continue
            if state:
                self._last_ok_ts[role] = ts
            else:
                self._last_fail_ts[role] = ts

        if interval <= 0 or (internet is None and dns is None):
            return

        # A resolver that stops answering looks exactly like "the internet is
        # down" to a browser or a game, so a DNS failure deserves the same
        # fine-grained resolution as a loss of raw connectivity.
        trouble = internet is False or dns is False
        healthy = internet is not False and dns is not False

        if healthy:
            if not self.fast_mode:
                return
            if self._ok_since is None:
                self._ok_since = ts
            if ts - self._ok_since >= self.settings.fast_hold_seconds:
                self._leave_fast_mode("recovered", ts)
            return

        if not trouble:
            return

        # Something is down. A failure breaks the recovery streak: otherwise a
        # flapping link would accumulate non-consecutive successes and drop out
        # of fast mode in the middle of the flapping.
        self._ok_since = None
        if not self.fast_mode:
            self.fast_mode = True
            self.fast_since = ts
            self.fast_reason = (
                "dns failing" if internet is not False else "outage detected"
            )
            self._announce_cadence(ts)
            return
        if (
            self.settings.fast_max_seconds > 0
            and self.fast_since is not None
            and ts - self.fast_since >= self.settings.fast_max_seconds
        ):
            self._leave_fast_mode("fast window elapsed", ts)

    def _leave_fast_mode(self, reason: str, ts: float) -> None:
        if not self.fast_mode:
            return
        self.fast_mode = False
        self.fast_reason = reason
        self.fast_since = None
        self._ok_since = None
        self._announce_cadence(ts)

    def _announce_cadence(self, ts: float) -> None:
        self._publish({"type": "cadence", "state": self.fast_snapshot(ts)})

    def fast_snapshot(self, ts: float | None = None) -> dict:
        current = ts if ts is not None else now()
        return {
            "active": self.fast_mode,
            "interval": self.settings.fast_interval if self.fast_mode
            else self.settings.latency_interval,
            "since": self.fast_since,
            "elapsed_s": round(current - self.fast_since, 1) if self.fast_since else None,
            "reason": self.fast_reason,
            "hold_seconds": self.settings.fast_hold_seconds,
            "max_seconds": self.settings.fast_max_seconds,
        }

    async def _track_incidents(self, verdict: dict, samples: Sequence[dict]) -> list[dict]:
        events: list[dict] = []
        ts = samples[0].get("ts") or now()
        failed = list(verdict.get("failed") or [])
        detail = {"errors": verdict.get("errors") or {}}

        internet = verdict.get("internet_ok")
        if internet is not None:
            events += await self.trackers["internet"].observe(
                ts=ts,
                down=not internet,
                scope=classify_outage(verdict),
                failed_targets=failed,
                total=int(verdict.get("total") or len(samples)),
                detail=detail,
                min_rounds=self.settings.incident_min_rounds,
                last_ok_ts=self._last_ok_ts.get("internet"),
                last_fail_ts=self._last_fail_ts.get("internet"),
            )

        dns_ok = verdict.get("dns_ok")
        if dns_ok is not None:
            events += await self.trackers["dns"].observe(
                ts=ts,
                down=not dns_ok,
                scope="dns",
                failed_targets=[s["target"] for s in samples if s.get("role") == "dns" and not s.get("ok")],
                total=1,
                detail=detail,
                min_rounds=self.settings.incident_min_rounds,
                last_ok_ts=self._last_ok_ts.get("dns"),
                last_fail_ts=self._last_fail_ts.get("dns"),
            )
        return events

    def _is_dead_guess(self, name: str | None) -> bool:
        """True for an auto-detected target that has never answered."""
        if not name:
            return False
        target = next((t for t in self.targets if t.name == name), None)
        return bool(
            target and target.guessed and self._target_success.get(name, 0) == 0
        )

    def _update_target_health(self, samples: Sequence[dict]) -> None:
        for sample in samples:
            name = sample.get("target") or "?"
            if sample.get("ok"):
                self._target_success[name] = self._target_success.get(name, 0) + 1
                self._target_fail[name] = 0
            else:
                self._target_fail[name] = self._target_fail.get(name, 0) + 1

        # A guessed target (auto-detected gateway, resolver) that never answers
        # is almost certainly filtered rather than down; standing it down keeps
        # it from mislabelling every outage as "your LAN is down".
        probation = max(1, self.settings.target_probation_rounds)
        for target in self.targets:
            if not target.guessed or target.name in self.disabled_targets:
                continue
            if (
                self._target_success.get(target.name, 0) == 0
                and self._target_fail.get(target.name, 0) >= probation
            ):
                reason = (
                    f"no response in {probation} rounds - {target.describe()} may filter TCP; "
                    "excluded from outage attribution"
                )
                self.disabled_targets[target.name] = reason
                # Its failures were never a connectivity fault, so stop
                # counting them as errors.
                self.counts["errors"] = max(
                    0, self.counts["errors"] - self._target_fail.get(target.name, 0)
                )
                self._publish(
                    {"type": "target", "name": target.name, "disabled": True, "reason": reason}
                )

    def _reschedule(
        self, previous: float | None, current: float, interval: float, quick: bool = False
    ) -> float | None:
        """Next deadline for a tier, or None when it runs no more."""
        if self._speed_forced == ("quick" if quick else "sustained"):
            self._speed_forced = None
            if interval <= 0:
                return None        # on-demand only
        if interval <= 0:
            return None
        # Back off when the provider asks us to slow down.
        return max((previous or current) + interval * self._speed_backoff, perf() + 0.02)

    async def _maintain(self) -> None:
        """Housekeeping on a timer: retention pruning and WAL checkpointing.

        Pruning only at startup is not enough for a tool built to run for days
        without a restart, which is exactly how it gets used.
        """
        if time.time() - self._last_maintenance < MAINTENANCE_INTERVAL:
            return
        self._last_maintenance = time.time()
        try:
            # Fold first, prune second: a probe must be summarised before any
            # retention rule is allowed to delete it.
            rolled = await self.store.rollout(self.settings.raw_window_hours)
            removed = await self.store.prune(self.settings.retention_days)
            # TRUNCATE reclaims the WAL file's disk space as well as folding it
            # back into the database. Once an hour there is no contention.
            await self.store.checkpoint("TRUNCATE")
        except Exception as exc:  # noqa: BLE001 - housekeeping must never be fatal
            self.last_error = f"maintenance: {type(exc).__name__}: {exc}"
            return
        self.maintenance = {
            "rolled": rolled,
            "at": time.time(),
            "pruned_rows": removed,
            "retention_days": self.settings.retention_days,
        }
        if removed:
            self._publish({"type": "maintenance", "removed": removed})

    def _take_trigger(self, tier: str) -> str:
        """Whether this run was asked for by hand or came round on the clock."""
        if tier in self._manual_tiers:
            self._manual_tiers.discard(tier)
            return "manual"
        return "scheduled"

    async def _corroborate(self, samples: list[dict], round_id: int) -> list[dict]:
        """Confirm a suspected drop against hosts on other networks.

        Two reasons to spend the connections: the primary targets say the link is
        down (in which case a second opinion from five other networks is exactly
        what turns "your probe endpoint failed" into evidence), or the slow
        baseline timer came round, which is how we learn which of these hosts are
        reachable at all here -- otherwise a host the ISP blocks could sit in the
        pool looking like a witness when it never answers anyway.
        """
        if not samples:
            return samples
        checks = [
            t for t in self.targets
            if t.fact_check and t.name not in self.disabled_targets
        ]
        if not checks:
            return samples
        suspects = evaluate_round(samples).get("internet_ok") is False
        interval = self.settings.fact_check_interval
        if self._next_fact_check is None and interval > 0:
            # Armed on first use, so run_once() and the CLI's one-shot probe get a
            # baseline too -- they never go through start().
            self._next_fact_check = perf() + interval
        due = self._next_fact_check is not None and perf() >= self._next_fact_check
        if not (suspects or due):
            return samples
        if self.settings.fact_check_interval > 0:
            self._next_fact_check = perf() + self.settings.fact_check_interval
        self._publish({
            "type": "corroborating",
            "kind": "latency",
            "reason": "drop" if suspects else "baseline",
            "targets": [t.name for t in checks],
        })
        try:
            extra = await measure_round(self._probe_settings(), checks, round_id)
        except Exception as exc:  # noqa: BLE001 - never kill the loop over this
            self.last_error = f"corroborate: {type(exc).__name__}: {exc}"
            return samples
        return samples + extra

    async def _probe_speed(self, tier: str = "sustained", trigger: str = "scheduled") -> dict | None:
        if not (self.settings.enable_download or self.settings.enable_upload):
            return None
        self.in_flight = "speed"
        self._publish({"type": "probing", "kind": "speed", "tier": tier, "trigger": trigger})

        def on_progress(payload: dict) -> None:
            # Called from inside the transfer, a few times a second, so the
            # dashboard can show the rate climbing while it is still measuring.
            self.speed_progress = {**payload, "running": True}
            self._publish({"type": "speed_progress", **payload})

        try:
            sample = await measure_speed(
                self.settings, tier=tier, trigger=trigger, progress=on_progress
            )
        except Exception as exc:  # noqa: BLE001
            sample = {
                "ts": now(),
                "kind": "speed",
                "tier": tier,
                "trigger": trigger,
                "target": "speedtest",
                "role": "internet",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            self.in_flight = None
            self.speed_progress = None

        saved = await self._record(sample)
        return saved

    async def _record(self, sample: dict) -> dict:
        saved = await self.store.add(sample)
        kind = sample.get("kind", "latency")
        self.counts[kind] = self.counts.get(kind, 0) + 1
        throttled = bool(sample.get("throttled"))
        if throttled:
            # Being rate limited by the speed test provider says nothing about
            # the connection, so it must not look like a fault.
            self.counts["throttled"] = self.counts.get("throttled", 0) + 1
        elif not sample.get("ok", True):
            self.counts["errors"] += 1
            self.last_error = sample.get("error")
        self.bytes_used += int(sample.get("download_bytes") or 0)
        self.bytes_used += int(sample.get("upload_bytes") or 0)
        if kind == "speed":
            self.last_speed = saved
            if sample.get("tier") == "quick":
                self.last_quick = saved
            else:
                self.last_sustained = saved
            if throttled:
                # Exponential backoff, capped at 8x the configured interval.
                self._speed_backoff = min(8, max(2, self._speed_backoff * 2))
            else:
                self._speed_backoff = 1
        self._publish({"type": "sample", "sample": saved})
        return saved

    async def run_once(self, kind: str = "all") -> None:
        """Blocking one-shot measurement used by the CLI."""
        if not self.targets:
            self.refresh_targets()
        if kind in ("latency", "all"):
            await self._probe_round()
        if kind in ("speed", "all"):
            await self._probe_speed()

    # ------------------------------------------------------- live reconfig
    def update_settings(self, updates: dict) -> dict:
        """Apply dashboard changes without restarting the sampler."""
        applied: dict = {}
        changed: set[str] = set()
        for key, value in updates.items():
            spec = UPDATABLE.get(key)
            if spec is None or value is None:
                continue
            caster, low, high = spec
            try:
                if caster is bool:
                    coerced = value if isinstance(value, bool) else str(value).lower() in {
                        "1", "true", "yes", "on",
                    }
                elif caster is str:
                    coerced = str(value)
                else:
                    coerced = caster(float(value))
            except (TypeError, ValueError):
                continue
            if low is not None and coerced < low:
                coerced = caster(low)
            if high is not None and coerced > high:
                coerced = caster(high)
            previous = getattr(self.settings, key)
            setattr(self.settings, key, coerced)
            applied[key] = coerced
            if coerced != previous:
                changed.add(key)

        if applied:
            # Only a real change to the targets earns a fresh probation period.
            # The dashboard posts every field on every Apply, so keying off the
            # posted keys meant changing the probe interval re-probed a gateway
            # that is already known never to answer, surfacing its timeouts as a
            # connection error.
            if changed & TARGET_KEYS:
                self.disabled_targets.clear()
                self._target_success.clear()
                self._target_fail.clear()
            self.refresh_targets()
            if "latency_interval" in applied and self._next_latency is not None:
                # A shorter interval takes effect immediately instead of after
                # the old (longer) deadline has elapsed.
                self._next_latency = min(
                    self._next_latency, perf() + self.settings.latency_interval
                )
            for key, target in (
                ("quick_interval", "_next_quick"),
                ("sustained_interval", "_next_sustained"),
            ):
                if key not in applied:
                    continue
                interval = getattr(self.settings, key)
                if interval <= 0:
                    setattr(self, target, None)
                else:
                    current = getattr(self, target)
                    setattr(self, target, min(current or (perf() + interval), perf() + interval))
            self._wake()
            self._publish({"type": "config", "settings": self.settings.to_dict()})
        return applied

    # ---------------------------------------------------------- subscribers
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest event and keep the newest.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    # -------------------------------------------------------------- status
    def targets_snapshot(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "label": t.label,
                "role": t.role,
                "kind": t.kind,
                "address": t.describe(),
                "guessed": t.guessed,
                "disabled": t.name in self.disabled_targets,
                "disabled_reason": self.disabled_targets.get(t.name),
                "fact_check": t.fact_check,
                "success": self._target_success.get(t.name, 0),
                "failures": self._target_fail.get(t.name, 0),
            }
            for t in self.targets
        ]

    def snapshot(self) -> dict:
        current = perf()
        remaining = None
        if self.settings.duration and self._t0 is not None:
            remaining = max(0.0, self.settings.duration - (current - self._t0))
        return {
            "running": self.running,
            "in_flight": self.in_flight,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stop_reason": self.stop_reason,
            "elapsed_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "duration_s": self.settings.duration,
            "remaining_s": round(remaining, 1) if remaining is not None else None,
            "next_latency_in": round(max(0.0, self._next_latency - current), 1)
            if self._next_latency
            else None,
            "next_quick_in": round(max(0.0, self._next_quick - current), 1)
            if self._next_quick
            else None,
            "next_sustained_in": round(max(0.0, self._next_sustained - current), 1)
            if self._next_sustained
            else None,
            "latency_interval": self.settings.latency_interval,
            "latency_interval_now": self.latency_interval_now,
            "fast": self.fast_snapshot(),
            "quick_interval": self.settings.quick_interval,
            "sustained_interval": self.settings.sustained_interval,
            "counts": dict(self.counts),
            "speed_backoff": self._speed_backoff,
            "maintenance": self.maintenance,
            "bytes_used": self.bytes_used,
            "last_error": self.last_error,
            "last_speed": self.last_speed,
            "speed_progress": self.speed_progress,
            "last_quick": self.last_quick,
            "last_sustained": self.last_sustained,
            "last_round": self.last_round,
            "burst": {
                "host": self.settings.burst_host,
                "handshakes": self.settings.burst_handshakes,
                "in_flight": bool(self._burst_task and not self._burst_task.done()),
                "last": self.last_burst,
            },
            "trace": {
                "host": self.settings.trace_host,
                "in_flight": bool(self._trace_task and not self._trace_task.done()),
                "last_ts": self._last_trace_ts or None,
                "last_drop_ts": self._last_drop_trace_ts or None,
                "last": self.last_trace,
            },
            "targets": self.targets_snapshot(),
            "incidents": {kind: tracker.snapshot() for kind, tracker in self.trackers.items()},
            "subscribers": len(self._subscribers),
        }
