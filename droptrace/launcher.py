"""Open the dashboard in the user's browser.

``webbrowser`` is unreliable under WSL: there is no registered browser and
``xdg-open`` is usually absent, so it silently does nothing. Since WSL has
Windows interop available, that path is handled explicitly and first.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser

_WINDOWS_CMD = "/mnt/c/Windows/System32/cmd.exe"


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version") as handle:
            return "microsoft" in handle.read().lower()
    except OSError:
        return False


def _spawn(command: list[str]) -> bool:
    """Start a detached command; True if the binary exists and launched."""
    executable = shutil.which(command[0]) or (
        command[0] if os.path.isabs(command[0]) and os.path.exists(command[0]) else None
    )
    if not executable:
        return False
    try:
        subprocess.Popen(
            [executable, *command[1:]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def open_browser(url: str) -> str:
    """Best-effort open of ``url``. Returns a label saying what was used."""
    if os.environ.get("DROPTRACE_NO_BROWSER"):
        return "skipped (DROPTRACE_NO_BROWSER is set)"

    if is_wsl():
        if _spawn(["wslview", url]):
            return "wslview"
        # cmd.exe is present on any normal WSL install with interop enabled.
        for candidate in (
            [_WINDOWS_CMD, "/c", "start", "", url],
            ["cmd.exe", "/c", "start", "", url],
            ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"],
            ["explorer.exe", url],
        ):
            if _spawn(candidate):
                return "Windows browser"

    try:
        if webbrowser.open(url):
            return "default browser"
    except Exception:  # noqa: BLE001 - opening a browser must never be fatal
        pass
    return "not opened"


def local_urls(port: int, bind: str) -> list[tuple[str, str]]:
    """URLs the dashboard is reachable at, as (label, url) pairs.

    ``0.0.0.0`` is not a usable address in a browser, so when bound to a
    wildcard this works out the real addresses instead of printing something the
    user cannot open -- which matters most when they are trying to reach it from
    another device.
    """
    if bind not in ("0.0.0.0", "::", ""):
        return [("local", f"http://{bind}:{port}/")]

    addresses: list[str] = []
    # The address the routing table would use to leave the machine. Under WSL2
    # NAT this is the VM's own address; under mirrored networking it is the
    # Windows LAN address, which is exactly what another device needs.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("1.1.1.1", 80))
            addresses.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass

    # Plus anything the hostname resolves to, which on a machine with both
    # Ethernet and Wi-Fi can be a different interface than the one above.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.append(info[4][0])
    except OSError:
        pass

    urls = [("local", f"http://127.0.0.1:{port}/")]
    seen = {"127.0.0.1"}
    for address in addresses:
        if address in seen or address.startswith("127."):
            continue
        seen.add(address)
        urls.append(("network", f"http://{address}:{port}/"))
    return urls


def wait_and_open(
    url: str,
    health_url: str | None = None,
    timeout: float = 30.0,
    log=print,
) -> threading.Thread:
    """Wait for the server to answer, then open the browser. Returns the thread."""
    health_url = health_url or f"{url.rstrip('/')}/api/health"

    def worker() -> None:
        deadline = time.time() + timeout
        ready = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=1) as response:
                    if response.status == 200:
                        ready = True
                        break
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.25)
        if not ready:
            log(f"  (server not up after {timeout:g}s — open {url} manually)")
            return
        result = open_browser(url)
        if result == "not opened":
            log(f"  could not open a browser automatically — open {url}")
        else:
            log(f"  opened in {result}: {url}")

    thread = threading.Thread(target=worker, daemon=True, name="droptrace-browser")
    thread.start()
    return thread
