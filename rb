#!/usr/bin/env bash
#
# rb - run any custom ROM build command and stream it to the rombot log.
#
# Because every ROM has its own build command (brunch / mka bacon / m derp / ax -br
# ...), rb does not care what you pass it. Just put your normal command after rb:
#
#     rb brunch fogos
#     rb mka bacon
#     rb m derp
#     rb ax -br
#
# It writes a START marker, tees the whole build output into the log that
# rombot watches, then writes an END marker with the real exit code.
#
set -o pipefail

ROMBOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="${ROMBOT_LOG:-$ROMBOT_DIR/build.log}"

if [ "$#" -eq 0 ]; then
    echo "usage: rb <your build command...>" >&2
    echo "e.g.   rb brunch fogos" >&2
    exit 2
fi

# Record the command line for the bot
printf '###ROMBOT_START %s %s\n' "$(date +%s)" "$*" >> "$LOG"

# stdbuf keeps output line-buffered so the bot sees progress live (not in 4 KB chunks)
if command -v stdbuf >/dev/null 2>&1; then
    stdbuf -oL -eL "$@" 2>&1 | tee -a "$LOG"
else
    "$@" 2>&1 | tee -a "$LOG"
fi
code=${PIPESTATUS[0]}

printf '###ROMBOT_END %s %s\n' "$(date +%s)" "$code" >> "$LOG"

echo
if [ "$code" -eq 0 ]; then
    echo "rb: build finished (exit 0)"
else
    echo "rb: build failed (exit $code)"
fi
exit "$code"
