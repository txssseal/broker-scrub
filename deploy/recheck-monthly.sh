#!/usr/bin/env bash
# Monthly: reopen confirmed deletions older than 90 days; the send job mails them.
# Install: ( crontab -l 2>/dev/null; echo "0 9 1 * * $HOME/broker-scrub/deploy/recheck-monthly.sh" ) | crontab -
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec flock -n /tmp/brokerscrub-recheck.lock brokerscrub recheck --reopen
