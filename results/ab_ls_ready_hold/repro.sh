#!/bin/bash
# usage: repro.sh HOME_DIR MIN_FREE  -- one runner tick in a throwaway AB_HOME, then ab ls --ready / --stuck
R=$(cd "$(dirname "$0")/../.." && pwd)
export AB_HOME=$1; unset AB_TASK AB_ACTOR AB_BOARD; export PYTHONPATH=$R
ab() { python3 -m agentboard "$@"; }
ab config set min_free_gib "$2" >/dev/null
[ -n "$NOADD" ] || ab add "cheap card" --tier low >/dev/null
ab config set paused false >/dev/null 2>&1
python3 - <<PY
import sys; sys.path.insert(0, "$R")
from agentboard import store, runner
c = store.connect()
r = runner.Runner(c)
r.fill(1); store.record_holds(c, r.waiting, r.busy_dirs(), getattr(r, "gates", None))
PY
echo '$ ab ls --ready'; ab ls --ready
echo '$ ab ls --stuck'; ab ls --stuck
