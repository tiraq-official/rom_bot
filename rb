#!/usr/bin/env bash
#
# rb - ROM build wrapper for rombot.
#
# IMPORTANT: this file defines a shell FUNCTION. It is NOT a normal script.
#
# ROM build commands such as `ax`, `m`, `brunch`, `mka` are themselves shell
# functions created by `. build/envsetup.sh`. A separate script cannot see
# them, and `stdbuf` cannot exec a function - which is why `rb ax -br` fails
# with "stdbuf: failed to run command 'ax'". So `rb` has to live inside your
# shell as a function.
#
# Usage:
#   1. Set up the ROM environment in your build shell:
#          . build/envsetup.sh
#   2. Load this wrapper (once per shell; add the source line to ~/.bashrc so
#      every new shell has it):
#          source ~/rombot/rb
#   3. Build as usual:
#          rb ax -br -j16
#
# After the build it runs post_build.py, which (if enabled in config.json)
# uploads the ROM and powers the server off.
#
rb() {
    local dir; dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local log="${ROMBOT_LOG:-$dir/build.log}"
    if [ "$#" -eq 0 ]; then
        echo "usage: rb <build command...>    e.g.  rb ax -br -j16" >&2
        return 2
    fi

    printf '###ROMBOT_START %s %s\n' "$(date +%s)" "$*" >> "$log"

    # Shell functions / builtins are run directly; real executables get stdbuf
    # so their output is line-buffered and the bot sees progress live.
    if [ "$(type -t "$1")" = "file" ] && command -v stdbuf >/dev/null 2>&1; then
        stdbuf -oL -eL "$@" 2>&1 | tee -a "$log"
    else
        "$@" 2>&1 | tee -a "$log"
    fi
    local code=${PIPESTATUS[0]}

    printf '###ROMBOT_END %s %s\n' "$(date +%s)" "$code" >> "$log"

    if [ "$code" -eq 0 ]; then
        echo "rb: build finished (exit 0)"
    else
        echo "rb: build failed (exit $code)"
    fi

    # Post-build automation: upload the ROM and (optionally) power off.
    if [ -f "$dir/post_build.py" ]; then
        python3 "$dir/post_build.py" "$code"
    fi

    return "$code"
}

# If someone runs this file directly instead of sourcing it, say so.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "rb defines a shell function - source it, don't run it directly:" >&2
    echo "    source \"$0\"" >&2
fi
