@echo off
setlocal
title DropTrace - internet stability monitor

rem Double-click this file to start DropTrace in WSL and open the dashboard.
rem Everything after this point is controllable from the web page itself.

rem --- settings you may want to change -------------------------------------
set "DISTRO=Ubuntu"
set "PORT=8777"
rem 0.0.0.0 so the phone can report as a vantage point; 127.0.0.1 keeps it local.
set "BIND=0.0.0.0"
rem Project path as seen from inside WSL:
set "WSLDIR=/mnt/f/projetos/stakfin/opencode-harness/contexts/alrohe/droptrace"
rem ------------------------------------------------------------------------

echo.
echo   DropTrace - starting in WSL (%DISTRO%) on port %PORT%
echo   Dashboard: http://127.0.0.1:%PORT%/
echo   From the phone: http://^<this-pc-ip^>:%PORT%/   (BIND=%BIND%)
echo.
echo   Leave this window open while you monitor. Close it (or press Ctrl+C
echo   here) to stop sampling.
echo.

rem Open the dashboard once the server answers (max ~30s), in a helper window.
start "" /min cmd /c "for /l %%i in (1,1,60) do (curl -s -o nul http://127.0.0.1:%PORT%/api/health && (start "" http://127.0.0.1:%PORT%/ & exit) || timeout /t 1 /nobreak >nul)"

rem Run the server in the foreground so this window shows the log.
wsl.exe -d %DISTRO% --cd "%WSLDIR%" -e python3 -m droptrace serve --web-port %PORT% --bind %BIND% --db data/droptrace.db

echo.
echo   DropTrace stopped.
pause
