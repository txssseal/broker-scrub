#!/usr/bin/env bash
# Drain up to daily_cap deletion demands. flock => safe to run hourly.
# Install (from anywhere):
#   ( crontab -l 2>/dev/null; echo "0 * * * * $(command -v brokerscrub) send --live" ) | crontab -
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec flock -n /tmp/brokerscrub-send.lock brokerscrub send --live
