@echo off
REM Stop the dwarf32stellarium bridge (Windows)
setlocal
set "WINTITLE=dwarf32stellarium_bridge"

taskkill /f /fi "windowtitle eq %WINTITLE%*" >nul 2>&1
if not errorlevel 1 (
    echo dwarf32stellarium bridge stopped.
) else (
    echo No running bridge found ^(or already stopped^).
)
endlocal
