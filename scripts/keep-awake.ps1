# Keeps this Windows machine awake while DropTrace is monitoring.
#
# Why this exists separately from the "Keep screen on" button in the dashboard:
# the browser can hold the *display* awake while a page is open and visible, but
# it cannot stop Windows from sleeping the machine, and while the machine sleeps
# nothing is sampled - a gap in the evidence exactly when nobody is watching.
#
# This uses SetThreadExecutionState, the same mechanism a video player uses. The
# request lasts only as long as this script runs: stop it (Ctrl+C) and Windows
# goes back to its normal power plan. Nothing is changed permanently, unlike
# `powercfg /change`, which edits the plan itself and has to be undone by hand.
#
# Usage, from an ordinary PowerShell window (no administrator needed):
#
#   powershell -ExecutionPolicy Bypass -File scripts\keep-awake.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\keep-awake.ps1 -SystemOnly
#
# -SystemOnly keeps the machine awake but lets the display turn off, which is what
# you want if the monitoring box has no need to show anything.
#
# Deliberately ASCII-only: Windows PowerShell 5.1 reads a .ps1 without a byte
# order mark as ANSI, and a stray em dash is then a syntax error.

param(
    [switch]$SystemOnly
)

$signature = @'
using System;
using System.Runtime.InteropServices;

public static class SleepGuard {
    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);

    public const uint ES_CONTINUOUS       = 0x80000000;
    public const uint ES_SYSTEM_REQUIRED  = 0x00000001;
    public const uint ES_DISPLAY_REQUIRED = 0x00000002;
}
'@

Add-Type -TypeDefinition $signature -ErrorAction Stop

$flags = [SleepGuard]::ES_CONTINUOUS -bor [SleepGuard]::ES_SYSTEM_REQUIRED
if (-not $SystemOnly) { $flags = $flags -bor [SleepGuard]::ES_DISPLAY_REQUIRED }

function Release-Guard {
    # ES_CONTINUOUS on its own clears the previous request.
    [void][SleepGuard]::SetThreadExecutionState([SleepGuard]::ES_CONTINUOUS)
}

try {
    $result = [SleepGuard]::SetThreadExecutionState($flags)
    if ($result -eq 0) {
        Write-Host "  could not raise the keep-awake request (SetThreadExecutionState returned 0)" -ForegroundColor Red
        exit 1
    }
    $what = if ($SystemOnly) { "system sleep blocked (display may still turn off)" } else { "system sleep AND display sleep blocked" }
    Write-Host ""
    Write-Host "  DropTrace keep-awake: $what" -ForegroundColor Cyan
    Write-Host "  This lasts while this window is open. Press Ctrl+C to stop."
    Write-Host ""
    while ($true) {
        Start-Sleep -Seconds 30
    }
}
finally {
    Release-Guard
    Write-Host "  keep-awake released - Windows power settings apply again"
}
