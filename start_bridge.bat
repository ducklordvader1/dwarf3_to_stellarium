@echo off
REM Start the dwarf32stellarium bridge (Windows)
REM Usage: start_bridge.bat [DWARF_IP] [extra args...]
REM   DWARF_IP defaults to 192.168.88.1
REM   Any extra args are passed through, e.g.:
REM     start_bridge.bat 192.168.1.10 --lat 48.8566 --lon 2.3522 --alt 35

setlocal
set "SCRIPT_DIR=%~dp0"
set "LOGFILE=%TEMP%\dwarf32stellarium.log"
set "WINTITLE=dwarf32stellarium_bridge"

REM Refuse to start a second instance (match our titled window)
tasklist /v /fi "imagename eq python.exe" 2>nul | find /i "%WINTITLE%" >nul
if not errorlevel 1 (
    echo Bridge already running.
    echo Logs: %LOGFILE%
    goto :eof
)

REM All command-line args are forwarded verbatim to server.py
start "%WINTITLE%" /b cmd /c "python ""%SCRIPT_DIR%server.py"" %* > ""%LOGFILE%"" 2>&1"

echo dwarf32stellarium bridge started.
if "%~1"=="" (echo Dwarf3 IP: 192.168.88.1 ^(default^)) else (echo Dwarf3 IP: %1)
echo Dashboard:      http://localhost:5002
echo Stellarium TCP: localhost:10001
echo Logs:           %LOGFILE%
echo Stop with:      stop_bridge.bat
endlocal
