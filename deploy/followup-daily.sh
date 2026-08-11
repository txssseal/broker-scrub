#!/usr/bin/env bash
# Daily: fire the firm TDPSA reply at any NEW portal-deflecting broker. Deduped
# in-app (each broker gets exactly one follow-up ever), so it's safe to run daily
# and safe to re-run. flock guards against overlap with a still-running send.
# Install: ( crontab -l 2>/dev/null; echo "0 12 * * * $HOME/broker-scrub/deploy/followup-daily.sh" ) | crontab -
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec flock -n /tmp/brokerscrub-followup.lock brokerscrub followup --live
