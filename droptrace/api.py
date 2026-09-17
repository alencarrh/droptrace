"""HTTP API + static dashboard for DropTrace."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, parse_seconds
from .probes import OUTAGE_LABELS
from .stats import build as build_stats
from .sampler import UPDATABLE, Sampler
from .storage import Store
from .trace import trace_path

STATIC_DIR = Path(__file__).parent / "static"

# Vantage points other than this machine report here. The label is what the
# comparison view groups by, so it is deliberately narrow.
SOURCE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
MAX_AGENT_PROBES = 500
# Accepted within a day of now: a phone with a wrong clock must not be able to
# write probes into last week's record.
AGENT_CLOCK_SLACK_S = 86400.0

# The dashboard is edited in place and reloaded by whoever is watching it, so a
# heuristic-cached app.js is worse than a 304: it can quietly keep running last
# week's UI. Revalidate instead.
NO_CACHE = "no-cache, must-revalidate"


class RevalidatingStaticFiles(StaticFiles):
    """Serve assets so the browser always checks with us before reusing them."""

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        # Covers the 200 and the 304, so a revalidated asset never falls back to
        # heuristic freshness either.
        response.headers["Cache-Control"] = NO_CACHE
        return response


# Presets for the dashboard range selector.
WINDOWS: dict[str, float] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
    "all": 0,
}


def _resolve_until(until: float | None) -> float | None:
    """Upper bound of the window, or None meaning "up to now"."""
    return float(until) if until else None


def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


async def _agent_token(store: Store, settings: Settings) -> str:
    """The shared secret for agents, generated once and kept in the database."""
    if settings.agent_token:
        return settings.agent_token
    existing = await store.meta("agent_token")
    if existing:
        return existing
    return await store.meta("agent_token", secrets.token_urlsafe(18)) or ""


async def _guard(request: Request, store: Store, settings: Settings) -> None:
    """Anything that changes state needs the token -- unless it is localhost.

    The dashboard on this machine is trusted (it is the operator's own screen and
    already has the database); a browser on the LAN is not, or anyone on the
    network could start speed tests, wipe the record, or inject probes that look
    like evidence. Reads stay open, which is a privacy choice, not a security one.
    """
    if _is_loopback(request):
        return
    supplied = (
        request.headers.get("x-agent-token")
        or request.query_params.get("token")
        or ""
    )
    expected = await _agent_token(store, settings)
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="agent token required")


def _resolve_since(window: str | float | None, since: float | None) -> float:
    if since is not None:
        return float(since)
    if window is None:
        return time.time() - WINDOWS["1h"]
    if isinstance(window, (int, float)):
        return time.time() - float(window)
    key = str(window).strip().lower()
    if key in WINDOWS:
        seconds = WINDOWS[key]
        return 0.0 if seconds == 0 else time.time() - seconds
    seconds = parse_seconds(key)
    if seconds is None:
        seconds = WINDOWS["1h"]
    return 0.0 if seconds <= 0 else time.time() - seconds


def _enrich_incident(row: dict) -> dict:
    row = dict(row)
    if row.get("ongoing"):
        row["duration_s"] = round(max(0.0, time.time() - float(row["started_at"])), 1)
    row["label"] = OUTAGE_LABELS.get(row.get("scope") or "", row.get("scope") or "Connectivity lost")
    row["failed_targets"] = [t for t in (row.get("failed_targets") or "").split(",") if t]
    if isinstance(row.get("detail"), str):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            row["detail"] = json.loads(row["detail"])
    return row


def create_app(settings: Settings, store: Store, sampler: Sampler) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.connect()
        with contextlib.suppress(Exception):
            await store.prune(settings.retention_days)
        if settings.auto_start:
            await sampler.start()
        try:
            yield
        finally:
            if sampler.running:
                await sampler.stop("shutdown")
            await store.close()

    app = FastAPI(
        title="DropTrace",
        version="0.2.0",
        summary="Continuous connectivity monitoring with outage evidence",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.sampler = sampler

    if STATIC_DIR.is_dir():
        app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": NO_CACHE}
        )

    @app.get("/stats", include_in_schema=False)
    async def stats_page() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "stats.html", headers={"Cache-Control": NO_CACHE}
        )

    @app.get("/agent", include_in_schema=False)
    async def agent_page() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "agent.html", headers={"Cache-Control": NO_CACHE}
        )

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "favicon.svg",
            media_type="image/svg+xml",
            headers={"Cache-Control": NO_CACHE},
        )

    # ------------------------------------------------------------------- api
    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "running": sampler.running,
            "samples": await store.count(),
            "ts": time.time(),
        }

    @app.get("/api/config")
    async def config() -> dict:
        return {
            "settings": settings.to_dict(),
            "windows": WINDOWS,
            "updatable": sorted(UPDATABLE),
        }

    @app.get("/api/status")
    async def status() -> dict:
        payload = sampler.snapshot()
        payload["stored"] = {
            "total": await store.count(),
            "latency": await store.count("latency"),
            "speed": await store.count("speed"),
            "first_ts": await store.first_ts(),
        }
        payload["current_incidents"] = [_enrich_incident(r) for r in await store.current_incidents()]
        return payload

    @app.get("/api/targets")
    async def targets() -> dict:
        return {"targets": sampler.targets_snapshot()}

    @app.get("/api/summary")
    async def summary(
        window: str | None = Query(None, description="1m, 5m, 1h, 6h, 24h, all or seconds"),
        since: float | None = Query(None, description="Explicit epoch lower bound"),
        until: float | None = Query(None, description="Explicit epoch upper bound"),
    ) -> dict:
        start = _resolve_since(window, since)
        end = _resolve_until(until)
        data = await store.summary(start, end)
        data["window"] = window or "1h"
        data["until"] = end
        data["uptime"]["downtime_pct"] = (
            round(100.0 - data["uptime"]["up_pct"], 3)
            if data["uptime"].get("up_pct") is not None
            else None
        )
        return data

    @app.get("/api/series")
    async def series(
        window: str | None = Query(None),
        since: float | None = Query(None),
        until: float | None = Query(None),
        max_points: int = Query(600, ge=10, le=5000),
    ) -> dict:
        start = _resolve_since(window, since)
        end = _resolve_until(until)
        latency = await store.series("latency", start, max_points, end)
        speed = await store.series("speed", start, max_points, end)
        return {
            "since": start,
            "until": end,
            "generated_at": time.time(),
            "raw_cutoff": await store.raw_cutoff(),
            "latency": latency,
            "speed": speed,
            "rounds": await store.round_states(start, max_points, end),
            "incidents": [
                _enrich_incident(r) for r in await store.incidents(start, limit=500, until=end)
            ],
        }

    @app.get("/api/samples")
    async def samples(
        kind: str | None = Query(None, pattern="^(latency|speed)$"),
        limit: int = Query(50, ge=1, le=1000),
    ) -> dict:
        return {"samples": await store.recent(kind, limit)}

    @app.get("/api/rounds")
    async def rounds(limit: int = Query(3, ge=1, le=50)) -> dict:
        return {"rounds": await store.recent_rounds(limit)}

    @app.get("/api/incidents")
    async def incidents(
        window: str | None = Query(None),
        since: float | None = Query(None),
        until: float | None = Query(None),
        limit: int = Query(200, ge=1, le=5000),
    ) -> dict:
        start = _resolve_since(window, since)
        end = _resolve_until(until)
        rows = [_enrich_incident(r) for r in await store.incidents(start, limit, end)]
        # An ongoing outage that started before the window is still relevant.
        ongoing = [
            _enrich_incident(r)
            for r in await store.current_incidents()
            if not end or r["started_at"] <= end
        ]
        return {
            "incidents": rows,
            "ongoing": ongoing,
            "stats": await store.incident_stats(start, end),
        }

    @app.get("/api/export.csv")
    async def export_csv(
        window: str | None = Query(None),
        since: float | None = Query(None),
        until: float | None = Query(None),
        kind: str | None = Query(None, pattern="^(latency|speed)$"),
    ) -> PlainTextResponse:
        start = _resolve_since(window, since) if (window or since) else None
        body = await store.to_csv(start, kind, _resolve_until(until))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return PlainTextResponse(
            body,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="droptrace-samples-{stamp}.csv"'},
        )

    @app.get("/api/incidents.csv")
    async def export_incidents(
        window: str | None = Query(None),
        since: float | None = Query(None),
        until: float | None = Query(None),
    ) -> PlainTextResponse:
        start = _resolve_since(window, since) if (window or since) else 0.0
        body = await store.incidents_to_csv(start, _resolve_until(until))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return PlainTextResponse(
            body,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="droptrace-outages-{stamp}.csv"'},
        )

    # --------------------------------------------------------------- control
    @app.post("/api/control/start")
    async def control_start(request: Request) -> dict:
        await _guard(request, store, settings)
        await sampler.start()
        return sampler.snapshot()

    @app.post("/api/control/stop")
    async def control_stop(request: Request) -> dict:
        await _guard(request, store, settings)
        await sampler.stop("stopped by user")
        return sampler.snapshot()

    @app.post("/api/control/config")
    async def control_config(request: Request, payload: dict = Body(...)) -> dict:
        """Change cadence / payload size / targets while running."""
        await _guard(request, store, settings)
        applied = sampler.update_settings(payload)
        rejected = sorted(set(payload) - set(applied))
        return {"applied": applied, "rejected": rejected, "settings": settings.to_dict()}

    @app.post("/api/probe")
    async def probe(request: Request, kind: str = Query("all", pattern="^(all|latency|speed)$")) -> dict:
        await _guard(request, store, settings)
        if not sampler.running:
            if kind in ("latency", "all"):
                await sampler.run_once("latency")
            if kind in ("speed", "all"):
                await sampler.run_once("speed")
            return {"ran": kind, "running": False}
        sampler.request_run(kind)
        return {"requested": kind, "running": True}

    @app.post("/api/reset")
    async def reset(
        request: Request,
        kind: str | None = Query(None, pattern="^(latency|speed)$"),
        confirm: str | None = Query(None),
    ) -> dict:
        """Delete stored measurements. Requires an explicit confirmation.

        A bare POST here erases the evidence the tool exists to collect, and it is
        one curl away from doing it by accident -- as happened once while testing
        something else. The dashboard sends the flag; a human at a terminal has to
        type it.
        """
        await _guard(request, store, settings)
        if confirm != "yes":
            raise HTTPException(
                status_code=400,
                detail="this deletes stored measurements; repeat with ?confirm=yes",
            )
        removed = await store.clear(kind)
        sampler.counts = {"latency": 0, "speed": 0, "errors": 0, "rounds": 0}
        sampler.last_round = None
        sampler.last_speed = None
        sampler.bytes_used = 0
        # The dashboard reads the last test from these two, so leaving them set
        # shows a speed test whose samples were just deleted.
        if kind in (None, "speed"):
            sampler.last_quick = None
            sampler.last_sustained = None
            sampler.speed_progress = None
        sampler._publish({"type": "reset", "removed": removed})
        return {"removed": removed}

    @app.get("/api/traces")
    async def traces(
        window: str | None = Query(None),
        since: float | None = Query(None),
        until: float | None = Query(None),
        trigger: str | None = Query(None, pattern="^(drop|baseline|manual)$"),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict:
        """The hop lists on record, newest first."""
        start = _resolve_since(window, since)
        end = _resolve_until(until)
        rows = await store.traces(start, end, trigger=trigger, limit=limit)
        return {"traces": rows, "total": await store.count_traces()}

    @app.post("/api/trace")
    async def trace_now(request: Request) -> dict:
        """Trace the path right now, and keep it.

        The sampler traces by itself when a drop begins; this is the "do it now"
        for a person who is watching the connection misbehave and wants the hop
        list before it recovers.
        """
        await _guard(request, store, settings)
        if not settings.trace_host:
            raise HTTPException(status_code=400, detail="tracing is off (TRACE_HOST is empty)")
        result = await trace_path(
            settings.trace_host,
            timeout=settings.trace_timeout,
            hop_timeout=settings.trace_hop_timeout,
            max_hops=settings.trace_max_hops,
        )
        result["trigger"] = "manual"
        result["id"] = await store.add_trace(result)
        sampler.last_trace = result
        sampler._publish({"type": "trace", "trace": result})
        return result

    @app.get("/api/failures")
    async def failures(
        window: str | None = Query(None),
        since: float | None = Query(None),
        until: float | None = Query(None),
        limit: int = Query(200, ge=1, le=5000),
    ) -> dict:
        start = _resolve_since(window, since)
        rows = await store.failures(start, _resolve_until(until), limit)
        return {"since": start, "count": len(rows), "failures": rows}

    @app.get("/api/stats")
    async def stats(
        window: str | None = Query(None),
        since: float | None = Query(None),
        until: float | None = Query(None),
    ) -> dict:
        """Consolidated figures for the statistics page, across both tiers."""
        start = _resolve_since(window, since)
        end = _resolve_until(until) or time.time()
        payload = await store.stats(start, end)
        data = build_stats(payload, start, end)
        data["window"]["label"] = window or "1h"
        return data

    @app.get("/api/devices/live")
    async def devices_live() -> dict:
        """Which vantage points reported in the last minute, right now.

        The statistics page polls this every few seconds so the on/off pill
        tracks the devices themselves rather than the last statistics load: with
        auto refresh off (or set to ten minutes) a device that keeps reporting
        would otherwise still go "off" a minute after the page was drawn.
        """
        now = time.time()
        seen = await store.last_seen()
        return {
            "now": now,
            "devices": [
                {"source": source, "last_seen_ago_s": round(max(0.0, now - ts), 1)}
                for source, ts in sorted(seen.items())
            ],
        }

    # ------------------------------------------------------------ vantage points
    def _device_links(source: str, token: str) -> dict:
        """The two ways to attach this device, ready to copy."""
        from .launcher import local_urls

        urls = [
            url.rstrip("/") for _label, url in local_urls(settings.web_port, settings.bind)
            if "127.0.0.1" not in url
        ]
        base = urls[0] if urls else f"http://127.0.0.1:{settings.web_port}"
        return {
            "browser_agent": f"{base}/agent?token={token}",
            "python_agent": (
                f"python3 -m droptrace agent --server {base} --token {token}"
                f" --source {source}"
            ),
        }

    @app.get("/api/agent/info")
    async def agent_info(request: Request) -> dict:
        """The devices you have added, and the token that controls this server.

        Loopback only: it hands out tokens, and a LAN visitor must not be able to
        read them -- otherwise the protection would be pointless.
        """
        if not _is_loopback(request):
            raise HTTPException(status_code=403, detail="local access only")
        control = await _agent_token(store, settings)
        agents = await store.agents()
        devices = []
        for source, row in sorted(agents.items()):
            token = row.get("token") or ""
            devices.append({
                "source": source,
                "token": token,
                "platform": row.get("platform") or "",
                "agent": row.get("agent") or "",
                "first_seen": row.get("first_seen"),
                "last_seen": row.get("last_seen"),
                "probes": row.get("probes") or 0,
                "links": _device_links(source, token) if token else {},
            })
        return {
            "control_token": control,
            "bind": settings.bind,
            "reachable": settings.bind not in ("127.0.0.1", "localhost"),
            "devices": devices,
        }

    @app.post("/api/agent/devices")
    async def add_device(request: Request, body: dict = Body(...)) -> dict:
        """Add a device (or rotate its token). Localhost only."""
        if not _is_loopback(request):
            raise HTTPException(status_code=403, detail="local access only")
        source = str(body.get("source") or "").strip()
        if not SOURCE_LABEL.match(source) or source == "local":
            raise HTTPException(
                status_code=422,
                detail="use letters, digits, dot, dash or underscore (max 32)",
            )
        token = secrets.token_urlsafe(18)
        device = await store.add_agent(source, token)
        device["links"] = _device_links(source, token)
        return device

    @app.delete("/api/agent/devices/{source}")
    async def remove_device(request: Request, source: str) -> dict:
        """Revoke a device: its token stops working immediately."""
        if not _is_loopback(request):
            raise HTTPException(status_code=403, detail="local access only")
        removed = await store.drop_agent(source)
        if not removed:
            raise HTTPException(status_code=404, detail="no such device")
        return {"removed": source}

    @app.get("/api/agent/whoami")
    async def agent_whoami(token: str = Query("")) -> dict:
        """What this token will file measurements under, for the confirm screen."""
        device = await store.agent_by_token(token)
        if not device:
            raise HTTPException(status_code=401, detail="unknown device token")
        return {
            "source": device["source"],
            "platform": device.get("platform") or "",
            "agent": device.get("agent") or "",
            "probes": device.get("probes") or 0,
        }

    @app.get("/api/agent/targets")
    async def agent_targets() -> dict:
        """The probe list, so an agent measures the same things this machine does."""
        if not sampler.targets:
            sampler.refresh_targets()
        return {
            "targets": [
                {
                    "name": t.name, "role": t.role, "kind": t.kind, "host": t.host,
                    "ports": list(t.ports), "probe_name": t.probe_name,
                    "fact_check": t.fact_check,
                }
                for t in sampler.targets if not t.fact_check
            ]
        }

    @app.post("/api/agent")
    async def agent_ingest(request: Request, body: dict = Body(...)) -> dict:
        """Probes reported by another vantage point on the network.

        Stored verbatim with the agent's label and never allowed to decide this
        machine's verdict: a phone on flaky Wi-Fi must not be able to raise an
        outage here, and equally must not be able to hide one.
        """
        supplied = (
            request.headers.get("x-agent-token")
            or request.query_params.get("token")
            or str(body.get("token") or "")
        )
        device = await store.agent_by_token(supplied)
        if not device:
            raise HTTPException(
                status_code=401,
                detail="unknown device token -- add this device from the dashboard first",
            )
        # The label comes from the token, never from the body: a device cannot
        # file measurements under another device's name, by accident or otherwise.
        source = device["source"]
        probes = body.get("probes")
        if not isinstance(probes, list) or not probes:
            raise HTTPException(status_code=422, detail="no probes")
        if len(probes) > MAX_AGENT_PROBES:
            raise HTTPException(status_code=413, detail="too many probes")

        now = time.time()
        rows = []
        for item in probes:
            if not isinstance(item, dict):
                continue
            try:
                ts = float(item.get("ts") or now)
            except (TypeError, ValueError):
                continue
            if abs(ts - now) > AGENT_CLOCK_SLACK_S:
                continue
            ms = item.get("probe_ms")
            try:
                ms = float(ms) if ms is not None else None
            except (TypeError, ValueError):
                ms = None
            rows.append({
                "ts": ts,
                "kind": "latency",
                "source": source,
                "target": str(item.get("target") or "unknown")[:64],
                "role": str(item.get("role") or "internet")[:16],
                "ok": bool(item.get("ok")),
                "probe_ms": ms,
                "error": (str(item.get("error"))[:200] if item.get("error") else None),
                "round_id": int(item.get("round_id") or int(ts * 1000)),
            })
        if not rows:
            raise HTTPException(status_code=422, detail="no usable probes")
        await store.add_many(rows)
        await store.record_agent(
            source,
            platform=str(body.get("platform") or "")[:40],
            agent=str(body.get("agent") or "")[:16],
            probes=len(rows),
        )
        sampler._publish({"type": "remote", "source": source, "probes": len(rows)})
        return {"stored": len(rows), "source": source}

    # ------------------------------------------------------------------- sse
    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = sampler.subscribe()

        async def stream():
            try:
                yield _sse({"type": "hello", "state": sampler.snapshot(), "ts": time.time()})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(event)
            except asyncio.CancelledError:
                raise
            finally:
                sampler.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _sse(payload: dict) -> str:
    return f"event: {payload.get('type', 'message')}\ndata: {json.dumps(payload)}\n\n"
