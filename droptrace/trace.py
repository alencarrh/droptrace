"""Path tracing: where the traffic actually stops.

A verdict says *that* the connection dropped; a hop list says *where*. That is
the difference between "my internet is unstable" and "the traffic dies at
100.64.0.1, the provider's edge, while the router one hop away keeps
answering" -- which is the sentence an ISP has to act on.

Tracing an outage is also the most fragile thing this tool does: the path is
broken by definition, resolvers may be dead, and a trace that hangs while the
fast probe cadence is running would cost the boundaries of the very drop it is
supposed to explain. So it is always fired as a *background* task with a hard
timeout, at most once per cooldown, and never with DNS (``-d``/``-n``): names
cannot be resolved in the middle of the event being investigated.

Three tracers are understood, in this order:

``tracepath``
    Linux, part of iputils, works **unprivileged** (UDP plus ``IP_RECVERR``,
    no raw socket). This is the tool the roadmap asked for.
``tracert``
    The Windows one, used when this runs under WSL. It traces from the *host*,
    which is the machine whose connection is actually being complained about --
    and in mirrored networking it is the same path the probes take.
``traceroute``
    The classic fallback for a Linux box without iputils.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from pathlib import Path

# Where Windows keeps tracert, seen from inside WSL.
WINDOWS_TRACERT = "/mnt/c/Windows/System32/tracert.exe"

# One hop line from tracepath:
#   1:  _gateway                          0.512ms
#   2?: 192.168.1.1                        1.102ms asymm  1
#   4:  no reply
TRACEPATH_HOP = re.compile(
    r"^\s*(?P<ttl>\d+)\??:\s+(?P<host>\S+)(?:\s+(?P<rest>.*))?$"
)

# One hop line from tracert:
#   1    <1 ms    <1 ms    <1 ms  192.168.1.1
#   2     *        *        *     Request timed out.
TRACERT_LINE = re.compile(r"^\s*(?P<ttl>\d+)\s+(?P<rest>.*\S)\s*$")
TRACERT_MS = re.compile(r"([\d.]+)\s*ms")
TRACERT_NOISE = {"request", "timed", "out", "out."}


def find_tracer() -> tuple[str, list[str]] | None:
    """The first tracer this machine has, as ``(name, argv prefix)``.

    Discovery is cheap and happens per trace, so installing iputils while the
    sampler runs is picked up by the next event.
    """
    for name, argv in (
        ("tracepath", ["tracepath"]),
        ("tracert", [WINDOWS_TRACERT]),
        ("traceroute", ["traceroute"]),
    ):
        if name == "tracert":
            if Path(WINDOWS_TRACERT).exists():
                return name, argv
            continue
        found = shutil.which(name)
        if found:
            return name, [found]
    return None


def command_for(name: str, argv: list[str], host: str, max_hops: int,
                hop_timeout: float) -> list[str]:
    """The command line, per tracer, always without DNS."""
    if name == "tracepath":
        # -n: numeric only, -m: max hops, -l: pktlen (tracepath needs a payload
        # size, its default is fine but pinning it keeps runs comparable).
        return [*argv, "-n", "-m", str(max_hops), "-l", "1280", host]
    if name == "tracert":
        # -d: numeric only, -h: max hops, -w: per-hop timeout in milliseconds.
        return [*argv, "-d", "-h", str(max_hops), "-w", str(int(hop_timeout * 1000)), host]
    return [*argv, "-n", "-m", str(max_hops), "-w", f"{hop_timeout:g}", host]


def parse_tracepath(text: str) -> list[dict]:
    """Parse ``tracepath`` output into hops, first line of each TTL wins."""
    hops: dict[int, dict] = {}
    for line in text.splitlines():
        match = TRACEPATH_HOP.match(line)
        if not match:
            continue
        ttl = int(match.group("ttl"))
        host = match.group("host")
        rest = (match.group("rest") or "").strip()
        note = ""
        rtt: float | None = None
        if host == "no":
            # " 4:  no reply" -- the token before the note is "no", "reply".
            host = ""
            note = "no reply"
        elif host in ("[LOCALHOST]", "localhost"):
            host = "localhost"
        rtt_match = re.match(r"([\d.]+)ms", rest)
        if rtt_match:
            rtt = float(rtt_match.group(1))
        if "asymm" in rest:
            asymm = re.search(r"asymm\s+(\d+)", rest)
            note = f"asymm {asymm.group(1)}" if asymm else "asymm"
        if "reached" in rest:
            note = (note + " reached").strip()
        hops.setdefault(
            ttl, {"ttl": ttl, "host": host, "rtt_ms": rtt, "note": note}
        )
    return [hops[ttl] for ttl in sorted(hops)]


def parse_tracert(text: str) -> list[dict]:
    """Parse ``tracert`` output into hops.

    ``Request timed out`` lines are kept with an empty host: a silent router in
    the middle is normal (measured here: hop 16 of 18) and must stay visible as
    a gap, not be quietly dropped into a shorter-looking path.
    """
    hops: list[dict] = []
    for line in text.splitlines():
        match = TRACERT_LINE.match(line)
        if not match:
            continue
        ttl = int(match.group("ttl"))
        rest = match.group("rest")
        times = TRACERT_MS.findall(rest)
        # Strip the RTT columns and the stars; whatever is left is either the
        # address or the "Request timed out." message.
        leftover = TRACERT_MS.sub(" ", rest).replace("*", " ").replace("<", " ").split()
        host = "" if any(word.lower() in TRACERT_NOISE for word in leftover) else (
            leftover[0] if leftover else ""
        )
        hops.append(
            {
                "ttl": ttl,
                "host": host,
                "rtt_ms": min(float(t) for t in times) if times else None,
                "note": "" if host else "no reply",
            }
        )
    return hops


def parse_traceroute(text: str) -> list[dict]:
    """Parse classic ``traceroute`` output (``1  host (ip)  1.2 ms``)."""
    hops: list[dict] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not match:
            continue
        ttl = int(match.group(1))
        rest = match.group(2).strip()
        times = TRACERT_MS.findall(rest)
        host = ""
        if rest and not rest.startswith("*"):
            token = rest.split()[0]
            host = token
        hops.append(
            {
                "ttl": ttl,
                "host": host,
                "rtt_ms": min(float(t) for t in times) if times else None,
                "note": "" if host else "no reply",
            }
        )
    return hops


PARSERS = {
    "tracepath": parse_tracepath,
    "tracert": parse_tracert,
    "traceroute": parse_traceroute,
}


def summarize(hops: list[dict], target: str, max_hops: int, tracer: str,
              duration_ms: float, error: str | None = None) -> dict:
    """The one-line verdict of a trace, plus the hops it came from."""
    answered = [hop for hop in hops if hop.get("host")]
    last = answered[-1]["host"] if answered else ""
    # tracert prints the destination as the final hop when it arrives; the
    # first hop is the router either way, so an empty list means nothing.
    reached = bool(last) and last == target
    if not reached and error is None and not hops:
        error = "the tracer produced no hops"
    return {
        "ts": time.time(),
        "trigger": "",
        "host": target,
        "tracer": tracer,
        "reached": reached,
        "hops": len(hops),
        "max_hops": max_hops,
        "answered": len(answered),
        "last_hop": last,
        "duration_ms": round(duration_ms, 1),
        "error": error,
        "hop_list": hops,
    }


async def trace_path(
    host: str,
    *,
    timeout: float = 15.0,
    hop_timeout: float = 0.7,
    max_hops: int = 20,
    tracer: tuple[str, list[str]] | None = None,
) -> dict:
    """Trace the path to ``host`` and never raise, never hang.

    Returns a trace dict ready to be stored: ``reached``, ``last_hop``,
    ``hops`` (how far it got), ``answered`` (how many hops spoke) and the hop
    list. An unavailable tracer or a killed run is reported in ``error`` with
    the partial hops it managed to print before the cap, which are still worth
    recording: "it got as far as hop 4" is the evidence.
    """
    started = time.perf_counter()
    found = tracer or find_tracer()
    if found is None:
        return summarize(
            [], host, max_hops, "none", 0.0,
            "no tracer installed (tracepath / tracert / traceroute)",
        )
    name, argv = found
    command = command_for(name, argv, host, max_hops, hop_timeout)
    parse = PARSERS[name]

    process = None
    reader: asyncio.Task | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Read the pipe in its own task rather than through communicate(): when
        # the cap fires, communicate() is cancelled and the output buffered by
        # the tracer so far is lost -- and those are exactly the hops worth
        # keeping ("it got as far as hop 4").
        reader = asyncio.create_task(process.stdout.read())  # type: ignore[union-attr]
        try:
            await asyncio.wait_for(process.wait(), timeout)
            error = None
        except asyncio.TimeoutError:
            process.kill()
            error = f"trace capped at {timeout:g}s"
        raw = b""
        if reader is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                raw = await asyncio.wait_for(reader, 5)
    except OSError as exc:
        return summarize(
            [], host, max_hops, name, (time.perf_counter() - started) * 1000,
            f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - a trace must never break a round
        return summarize(
            [], host, max_hops, name, (time.perf_counter() - started) * 1000,
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        # Cancelled (a stop, a shutdown) must not leave a tracer running: the
        # child would outlive the process that asked for the answer.
        if reader is not None and not reader.done():
            reader.cancel()
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()

    text = raw.decode(errors="replace")
    duration = (time.perf_counter() - started) * 1000
    trace = summarize(parse(text), host, max_hops, name, duration, error)
    if error is None and not trace["hops"]:
        trace["error"] = "the tracer produced no hops"
    return trace
