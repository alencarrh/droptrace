# Reaching DropTrace from your phone (or another device)

Everything here was checked against this machine: **WSL 2.3.26 on Windows 11 25H2
(build 26200)**, dashboard on port **8777**, LAN addresses **192.168.1.51 (Wi-Fi)**
and **192.168.1.50 (Ethernet)**.

Quick answer if you remember nothing else:

1. Put `networkingMode=mirrored` + `firewall=false` in `C:\Users\<you>\.wslconfig`
2. Run `wsl --shutdown` from PowerShell, wait ~10 s, reopen WSL
3. Start DropTrace with `--bind 0.0.0.0`
4. On your phone (same Wi-Fi): **http://192.168.1.51:8777/**

> **Status:** `C:\Users\<you>\.wslconfig` is written (2026-09-15) with both
> settings. It takes effect at the next `wsl --shutdown`, which stops every
> distro — including the one DropTrace is running in, so restart it afterwards
> (`start.bat`, or `make serve`). Until then WSL is still in NAT mode.

---

## Why `--bind 0.0.0.0` alone is not enough

WSL2 runs in its own virtual machine behind its own NAT. The VM has a private
address (`172.20.x.x`) that your phone cannot route to, so a port opened inside
WSL is invisible to the LAN no matter what you bind to. Windows has to bridge the
two. There are two ways, and both need one-time setup on the Windows side.

Check where you are at any time:

```bash
hostname -I                                  # the WSL address (172.20.x.x under NAT)
ip route show default                         # the gateway WSL is using
```

---

## Option A — mirrored networking (recommended)

WSL then shares the Windows network interfaces, so the port you open inside WSL is
the port the LAN sees, and the address your phone uses is simply your Windows
address.

> **Step-by-step with every command and its expected output, written for an
> Administrator PowerShell: [WSL-MIRRORED-SETUP.md](WSL-MIRRORED-SETUP.md).** The
> summary below is the short version.

**1. Create `C:\Users\<you>\.wslconfig`** (it does not exist yet by default):

```ini
[wsl2]
networkingMode=mirrored
firewall=false
```

`firewall=false` disables the Hyper-V firewall filtering for WSL. Windows
Firewall is enabled on all three profiles here, so without it the LAN is refused.
The alternative, if you would rather keep the firewall active, is to leave
`firewall` at its default and add an inbound rule from an administrator
PowerShell instead:

```powershell
New-NetFirewallRule -DisplayName "DropTrace 8777" -Direction Inbound `
  -Protocol TCP -LocalPort 8777 -Action Allow
```

Requires Windows 11 22H2+ for `networkingMode`; you are well past that.

**2. Restart WSL.** From PowerShell:

```powershell
wsl --shutdown
```

Wait about 10 seconds (WSL needs to fully stop before it re-reads the config),
then reopen your WSL terminal.

> **This terminates everything running in WSL** — any containers, services and
> the DropTrace instance itself. Warned.

**3. Start DropTrace bound to all interfaces:**

```bash
cd /mnt/f/projetos/stakfin/opencode-harness/contexts/alrohe/droptrace
python3 -m droptrace serve --bind 0.0.0.0
```

The banner prints every address it is reachable at, so you do not have to guess:

```
  → local    http://127.0.0.1:8777/
  → network  http://192.168.1.51:8777/   <- from another device on this network
```

**4. On your phone** (connected to the same Wi-Fi): **http://192.168.1.51:8777/**

If that does not load, try **http://192.168.1.50:8777/** (the Ethernet address) —
with both interfaces up, Windows may prefer the other one.

### Mirrored networking, provable attribution

Mirrored mode is not only about the phone port. In NAT mode the only "local"
addresses DropTrace can reach are WSL's own — the virtual gateway `172.20.x.1` and
the DNS proxy `10.255.255.254`, which sits on **loopback**. Neither crosses the
cable to the router, so a drop could not be proven to be upstream of your own
network: it was reported as *Internet unreachable (no router evidence)* even
though the drop was real.

In mirrored mode the distro holds the Windows interfaces and routes, so the
router (here `192.168.1.1`) is directly probeable and becomes the `lan` target.
That is what makes an *ISP or upstream down (router answered)* verdict something
you can show your provider. DropTrace also reads the host's route table itself
(`route.exe print -4`, ~90ms) even under NAT, so the router target works in both
modes; mirrored mode additionally removes the NAT layer from the measurement
path, which is one less thing between the probe and the wire.

Note that you cannot test the LAN path *from the host itself*: in mirrored mode
Windows and WSL hold the same address, and a connection the host makes to its own
`192.168.1.50` is delivered locally rather than handed to the WSL listener — so
`Test-NetConnection 192.168.1.50 -Port 8777` can fail while the phone works fine.
`http://localhost:8777/` from Windows is the desktop path, and the test that
matters is the phone.

Check it took effect inside WSL:

