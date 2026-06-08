#!/bin/bash
# kill_thoughtmachine.sh — forcefully stop all ThoughtMachine processes
fuser -k 5173/tcp 5174/tcp 5175/tcp 5176/tcp 5177/tcp 8000/tcp 2>/dev/null
sleep 1
echo "All ThoughtMachine processes stopped."
