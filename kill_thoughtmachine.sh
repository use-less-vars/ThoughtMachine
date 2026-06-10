#!/bin/bash
#===============================================================================
# kill_thoughtmachine.sh
#
#  ⚠ SYNCED with kill_thoughtmachine.bat — keep in agreement.
#  ⚠ If you edit this file, mirror the same change in the batch file.
#===============================================================================
# Forcefully stop all ThoughtMachine processes
fuser -k 5173/tcp 5174/tcp 5175/tcp 5176/tcp 5177/tcp 8000/tcp 2>/dev/null
sleep 1
echo "All ThoughtMachine processes stopped."