```bash
ip route show default        # mirrored: your real gateway, e.g. 192.168.1.1
cat /etc/resolv.conf         # mirrored: your real DNS, not 10.255.255.254
curl -s localhost:8777/api/targets | head -30   # look for the "lan" role row
```

---

## Option B — a port forward (no WSL config change)

Use this if mirrored mode disturbs Docker, a VPN, or anything else, and you would
rather not change WSL networking globally. From an **administrator** PowerShell:

```powershell
$wslIp = (wsl.exe hostname -I).Trim().Split()[0]
netsh interface portproxy add v4tov4 listenport=8777 listenaddress=0.0.0.0 `
  connectport=8777 connectaddress=$wslIp

New-NetFirewallRule -DisplayName "DropTrace 8777" -Direction Inbound `
  -Protocol TCP -LocalPort 8777 -Action Allow
```

DropTrace can stay on its default loopback bind with this approach if you prefer.

**A NAT'd WSL gets a new address on every reboot**, so the forward goes stale.
After restarting WSL, re-run it — or reset it with:

```powershell
netsh interface portproxy delete v4tov4 listenport=8777 listenaddress=0.0.0.0
```

Inspect what is currently forwarded with `netsh interface portproxy show all`.

---

## Verifying, step by step

Narrow the problem down from the inside out rather than guessing:

```bash
# 1. Is it up at all? (on the machine running it)
curl -sS http://127.0.0.1:8777/api/health

# 2. Is it listening beyond loopback? You want 0.0.0.0:8777 or <lan-ip>:8777.
ss -ltn | grep 8777
```

Then from **Windows** (PowerShell, not WSL):

```powershell
curl.exe -sS http://localhost:8777/api/health      # forwarding works?
curl.exe -sS http://192.168.1.51:8777/api/health    # the LAN address works?
```

If `localhost` works but the LAN address does not, it is the firewall. If neither
works, it is the bind or the forwarding. Only then try the phone.

From the **phone**, open `http://192.168.1.51:8777/api/health` first — a small JSON
response tells you whether it is a connectivity problem or a page problem.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ss -ltn` shows `127.0.0.1:8777` | You forgot `--bind 0.0.0.0`, or `--bind` did not take effect |
| Works on Windows `localhost`, not on the LAN address | Firewall: add the inbound rule or set `firewall=false` |
| Nothing works after editing `.wslconfig` | WSL did not restart: `wsl --shutdown`, wait ~10 s, reopen |
| Worked, then stopped after a reboot | NAT gave WSL a new address and the port forward is stale — re-run it, or switch to mirrored mode |
| Phone loads nothing at all | Phone on a different network/VLAN, or client isolation enabled on the router/guest Wi-Fi |
| Page loads but is stale or empty | The dashboard needs `GET /api/series`; check the phone is not on a captive-portal Wi-Fi |
| Port already in use on start | Another instance is running: `make serve PORT=8778` (`PORT=8778 ./start.sh`) |
| `Another DropTrace is already sampling …` | Two samplers on one database would double every reading, so the second refuses to start. Stop the first (the pid is named), or use `--db` to point it somewhere else |

Router note: many guest Wi-Fi networks enable "client isolation", which blocks
device-to-device traffic entirely. If your phone is on a guest network, that is
the wall, not this configuration.

---

## Security — read this before leaving it exposed

**DropTrace has no authentication.** Anyone who can reach the port can:

- read your connection history and every recorded outage,
- **pause or stop sampling**,
- press **Speed test**, and a sustained test now moves **hundreds of megabytes** —
  on a metered or capped connection, an open port is a way to burn your allowance,
- **delete the stored results** (`Reset`), destroying the very evidence you are
  collecting.

Practical guidance:

- Prefer `--bind 0.0.0.0` only while you are actually checking, then go back to the
  default loopback bind.
- Keep it off guest networks and away from anything you do not control.
- If you want it reachable long-term, a shared-token option is the missing piece —
  ask and it can be added (token in the URL so the phone is still one tap).
- Windows Firewall rules added above can be removed with
  `Remove-NetFirewallRule -DisplayName "DropTrace 8777"`.

---

## Undoing the changes

Mirrored networking — delete `C:\Users\<you>\.wslconfig` (or remove the
`[wsl2]` lines) and run `wsl --shutdown` again to return to NAT. Attribution then
depends on the router target alone, which still works (it is read from the host's
route table), but the NAT layer is back in the measurement path; without a router
probe answering, drops are reported as *Internet unreachable (no router
evidence)* rather than being blamed on the ISP.

Port forward:

```powershell
netsh interface portproxy delete v4tov4 listenport=8777 listenaddress=0.0.0.0
Remove-NetFirewallRule -DisplayName "DropTrace 8777"
```

Loopback-only is the default, so simply dropping `--bind 0.0.0.0` from your start
command restores it.
