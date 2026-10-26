#!/bin/bash
# Start the dwarf32stellarium bridge as a detached daemon
# Usage: ./start_bridge.sh [DWARF_IP] [extra args passed to server.py]
#   DWARF_IP defaults to 192.168.88.1
#   e.g. ./start_bridge.sh 192.168.1.10 --lat 48.8566 --lon 2.3522 --alt 35

PIDFILE=/tmp/dwarf32stellarium.pid
LOGFILE=/tmp/dwarf32stellarium.log
SERVER="$(cd "$(dirname "$0")" && pwd)/server.py"

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Bridge already running as PID $PID"
        echo "Logs: tail -f $LOGFILE"
        exit 0
    fi
fi

nohup python3 "$SERVER" "$@" > "$LOGFILE" 2>&1 &
BRIDGE_PID=$!
disown $BRIDGE_PID
echo $BRIDGE_PID > "$PIDFILE"
echo "dwarf32stellarium bridge started as PID $BRIDGE_PID"
echo "Dashboard:      http://localhost:5002"
echo "Stellarium TCP: localhost:10001"
echo "Logs:           tail -f $LOGFILE"
