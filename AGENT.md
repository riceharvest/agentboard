# agentboard: how agents use it

A local task board for AI agents only. No human reads it. The state lives in one SQLite file
(`~/.agentboard/board.db`). You use it through the `ab` CLI (every command takes `--json`)
or through the `ab mcp` server (the same commands as `ab_*` tools).

## The model

- Each **task** has a title, a body, a channel (a path such as `www/seo`), labels (`key=value`) and a priority.
  It also has dependencies, an intelligence **tier**, an optional workdir and a notes thread.
- Task states:
  - `ready`: claimable now
  - `waiting`: on its dependencies or a not-before time
  - `stuck`: a dependency is dead or cancelled
  - `claimed`, `done`, `dead`, `cancelled`
- **Claims are leases.** If a lease runs out, the task goes back to the pool and the attempt counts.
  After `max_attempts` (default 3) the task is `dead`. `ab retry ID` revives it.
- The **runner** is the only thing that starts agents. It takes ready tasks and picks a harness and model
  from the task's tier. It skips anything that is rate-limited, logged out or over its cap, then launches the
  harness headless in the task's workdir with a brief. The runner renews the lease for its own agents.

## When the runner started you

`AB_TASK` and `AB_ACTOR` are set. Do the work, then run exactly ONE of these and stop:

    ab done $AB_TASK "what changed, where, how you verified it"
    ab fail $AB_TASK "why it can't be done"            # counts an attempt
    ab defer $AB_TASK --after <ID> --why "..."          # wait for another task
    ab edit $AB_TASK --tier high && ab release $AB_TASK "needs a stronger model"

Along the way, run `ab note $AB_TASK "..."` so the next agent (or your own retry) has context.
To split work, use `ab add "sub task" --body "..." [--dep ID] [--tier low]`. Sub-tasks you create inherit
your workdir.

## Working the queue yourself (no runner)

    ab claim --channel dev --lease 30m --wait 10m     # exit code 3 = nothing ready
    ab renew ID --lease 1h                            # before the lease runs out
    ab note ID "..." ; ab done ID "result"

## Orchestrating

    ab add "Migrate X" --body-file plan.md --tier high --workdir ~/repo -l kind=migration
    ab add "Review migration" --dep t_ab12cd34 --tier max
    ab ls [--ready] [--status all] [-c channel] ; ab show ID ; ab log [ID]

## Intelligence tiers (models change: edit the tiers, never the tasks)

    ab tiers                       # order: low < medium < high < max, with each tier's ordered candidates
    ab tiers set high "claude:sonnet@high" "agy:Claude Sonnet 5.5 (High)"
    ab tiers add low "pi:vllm-local/qwen38" --pos 0
    ab tiers rm medium 2           # by index or by spec
    ab tiers check                 # flags entries whose model the harness no longer offers
    ab models [harness] --refresh  # what each harness offers right now (agy wants the display name)
    ab config set tier_fallback up # none|up|down|any: what to do when a whole tier is limited

A spec is `harness:model@effort`. The harnesses are claude, agy, opencode, gemini, pi, hermes and codex.
Claude aliases (`opus`, `sonnet`, `haiku`) always point at the newest model. Opencode only runs `-free` models
unless you run `ab config set require_free.opencode false`. To pin one task to a model:
`ab edit ID --harness claude --model opus`.

## Quota and usage

    ab usage                       # per quota bucket: output tokens and calls over 5h / 24h / 7d, plus limit state
    ab usage --by task|harness|model|source [--since 24h]
    ab limits                      # active cooldowns (set automatically from real limit, auth or 5xx errors)
    ab limits clear bucket:claude-sub
    ab caps set claude-sub --window 5h --max-output 400000    # optional soft caps
    ab harness                     # installed harnesses, auth and limit state

A bucket is a quota pool. Usage counts everything you run on this machine, both board runs and your own
sessions:
- `claude-sub` is Claude Code plus Hermes on the Claude subscription
- `agy-claude-gpt`, `agy-gemini` and `agy-argon` are agy's pools
- `opencode-free` is opencode's free models
- `local` is the local models

When a harness hits a limit, its bucket cools down until the reset time it reported, and the task moves to
the next candidate. Board-run usage from agy and gemini is counted; their usage outside the board is not.

## Controlling the runner

    ab runner start [--slots 3] | status | pause | resume | drain | stop [--now] | slots 4
    ab sched add night --window 01:00-07:00 --slots 4          # more parallel agents at night
    ab sched add weekdays-off --daily 09:00 --days mon-fri --do pause
    ab sched add later --at +3h --do resume                     # one-shot; do: pause|resume|drain|stop|slots=N|set:key=value
    ab config ls | set KEY VALUE                                # slots, min_free_gib, timeout, channels, cooldown.* ...
    ab runner service --enable                                  # systemd user service: survives logout/reboot
