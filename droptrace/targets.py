"""Probe target discovery.

A "target" is one host we check each round. Targets carry a *role* which is
what turns a pile of failed connects into an actual diagnosis:

``local``
    Something on this machine's own network path (the DNS resolver, the
    default gateway). If these fail too, the problem is local.
``internet``
    A public IP. If only these fail, the LAN is fine and the ISP/upstream is
    not.
``dns``
    Name resolution against the local resolver. A resolver that is up but
    broken looks exactly like "the internet is down" to a browser, so it gets
    its own role and its own incident track.

Auto-detected targets are marked ``guessed``: a router may silently drop all
TCP, so a guess that never answers is stood down at runtime instead of being
reported as an outage. That matters here -- the WSL2 gateway does not answer
TCP on 53/80/443, and a naive gateway probe would blame the LAN on every
single drop.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

# Friendly names so the chart legend reads "cloudflare" instead of "1-1-1-1".
KNOWN_HOSTS = {
    "1.1.1.1": "cloudflare",
    "1.0.0.1": "cloudflare-alt",
    "8.8.8.8": "google",
    "8.8.4.4": "google-alt",
    "9.9.9.9": "quad9",
    "208.67.222.222": "opendns",
    "4.2.2.1": "level3",
}

DEFAULT_PORT = 443


@dataclass(frozen=True)
class Target:
    name: str
    role: str                      # lan | local | internet | dns
    label: str
    kind: str = "tcp"              # tcp | dns
    host: str = ""
    ports: tuple[int, ...] = ()
    probe_name: str = ""           # for kind="dns"; "{random}" = a fresh label
    # For kind="dns": "answer" wants a real record back; "negative" accepts an
    # authoritative negative answer (NXDOMAIN, or NODATA with the zone's SOA),
    # which is the proof that the resolver went upstream instead of answering
    # from its cache.
    expect: str = "answer"
    # Probe at most every N seconds (0 = every round). The uncached DNS queries
    # use this: a random name is a real query to the authoritative servers and
    # has no business being sent 17,000 times a day.
    cadence: float = 0.0
    guessed: bool = False          # auto-detected: may be filtered entirely
    # Corroboration host: not probed every round, only when a round looks like a
    # drop (and once every fact_check_interval to learn the baseline).
    fact_check: bool = False

    def describe(self) -> str:
        if self.kind == "dns":
            return f"{self.probe_name.replace('{random}', '<random>')} via {self.host}"
        ports = "/".join(str(p) for p in self.ports)
        return f"{self.host}:{ports}"


# --------------------------------------------------------------- discovery
def default_gateway() -> str | None:
    """Best-effort default gateway lookup (no external binaries required)."""
    route = Path("/proc/net/route")
    if route.exists():
        try:
            with route.open() as handle:
                next(handle, None)
                for line in handle:
                    parts = line.split()
                    if len(parts) >= 3 and parts[1] == "00000000":
                        packed = struct.pack("<L", int(parts[2], 16))
                        candidate = socket.inet_ntoa(packed)
                        if candidate != "0.0.0.0":
                            return candidate
        except (OSError, ValueError):
            pass

    # Fallback: parse `ip route` when /proc is unavailable (macOS, containers).
    try:
        import subprocess

        output = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout
        match = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", output)
        if match:
            return match.group(1)
    except Exception:  # noqa: BLE001 - discovery must never be fatal
        pass
    return None


def _windows_default_gateway() -> str | None:
    """The *host's* default gateway, read from the Windows route table.

    Only useful under WSL. In NAT mode the distro's own default route points at
    a virtual adapter (172.x) that says nothing about the LAN, so nothing inside
    the VM can reach the real router through a route lookup. The host can: its
    route table row for 0.0.0.0/0 names the router. `route.exe` is a single
    native binary (~90ms), where PowerShell takes seconds, and the table's data
    rows are pure addresses, so no localized headings are parsed.
    """
    if "microsoft" not in Path("/proc/version").read_text(errors="ignore").lower():
        return None
    for binary in ("/mnt/c/Windows/System32/route.exe", "route.exe"):
        if binary.startswith("/") and not Path(binary).exists():
            continue
        try:
            import subprocess

            output = subprocess.run(
                [binary, "print", "-4"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
        except Exception:  # noqa: BLE001 - discovery must never be fatal
            continue
        best: tuple[int, str] | None = None
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gateway = parts[2]
                if gateway in ("0.0.0.0", "On-link", "On-link".lower()):
                    continue
                try:
                    metric = int(parts[4])
                except ValueError:
                    metric = 9999
                if best is None or metric < best[0]:
                    best = (metric, gateway)
        if best is not None:
            return best[1]
    return None


def discover_lan_gateway(explicit: str = "") -> str | None:
    """The address that has to cross the wire to reach the router.

    This is the one probe that can honestly say "the LAN was fine, so the drop
    is upstream". Under WSL the distro's default route cannot be trusted for
    that (it points at the NAT gateway), so the host's own route table wins.
    """
    if explicit.strip():
        return explicit.strip()
    return _windows_default_gateway() or default_gateway()


def nameservers(path: str = "/etc/resolv.conf") -> list[str]:
    """Nameservers from resolv.conf, in order."""
    found: list[str] = []
    try:
        with open(path) as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    if parts[1] not in found:
                        found.append(parts[1])
    except OSError:
        pass
    return found


def _target_name_for(host: str) -> str:
    if host in KNOWN_HOSTS:
        return KNOWN_HOSTS[host]
    if re.fullmatch(r"[\d.]+", host):
        return host.replace(".", "-")
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "target"


def parse_host_port(entry: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    entry = entry.strip()
    if entry.startswith("["):  # [::1]:443
        host, _, rest = entry.partition("]")
        host = host[1:]
        return host, int(rest.lstrip(":") or default_port)
    if entry.count(":") == 1:
        host, _, port = entry.partition(":")
        if port.isdigit():
            return host, int(port)
    return entry, default_port


def build_targets(settings: Settings) -> list[Target]:
    """Assemble the probe list, de-duplicating by name and host:port."""
    targets: list[Target] = []
    seen: set[str] = set()

    def add(target: Target) -> None:
        key = f"{target.kind}:{target.host}:{target.ports}:{target.probe_name}"
        if key in seen or not target.host:
            return
        seen.add(key)
        targets.append(target)

    # --- local hop: the DNS resolver ---------------------------------------
    resolvers = nameservers()
    if settings.probe_resolver and resolvers:
        add(
            Target(
                name="resolver",
                role="local",
                label=f"DNS resolver ({resolvers[0]})",
                host=resolvers[0],
                ports=(53,),
                guessed=True,
            )
        )

    # --- the LAN hop: the router ------------------------------------------
    # Only this role can claim "the LAN was fine": the resolver above may be a
    # proxy inside this very machine, which proves nothing about the wire.
    if settings.probe_gateway:
        gateway = discover_lan_gateway(settings.lan_gateway)
        if gateway:
            add(
                Target(
                    name="gateway",
                    role="lan",
                    label=f"Router ({gateway})",
                    host=gateway,
                    ports=(53, 80, 443),
                    guessed=True,
                )
            )

    # --- internet targets ---------------------------------------------------
    for entry in settings.public_targets.split(","):
        if not entry.strip():
            continue
        host, port = parse_host_port(entry)
        add(
            Target(
                name=_target_name_for(host),
                role="internet",
                label=f"{KNOWN_HOSTS.get(host, host)} ({host})",
                host=host,
                ports=(port,),
            )
        )

    # --- corroboration hosts, on other networks -----------------------------
    # Deliberately not part of the per-round verdict: they exist to confirm a
    # suspected drop, so probing them ten thousand times a day buys nothing.
    #
    # Not marked `guessed`, which would be wrong in a subtle way: probation stands
    # a target down after a few failures, and these are only ever probed when
    # things are already failing -- so one real outage would permanently retire
    # the whole pool as "never answers". The pool is configuration, not a guess.
    for entry in settings.fact_check_targets.split(","):
        if not entry.strip():
            continue
        host, port = parse_host_port(entry)
        add(
            Target(
                name=_target_name_for(host),
                role="internet",
                label=f"{KNOWN_HOSTS.get(host, host)} ({host})",
                host=host,
                ports=(port,),
                fact_check=True,
            )
        )

    # --- user supplied extras: name=host:port ------------------------------
    for entry in settings.extra_targets.split(","):
        if not entry.strip():
            continue
        name, _, address = entry.strip().partition("=")
        if not address:
            name, address = "", entry.strip()
        host, port = parse_host_port(address)
        add(
            Target(
                name=name.strip() or _target_name_for(host),
                role="internet",
                label=f"{name.strip() or host} ({host})",
                host=host,
                ports=(port,),
            )
        )

    # --- DNS health, and the same question to other resolvers ----------------
    # One pair of probes per resolver: the cached name (is the resolver itself
    # answering?) and, at a slower cadence, a name nobody has asked for (can it
    # still reach the authoritative servers?). Only the machine's own resolver
    # feeds the dns verdict; the others are the comparison that says whether the
    # fault is this resolver or name resolution on the whole line.
    if settings.dns_probe_name:
        for key, host, port, local in _dns_resolvers(settings, resolvers):
            add(
                Target(
                    name=key,
                    role="dns" if local else "dns-public",
                    label=(
                        f"DNS lookup {settings.dns_probe_name} via {host}"
                        if local else f"DNS {KNOWN_HOSTS.get(host, host)} ({host})"
                    ),
                    kind="dns",
                    host=host,
                    ports=(port,),
                    probe_name=settings.dns_probe_name,
                    guessed=local,
                )
            )
            if settings.dns_cache_bust_interval > 0:
                add(
                    Target(
                        name=f"{key}-upstream",
                        role="dns-upstream",
                        label=(
                            f"DNS uncached lookup via {host}"
                            if local else f"DNS uncached {KNOWN_HOSTS.get(host, host)} ({host})"
                        ),
                        kind="dns",
                        host=host,
                        ports=(port,),
                        probe_name=f"probe-{{random}}.{settings.dns_probe_name}",
                        expect="negative",
                        cadence=settings.dns_cache_bust_interval,
                        guessed=local,
                    )
                )

    return targets


def _dns_resolvers(
    settings: Settings, resolvers: list[str]
) -> list[tuple[str, str, int, bool]]:
    """``(key, host, port, is_the_machine's_own)`` for every resolver to probe.

    The machine's own resolver comes first and keeps the name ``dns``, so the
    history recorded before this existed still lines up with the verdict.
    """
    entries: list[tuple[str, str, int, bool]] = []
    local = resolvers[0] if resolvers else ""
    if local:
        entries.append(("dns", local, 53, True))
    for entry in settings.dns_servers.split(","):
        if not entry.strip():
            continue
        host, port = parse_host_port(entry, default_port=53)
        if not host or any(host == known for _key, known, _port, _local in entries):
            continue
        entries.append((f"dns-{_target_name_for(host)}", host, port, False))
    return entries


def is_private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False
