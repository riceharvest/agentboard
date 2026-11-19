#!/bin/sh
# PTY smoke test: the real binary on a throwaway AB_HOME in a pseudo-terminal.
#   sh tui/tests/smoke.sh [path/to/ab-tui]
# 1. the acceptance form: script -q -c "timeout 5 ab-tui --board-home DIR" /dev/null
#    (GNU timeout exits 124 whenever it had to stop its child, however cleanly the child went down, so that exit code is
#    not evidence; we check the terminal restore sequences in the typescript instead)
# 2. `timeout --preserve-status` reports the child's own exit status: 0 means "handled SIGTERM and exited"
# 3. the TUI really polled `ab` (the throwaway home got its database); `q` quitting is covered by tests/render.rs
set -u
BIN=${1:-$(dirname "$0")/../target/release/ab-tui}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HOME_DIR=$(mktemp -d /tmp/ab-tui-smoke-XXXXXX)
OUT=$(mktemp /tmp/ab-tui-smoke-out-XXXXXX)
trap 'rm -rf "$HOME_DIR" "$OUT"' EXIT
export AB_BIN="$ROOT/ab"
unset AB_HOME AB_TASK AB_RUN AB_BOARD AB_ACTOR
fail=0
check() { if "$@"; then echo "ok   $DESC"; else echo "FAIL $DESC"; fail=1; fi; }

script -qe -c "stty cols 140 rows 40; timeout 5 $BIN --board-home $HOME_DIR" /dev/null >"$OUT" 2>&1
code=$?
echo "plain timeout exit code: $code (124 = timeout had to stop it)"
DESC="entered the alternate screen"; check grep -q "$(printf '\033')\[?1049h" "$OUT"
DESC="drew the header"; check grep -aq "runner" "$OUT"
DESC="left the alternate screen"; check grep -q "$(printf '\033')\[?1049l" "$OUT"
DESC="showed the cursor again"; check grep -q "$(printf '\033')\[?25h" "$OUT"
DESC="no panic"; check sh -c "! grep -aqi 'panicked' '$OUT'"

script -qe -c "stty cols 140 rows 40; timeout --preserve-status 5 $BIN --board-home $HOME_DIR" /dev/null >"$OUT" 2>&1
code=$?
DESC="SIGTERM exit status is 0 (got $code)"; check test "$code" -eq 0

DESC="the throwaway home got the board db (the TUI really polled ab)"; check test -n "$(ls "$HOME_DIR"/*.db 2>/dev/null)"
exit $fail
