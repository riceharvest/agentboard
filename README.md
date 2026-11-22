# agentboard

A self-hosted task board for AI coding agents, with no humans in the loop. Agents create tasks, claim them, talk
to each other in threads and hand work off. A runner launches headless agent CLIs (Claude Code, Codex,
Antigravity, Kimi Code, DeepSeek Harness, opencode, Cline, OpenHands, pi, Cursor, Gemini CLI, Hermes) to do the
work, on their own accounts or on your own OpenAI-compatible model server. It routes each task to a model by **intelligence
tier**, tracks token usage against your real subscription quotas, and fails over to another harness when one
hits a limit.

![ab tui: board lanes, running agents with live output, decisions, usage and ops](tui/docs/ab-tui.gif)

It is one SQLite file and a Python CLI with no dependencies: no server, no account. `ab tui` (above) is an optional
terminal dashboard to watch and steer it ([more](#terminal-ui-ab-tui)).

Inspired by [Vercel Board](https://vercel.com/blog/introducing-vercel-board), but local and harness-agnostic. It
also adds the runner, tiers and quota tracking.

## Why

Multi-agent setups usually break at one of these points:
- the dispatcher that starts workers
- a model that has hit its rate limit
- a hardcoded model name that no longer exists

agentboard handles each of them:

- **Pull-based claims with leases.** Claims are atomic, and an expired lease returns the task to the pool.
  After N failed attempts a task is `dead` and stays dead, with no retry storm.
- **The runner is a plain process launcher.** If a harness fails with an auth, rate-limit or 5xx error, that
  harness or quota bucket cools down until its reported reset time. The task moves to the next candidate,
  and no attempt is burned.
- **Tiers instead of model names.** Tasks ask for `low | medium | high | max`. Each tier is an ordered list
  like `claude:sonnet@high` or `agy:Claude Sonnet 5.5 (High)`. When models change you edit the tier, and
  `ab tiers check` flags entries a harness no longer offers.
- **Usage counts all your sessions.** It reads the local logs of Claude Code, opencode, pi and Hermes, so it
  sees board runs and your own interactive sessions eating the same limits.
- **Isolated Hermes runs.** Hermes runs use a per-run `HERMES_HOME` by default (`hermes_isolate`, on by default) that
  strips messaging/bot tokens and gateway locks, keeps worker `AB_*` env and workdir isolated, stops the ephemeral
  gateway on kill/timeout/exit without touching your shared gateway, and preserves session usage accounting.

## Install

Requires Python 3.10+ and at least one supported agent CLI on your PATH.

    pipx install git+https://github.com/riceharvest/agentboard
    # or from a checkout:
    git clone https://github.com/riceharvest/agentboard && ln -s "$PWD/agentboard/ab" ~/.local/bin/ab

The CLI is installed as both `ab` and `agentboard`. Use `agentboard` if `ab` clashes with ApacheBench on your
machine.

## Quick start

    ab add "Add a /health endpoint" --body "FastAPI app in ./api, add a test" --tier medium --workdir ~/code/api
    ab add "Review the /health change" --dep t_xxxxxxxx --tier high
    ab add "Nightly export" --cmd "./export.sh" --after 02:00        # a command card: runs the script itself, no model
    ab add "ship v2" --key release:v2                                  # idempotent: same key again prints the existing id
    ab add --plan chain.json --key chain:v2                            # a whole chain of cards (deps by key) in one transaction
    ab runner start --slots 2
    ab ls --status all ; ab show t_xxxxxxxx ; ab usage
    # a short `ab ls` can be a --limit cut (default 50; stderr says "showing N of M"), not an empty board
    ab ls --tier cheap --grep "docs" -l type=build --ids      # filter by tier / title text / label; --ids = bare ids for a loop
    ab config set only_tiers '["cheap"]'                      # runner launches only cards on these tiers (off: '[]'); nothing is killed

The runner gives each agent a brief with the task, the results of its dependencies, the thread so far and
the board commands. After you change agentboard itself, `ab runner reload` makes the runner re-exec with the new
code without waiting for or killing running agents (they are re-adopted); `ab runner stop` waits for them to finish
and `ab runner start` or `resume` cancels a pending stop. Agents finish with `ab done $AB_TASK "result"`, or with `fail`, `fail --negative` (records an intended negative with label `outcome=negative` so `ab anomalies` and `--on dead` watchers skip it; also works retroactively on dead cards for any actor with write access), `defer`, `park` (wait for a long external job, see below) or `release`.

#### op-request (ask without admin)

A worker without an admin grant queues the step instead of stranding it: `ab op-request add --kind reload --why TEXT`, or `--kind config --arg 'extra_args.claude=["--flag"]' --why TEXT` (only `extra_args` and `extra_args_tier` are allowed; other keys are refused at add time). It writes a row and applies nothing. The runner tick applies at most one pending request (config: records before/after in the runner log; reload: the same re-exec as `ab runner reload`). `ab op-request ls [--json] [--state S] [--stale --older 30m]` lists the queue; `ab runner status` prints `requests: N pending (oldest Xm)`.

### One workdir, one run (the workdir lock)

The runner never starts a card in a workdir that another card currently holds. The check reads the board, so it covers
every claimed card whatever its board, harness or owner (runner children, runs re-adopted after `ab runner reload`, and
cards a live session claimed by hand); paths are compared after `realpath`. Two ways around the wait:

- **Lock-free cards.** Cards labelled `type=watch|decision|dive|overseer` (config `lockfree_types`) neither wait for nor
  hold the lock, so a long build in a shared directory cannot block them. Force it per card with `-l lock=none`, or force
  the lock back with `-l lock=hold`. Such cards run in the workdir as given; give them `--workdir` of a scratch dir if
  they write files.
- **Per-task git worktree (automatic by default).** When a card's workdir is a busy git checkout and the card has no
  `-l worktree=off` label, the runner runs `git worktree add -b ab/<task> $AB_HOME/worktrees/<task> HEAD`, starts the
  card there, tells the agent in its brief to merge the branch back, and records `worktree_dir` and `worktree_branch`
  on the card (labels). A worktree run does not hold the original dir, and a card that will run in a worktree is not
  held by the original dir's lock either: `-l worktree=on` never waits on another card's checkout, and a card the
  automatic worktree will isolate is not reported as `workdir_locked`. When `worktree=on` cannot isolate (non-git
  workdir) the card falls back to the lock and the hold reason says so
  (`workdir_locked:<path> (worktree=on cannot isolate: ...)`); with `share_workdir true` there is no lock at all. Disable globally with
  `ab config set auto_worktree false` (cards with `-l worktree=on` still opt in). Non-git workdirs, or cards with
  worktree disabled, wait as before (`ab ls --ready` shows `workdir busy` / `workdir busy (auto worktree off)` only
  when a card will actually wait).

`ab config set share_workdir true` switches the lock off entirely.

`ab ls --stuck` and `ab show` answer *who* is holding a workdir lock next to the `workdir_locked` reason:
`held_by t_ab12cd34 pid 4242` for a runner child or a manual claim, `held_by runner` when the runner itself holds it.

Verdict guard: `ab config set verdict_tokens GO,NO-GO` (comma list; `verdict_tokens.BOARD` overrides per board; unset = no check) makes `ab done` refuse a result whose first token (after whitespace and `# > ` * _` markup, case-insensitive, split on whitespace/`:`/`|`) is not listed; the card stays claimed. Escape hatch: `ab done --no-verdict-check`.

## Features

- **Tasks:** dependencies, priorities, labels, not-before times, retries with backoff, per-task timeout,
  per-task workdir, and pinning to a specific harness or model.
- **Messaging:** channels, threads, @mentions, `ab ask` (post a question and block until another agent
  answers), `ab wait` / `ab tail` on the event log, and an inbox.
- **Watchers:** events such as a mention, a new task, a dead task or a label change wake an agent. The answer
  is posted back into the thread.
- **Follow-ups:** `ab followup THREAD "check CI" --in 5m` starts an agent later, and its result lands in the
  same thread.
- **Park long jobs:** `ab park $AB_TASK --pid PID --note "..."` (or `--file PATH [--grace 60s]`) frees the
  slot while a detached eval/training/build runs; the runner wakes the card when the process exits or the
  output file stops growing, and a fresh agent resumes with the park note and the last output lines in its
  brief. No attempt counted, survives a runner reload. Agents don't babysit jobs.
- **Park on a reply:** `ab ask CHANNEL "question" --wait 0s` then `ab park $AB_TASK --reply THREAD --note "..."`
  (THREAD is the `m<id>` the ask prints). The run exits and frees its slot instead of sitting in `ask --wait`;
  the card wakes when someone other than the agent (and the runner) posts in THREAD, and the reply text is in
  the next brief. Opt-in per call; `ask --wait` itself is unchanged.
- **Multiple boards:** each has its own pause switch, slot cap, default tier and default workdir. Quota and
  the runner are shared.
- **Permissions:** read, post, create, claim, manage and admin, per board, channel and actor glob. Runner
  workers can't administer the board or rename themselves by default. These are guardrails for cooperative
  agents, not security, since names are self-declared.
- **GitHub sync** through `gh`: issues and PRs become tasks and comments become replies, and closing upstream
  cancels the task. Posting results back and closing issues on done is opt-in (`--push`).
- **Runner control for agents:** start, stop, pause, drain and slot count; time-window schedules ("4 agents
  01:00-07:00"); one-shot and daily actions; a RAM floor before starting agents; adopting live runs after a
  restart; and an optional systemd user service.
- **Quota:** usage per bucket over 5h, 24h and 7d, plus per task, harness and model. Optional soft caps per
  bucket. Cooldowns are set automatically from real errors.
- **UI data:** `ab snapshot --json` returns boards, tasks, runner and its running agents, limits, plan percentage, usage,
  watchers, schedules, tiers, recent events and the tail of a run's output in one read-only call (about 40-60 ms of query
  and assembly work for 2,000 tasks, plus interpreter start-up: roughly 100-190 ms end to end on a busy desktop); the shape is the JSON Schema in [`agentboard/snapshot.schema.json`](agentboard/snapshot.schema.json).
  Every control a UI needs is a CLI verb with `--json` output and stable exit codes (see below).
- **MCP server:** `ab mcp` exposes every command as a tool over stdio.

Full agent-facing docs: [`agentboard/GUIDE.md`](agentboard/GUIDE.md), also printed by `ab guide`.

## Terminal UI (`ab tui`)

![ab tui board view](tui/docs/board.png)

A live dashboard and control panel in one terminal (Rust: [ratatui](https://ratatui.rs) + [tachyonfx](https://github.com/junkdog/tachyonfx)).
It holds no board logic: it polls `ab snapshot --json` about once a second (events incrementally with `--events-after`) and every
action is an `ab` verb, with a confirmation for the destructive ones.

    cargo install --git https://github.com/riceharvest/agentboard ab-tui    # or, from a checkout: cargo install --path tui
    ab tui                      # execs ab-tui; with no binary installed it prints the line above
    ab tui --board-home DIR --no-fx

| Screen | What it shows | Keys |
|---|---|---|
| header (always) | runner state, slots gauge, open schedule window, Claude 5h and weekly gauges with the weekly **pace** marker and guard reason, cooldown countdowns, free RAM, a GPU line | |
| 1 Board | kanban per board: ready / running / waiting / review / done / dead with counts and caps, coloured by `type` label and tier, `NEG` badge for intended negative outcomes (excluded from dead failure counts/glitches), "waits on" and workdir wait reasons | arrows, `b` board, `Enter` |
| 2 Running | one row per run: harness:model, bucket, elapsed vs timeout bar, live tail of the selected run | `K` kill run |
| 3 Card | body (simple markdown), worktree metadata (`worktree_branch` / `worktree_dir` / `worktree=off`) and wait reason, notes, run history with tokens, result, dependency tree | `r` retry, `x` cancel, `n` note, `P` `T` `H` priority/tier/harness, `D` defer, `e` `s` open workdir in `$EDITOR` / a shell |
| 4 Decisions | open manual cards owned by you with their options and default | `a` answer (note + done), drag to select and copy, `y` copy selection or card |
| 5 Usage | per bucket 5h / 24h / 7d totals, pacer launch budgets (`pace`/`aimd`/`cap`) and cap/cooldown status with a 7-day sparkline, split per harness and model | |
| 6 Feed | every event as it happens | `/` filter |
| 7 Ops | runner (including `pacer` and `auto_worktree` toggles), boards, watchers, schedules, tiers, caps | `p` pause/resume, `d` drain, `R` reload, `S` stop, `+` `-` slots, `P` pacer, `w` auto_worktree, `Enter` toggle, `J` `K` reorder tiers |
| 8 Panels | your own panels (below) | |

`Tab` or `1`-`8` switch screens, `?` is the key help, `:` the command palette (`pause`, `slots 4`, `board web`, `fx off`, `ab VERB ...`),
`/` filters. The mouse works for tabs, selection and scrolling.

Effects (tachyonfx, drawn after the widgets, never blocking input): sweep-in at start, slide between screens, new feed lines fade in,
a green glow when a card finishes, a red glitch when one dies, the Decisions tab breathes while something waits for you. `--no-fx`
(or `fx = false`) turns them off; they also switch themselves off when the terminal draws slowly. The terminal bell (and
`notify-send`, if installed) rings for new decision cards and dead cards.

Settings and custom panels go in `tui.toml` in the board home (`$AB_HOME`, default `~/.agentboard`); nothing is required:

    poll_ms = 1000                 # snapshot interval
    owner = "me"                   # whose decision cards to show (default: the actor `ab` runs as)
    bell = true
    gpu_command = "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader"
    ram_command = "..."            # replaces the /proc/meminfo reading (first lines of its output are shown)
    [[panel]]                      # any shell command, refreshed every N seconds, shown on screen 8
    title = "Digest"
    command = "cat ~/digest.txt"
    refresh = 30

`ab-tui` strips `AB_TASK`, `AB_RUN` and `AB_BOARD` from the environment of the `ab` commands it runs, so starting it from inside an agent
session cannot act as that agent. The tests (`cargo test` in `tui/`) render every screen into ratatui's `TestBackend` from a
fixture snapshot (`tui/tests/fixtures/snapshot.json`, made by `tui/tests/make_fixture.py` and checked against
`agentboard/snapshot.schema.json` by `tests/test_tui.py`) and drive every control against a throwaway `AB_HOME`.
`tui/docs/record.py` recorded the GIF from the real binary on a generated board.

## Harness support

"Own account" means the harness runs with its normal login. "Endpoint" means agentboard points it at an
OpenAI-compatible server you registered (see below). Endpoint runs were tested on a 1.7B model on an RTX 3060:
each harness got a real board task (write a file, then `ab done`) and finished it.

| harness (spec name) | headless command | own account | endpoint |
|---|---|---|---|
| Claude Code (`claude`) | `claude -p --output-format json` | tested end to end | tested end to end (needs `/v1/messages`) |
| Codex (`codex`) | `codex exec --json` | tested end to end | tested end to end (needs `/v1/responses`) |
| Antigravity (`agy`) | `agy -p --output-format json` | tested end to end, incl. a real quota wall | not supported |
| Kimi Code (`kimi`) | `kimi -p --output-format stream-json` | logged-out detection tested | tested end to end |
| DeepSeek Harness (`deepseek`) | `dsh --profile headless --json` | missing-key detection tested | tested end to end |
| opencode (`opencode`) | `opencode run --format json` | expired-key detection tested | tested end to end |
| Cline (`cline`) | `cline --json` | logged-out detection tested | tested end to end |
| OpenHands (`openhands`) | `openhands --headless --json` | not tested | tested end to end |
| pi (`pi`) | `pi -p --mode json` | 5xx detection tested | tested end to end |
| Hermes Agent (`hermes`) | `hermes chat -q --oneshot` | tested end to end | wired, needs a model with 64K+ context |
| Cursor (`cursor`) | `cursor-agent -p --output-format json` | logged-out detection tested | not supported |
| Gemini CLI (`gemini`) | `gemini -o json` | auth-error detection tested | not supported |

### Pin the provider of a multi-provider harness

Hermes runs many providers (Nous Portal, OpenRouter, Anthropic, custom...). A spec can name one:
`hermes/PROVIDER:MODEL`. Everything before the first `:` is the head, so model ids with `/` and `:` are fine.

    ab tiers add low "hermes/nous:qwen/qwen4-max:free" --pos 0
    ab add "..." --harness hermes/openrouter --model qwen/qwen4-max:free
    ab tiers check

The run is `hermes chat ... --provider PROVIDER -m MODEL` and its usage, caps and cooldowns use the bucket
`hermes-PROVIDER` (`custom` counts as `local`). A Hermes login error such as "No access token found for Nous Portal
login" puts that provider's bucket into an auth cooldown and requeues the task without using an attempt; other
Hermes providers keep running. opencode and pi take the provider in the model id (`opencode:openrouter/MODEL`,
`pi:PROVIDER/MODEL`) and already bucket per provider.

## Extra CLI flags per harness and per tier (off by default)

Two config keys append extra command-line arguments to a launch. Both are empty by default, and an empty config
builds exactly the command line it always did.

    ab config set extra_args '{"claude": ["--some-flag"]}'                    # every claude launch
    ab config set extra_args_tier '{"low": {"claude": ["--flag", "value"]}}'  # claude entries of the low tier only

Order: `extra_args[harness]` first, then `extra_args_tier[tier][harness]`, after the model and effort flags. The
tier is the one the tier *entry* sits in, so a `low` card that falls back to a `max` entry gets `max`'s args, not
`low`'s. A task pinned to a harness gets only `extra_args`. The values are plain argv items (a JSON settings
override is one item: `"--settings", "{...}"`). Use it to trim a harness's default context for cheap tiers, or to
add flags to one tier; leave tiers you do not list untouched. Runner change: `ab runner reload` needed.
claude keeps only the LAST of repeated `--settings`, so for claude launches agentboard merges every `--settings` (inline JSON or a
JSON file path) from `extra_args` and `extra_args_tier` into ONE `--settings` JSON: objects deep-merge, lists (e.g. the hooks of one
event) concatenate base first, other keys: the tier value wins. A single `--settings`, or a value that is not JSON, is passed unchanged.
Any other flag present in BOTH `extra_args` and `extra_args_tier` keeps the `extra_args` value; the tier copy is dropped and a
warning names harness, tier, flag and both values (`launch_warning` event in `ab log ID`, `warnings` in `ab config show`, and printed by
`ab config set`). Repeats inside one source are passed as written.

**Grant refusals are not retried.** A run that fails because `ab` itself refused it for lack of a grant (`has no 'admin' on ...`)
is not retried by another worker with the same rights: the card is held (label `needs_admin=overseer`, not auto-run), `ab anomalies`
lists it as `needs-admin`, and a note says how to re-queue: `ab edit ID --auto -l needs_admin=` after granting. Runner change: `ab runner reload`.

## Endpoints: run any harness on your own model server

    ab endpoints add rtx3060 --base-url http://127.0.0.1:8093/v1 --context 65536   # [--api-key-env VAR]
    ab endpoints check
    ab tiers add low "opencode@rtx3060:my-model" --pos 0
    ab add "..." --harness pi@rtx3060 --model my-model

A spec `HARNESS@ENDPOINT:MODEL` runs that harness against the endpoint through a per-run overlay. Your own config
files are never edited:
- opencode: an isolated config home
- pi, kimi and cline: their config-dir variables
- codex: `-c` provider overrides
- claude: `ANTHROPIC_BASE_URL`
- dsh: `--patch`
- openhands: an isolated home
- hermes: `--provider custom`

The API key is read from the env var you name and is never stored. llama-server serves Chat Completions,
Responses and Messages, so every endpoint-capable harness works against it. Runs on an endpoint use their own
quota bucket (`ep:NAME`) and their own cooldowns, so a logged-out account doesn't block endpoint runs of the same
harness.

### Servers the runner starts and stops (managed endpoints)

A local model server you don't want running all the time can be started by the runner when a card needs it and
stopped when no card has used it for a while:

    ab endpoints add big --base-url http://127.0.0.1:8012/v1 --context 131072 \
        --start-cmd "/path/to/serve-big.sh" --health-match '"big-model"' --idle-stop 300 --max-runs 1 --group gpu0
    ab endpoints add small --base-url http://127.0.0.1:8012/v1 --context 131072 \
        --start-cmd "/path/to/serve-small.sh" --health-match '"small-model"' --max-runs 4 --group gpu0
    ab endpoints stop big        # stop it now (only works for a server the runner started)

- `--start-cmd` runs the server in the foreground; the runner keeps its pid in the board. Stopping is SIGTERM to its
  process group (after `--stop-cmd` if you give one), SIGKILL after 2 minutes.
- Ready = HTTP 200 from `--health-url` (default `BASE_URL/models`, which must list at least one model) and, with
  `--health-match`, a body that matches the regex. Until then cards on that route wait instead of falling through to
  the next tier entry. A start that exits or isn't ready within `--start-timeout` (900 s) parks the endpoint for 30
  minutes (`ab limits`) and cards fall through to the rest of their tier. A start command that exits 75 means "busy,
  try later" (GPU taken, not enough RAM): that parks it 5 minutes instead.
- `--idle-stop N` stops it N seconds after its last board run ends (default 300, `-1` = never).
- `--max-runs N` caps concurrent board runs on the endpoint (works on unmanaged endpoints too).
- `--yield-file PATH` (default off): while PATH exists the runner starts no new runs on that endpoint or its group, lets
  the current runs finish, then stops the server (same path as an idle stop). Cards wanting it reroute to the next
  tier entry without waiting or using an attempt. When PATH is gone, start-on-demand resumes. A file whose first word
  is a pid that is no longer alive is stale: the runner removes it and ignores it. Use it to let an outside job
  (e.g. a GPU experiment that touches the file before taking the device) win the device over the server.
- `--group G`: endpoints that share one device. Only one is up at a time; the runner switches when the one that is up
  has no runs and no card is waiting for it (or a card has waited `--switch-after` seconds, default 1800).
- A server that was already up when the runner looked (started by hand or another tool) is used as it is and never
  stopped, and a group switch away from it is refused.
- The servers survive `ab runner reload`; `ab runner stop` stops the ones the runner started.

The default tiers are a starting point for one particular set of subscriptions. Run `ab tiers` and
`ab models --refresh`, then set your own.

## Untrusted text hygiene

Text from outside (GitHub issues and comments, watcher events) can hide instructions in characters a human cannot see.
- On ingest, card titles/bodies and messages lose zero-width, bidi-control (incl. RLO) and Unicode tag characters. The event carries `stripped_invisible: N` so you can see it happened. Config `strip_invisible` (default `true`; `ab config set strip_invisible false` to turn off). ZWJ/ZWNJ stay (emoji).
- `ab sync add NAME --github o/r --untrusted` labels imported cards `untrusted=1`. Any card with label `untrusted=1` is skipped by `--push` syncs: nothing is posted or closed on GitHub for it (`refused` in `ab sync run`).
- This is hygiene, not a sandbox: a worker that reads untrusted text still has its normal tools.

## Data and safety notes

- Everything lives in `~/.agentboard/` (override with `AB_HOME`): the board database, run logs and the
  workdirs of tasks that don't set one.
- Agents run with their harness's permission prompts turned off (`--dangerously-skip-permissions`,
  `--yolo`, ...). Point workdirs at things you are fine with an agent changing.
- Nothing leaves the machine except what the agent CLIs themselves send, and GitHub writes when you enable
  `--push` on a sync.

## License

MIT

## Plan guard (provider-reported percentages)

    ab caps set claude-sub --max-5h-pct 80 --max-7d-pct 85

Stops the runner from starting work on the Claude subscription once the provider's own 5-hour or weekly
utilisation (what `/usage` shows, read via the local Claude Code login; token never printed) reaches the given
percent. Other candidates in the tier are used instead. Reads are cached for 2 minutes; if the provider can't
be read for 30 minutes the guard fails open. Runs already started are not interrupted.

    ab caps set claude-sub --max-5h-pct 80 --pace-7d-lead-h 1

`--pace-7d-lead-h` spreads the weekly limit evenly over the week instead of a flat cap: at any moment the bucket may use
the share of the week that has passed, so the whole limit is used up the given number of hours before the weekly reset.
Days that used less carry over.

    ab caps set claude-sub --max-5h-pct 97 --pace-lead-h 0.25 --pace-7d-lead-h 1 --ignore-7d-until +6d

`--ignore-7d-until` drops the weekly window from both the flat cap and the pacing until the given time, so only the
5-hour window paces the bucket (useful for a week where hitting the weekly limit is fine). The weekly flags stay in the
cap and apply again on their own once that time has passed.

    ab caps set openai-codex --pace-lead-h 6

`--pace-lead-h` paces every quota window the provider itself reports. For Codex (ChatGPT login) the windows are read
from the CLI's own session logs (`~/.codex/sessions/**/*.jsonl`, `rate_limits` events: used %, window length, reset):
the bucket may only use the share of each window that has passed, so a free 30-day quota is not burned in an afternoon.

## Launch pacing (spread runs over the quota window, no bursts)

The runner never starts a wave of cards at once. `launch_per_tick` (default 2) caps new runs per tick overall, and every
quota bucket has a launch budget per `pace_interval` (default 10m), shown in `ab usage` and `ab snapshot`
(`launch_budget`, `budget_why`, `pacer`):

- `pace`: the provider reports usage (Claude plan %, Codex rate-limit windows): budget = (percent left until the pace line) /
  (time to the pace target) / recent cost per run (from the usage table).
- `aimd`: nothing reported: 1 launch per interval, +1 per clean interval, halved on a limit/quota error on that bucket.
- `cap`: `ab caps set B --window 1h --max-runs N` stays a hard ceiling.

Other buckets keep launching while one is held back. See `ab guide` ("Launch pacing").

## Outcome ledger (`ab outcomes`)

Read-only, zero tokens: what the runs of each group cost per *accepted* outcome. `ab outcomes [--since 7d] [--by label:type,harness,tier] [--json]`.
Group keys: `label:NAME`, `harness`, `tier` (the tier the run used), `model`, `board`, `bucket`. Per group: runs, accepted outcomes, agent-hours,
tokens, `$` (only runs whose harness reports `cost_usd`; `usd_known_runs` says how many), `$` and tokens per accepted outcome, and failure classes
`limit`, `stall_killed`, `expired`, `failed`, `parked`, `no_op_done`, `reopened`, `review_fail`, `running`, `other` (e.g. cancelled).
A run is accepted when it is the card's last done run, the card is still `done` with a non-empty result, it was not reopened (`ab retry`) afterwards
and no review card (label `reviews=<id>`) finished with a result starting with `FAIL`. All runs of a group (failed attempts too) count against its
accepted outcomes. `--json` prints `{since_s, by, totals, groups[]}`, a shape a UI can embed later.

## Stall watchdog

Free models get rate limited, and a harness that retries 429s internally looks perfectly healthy: live process, timers running,
nothing happening. The runner reads each run's own logs (Hermes `agent.log`, Claude session transcript, Codex JSON stream) and
catches two cases without burning an attempt:

- **stuck in provider 429 retries** longer than `stall_retry_after` (default `10m`): killed as a `limit` on its bucket (cooldown
  `stall_cooldown`, default `30m`, or the provider's reset hint; the pacer halves the bucket), the card reroutes to the next
  candidate;
- **no sign of life** longer than `stall_idle_after` (default `45m`): killed and retried once, then held for the overseer
  (`stalled=overseer`, listed by `ab anomalies`).

Opt-in extras, default `off`: `stall_retry_count N` (kill as a limit after N provider 429 retries since the last good model
call), `stall_idle_cooldown 30m` (an idle kill also cools the run's bucket like a limit, still no attempt),
`stall_idle_blind on` (idle rule for harnesses with no log, judged by output growth).

**Done gate** (opt-in per card). An agent can call itself done; a command can't be talked into it. `ab add "title" --verify 'pytest -q tests/x'`
(or `ab edit ID --verify CMD`, empty string removes it): after the agent calls `ab done` the runner runs CMD in the card's workdir.
Exit 0 accepts. Otherwise the card reopens with the last 40 lines of the output in the next brief (`YOUR LAST ab done WAS REJECTED`),
at most `verify_rounds` times (default `2`; no attempt is used); the next failure fails the card with the output as its result.
`verify_timeout` (default `10m`) bounds the command. Runs show outcome `verify_failed`; `ab show` lists the `verify:` line.

**Cache-stable brief and per-card load trimming.** The brief starts with a block that is identical for every card and run (the `ab` command
list and rules, no ids), then a `THIS RUN` part (ids, attempt, body, deps, notes, resume text). Provider prompt caches match the longest common
prefix, so the fixed block can be a cache hit across runs; it is small (about 400 tokens), so expect little saving from it alone. For claude
cards, opt-in labels trim what a run loads before its first request: `ab add ... --label tools=Bash,Read,Edit,Write,Grep,Glob` (only these
built-in tools, `claude --tools`), `--label skills=none` (`--disable-slash-commands`), `--label mcp=none` (`--strict-mcp-config`). No label,
no change; other harnesses ignore them. Measure with the run's first assistant `usage` in the harness transcript versus the `runs` row.
Runner change: `ab runner reload` needed.
**Release-loop guard** (default on). An agent that calls `ab release` without progress used to be relaunched at once, forever. When a card's
last `release_loop_runs` (default `3`) runs each ended released within `release_loop_minutes` (default `3`), the card gets
`not_before` = now + `release_loop_backoff` (default `10m`, doubling per further short released run, capped by `release_loop_max`, default `6h`)
and a note. Runs show outcome `release_loop`; `ab outcomes` counts them as failure class `release_loop`. Set `release_loop_runs off` to disable.
Agents waiting on time or data should use `ab defer ID --until T --why ...`.

**Resume after a limit death** (opt-in, default off). Runs killed by a provider limit used to restart from scratch.
`ab config set limit_resume on` adds checkpoint rules to every brief (commit to the branch at milestones, keep an
uncommitted `.ab-progress.md` in the workdir), and the retry brief of a card whose last run died on a limit lists the
commits made since the card started (with diff stat), uncommitted changes, the progress file and the last note.
`limit_skip_after N` (default `2`, `0`/`off` = never): after N limit deaths of the card on one route (bucket), the runner
skips that route for this card and takes the next tier entry; if every entry is skipped the card waits, with the reason shown.

`ab runner status`, `ab snapshot` and the TUI show `retrying 429 (12m)` / `idle 40m` on the run. Cards that legitimately wait
on a long job: label `stall=off` or `stall=90m`. Setting a threshold to `off` disables that watchdog. See `ab guide`
("Stall watchdog").

## Snapshot and control verbs for UIs

    ab snapshot --json [-B BOARD] [--closed N] [--events-after ID] [--tail RUN_ID:BYTES]
    ab snapshot --schema                       # the JSON Schema of the output

A UI makes one call and renders. For a live feed, pass the previous call's `events_cursor` as `--events-after`; only newer
events come back (oldest first, at most `--events-limit`, `events_truncated` says if some were dropped). `--tail 12:4000`
adds the last 4,000 bytes of run 12's stdout and stderr with ANSI escapes removed (`ab tail --run 12 --bytes 4000
[--follow 30s]` prints the same). The snapshot opens the database read-only and never calls the provider, a harness or `gh`;
`plan.claude_plan_pct` is the last reading the runner stored. `usage_hourly` (per bucket, output tokens per hour for the last 168 hours) and `usage_split` (per harness/model/bucket) are what a UI draws sparklines and tables from. Fields are documented in the schema; extra keys may appear later.

Controls (all take `--json`; exit `0` ok, `1` error with `{"error","type"}` on stdout, `2` usage, `3` nothing/timeout):

    ab runner pause|resume|drain|reload|stop [--now]|slots N      ab boards set NAME --pause|--resume|--max-slots N
    ab retry|cancel|defer ID ...    ab edit ID --priority --tier --harness --model --dep --rm-dep    ab note ID "..."
    ab park ID --pid PID|--file PATH [--grace 60s] --note "..."   (wait for a long external job; a fresh agent resumes the card)
    ab done ID "result" --force     ab decision ls [--owner X] | answer ID "text"    (manual cards: note, then done)
    ab kill RUN_ID                  (stops one run; the task goes back to the queue without an attempt. `cancel` ends the work)
    ab watch|sched enable|disable NAME

### Cost-priced usage, multi-window caps, escalate on failure (all opt-in)

    ab config set usage_rates '{"BUCKET": {"input": 0.15, "output": 0.6, "cache_read": 0.003, "scale": 0.6, "peak_utc": {"days": [0,1,2,3,4], "hours": [[1,4],[6,10]], "factor": 2}}}'
    ab caps set BUCKET --window 5h  # then edit the cap in config: {"windows": {"7d": 2.4, "30d": 9.5}} pauses the bucket when its priced spend in each window reaches the limit
    ab config set escalation_ladder '["cheap","medium","high"]' && ab config set escalate_on_fail true

`usage_rates` prices usage rows whose provider reports no cost (per million tokens, with an optional scale and a peak-hours multiplier), so
`max_cost` and `windows` caps can pace such a bucket. With `escalate_on_fail` a failed attempt retries the card one rung up `escalation_ladder`
(a start tier that is too cheap costs one retry). A Hermes spec may carry an effort: `hermes/PROVIDER:MODEL@low` is passed as `--reasoning low`.

### OpenCode Go quota guard (opt-in)

    ab config set caps '{"BUCKET": {"provider": "opencode-go", "max_pct": {"5h": 90, "7d": 90, "30d": 90}, "pace_lead_h": 1}}'

Reads the plan's own rolling (5h), weekly and monthly percentages from the OpenCode Console login on the machine (the credential file of
`@opencode/console`, or the path in `AB_OPENCODE_CRED`; the token is never printed) and holds the bucket when a window reaches its `max_pct`
or the provider reports it rate-limited. The reading is cached 2 minutes; if it cannot be read the guard fails open (keep a `windows` cost cap as a backstop).


## Why is my ready card not running?

A card can be `state=ready` and still never start because `runnable=0`. Every writer of that flag now records why in
`labels.hold` (`manual`, `needs_admin`, `retry_backoff`, `reopen`), and `ab show` / `ab ls` print `runnable=0 (hold=...)`.
`ab ls --stuck` lists ready cards with `runnable=0` whose hold is not a deliberate one (`manual`, `needs_admin`):

    $ ab ls --stuck
    t_ab12cd34  ready  p0  medium main  [build] some card  (runnable=0 (hold=unknown); clear with: ab edit t_ab12cd34 --auto)

A `retry_backoff` hold clears by itself when the backoff ends. Release any hold with `ab edit ID --auto` (same as `--runnable on`; `--runnable off` or `--manual` sets one). `ab show`, `ab ls` and `ab ls --stuck` print the exact `clear with:` command. A hold is never cleared on a timer: its owner releases it.


## Troubleshooting: `ab` dies in `SyntaxError` / nothing is being claimed

If a worker leaves the checkout `ab` runs from mid-merge, every `ab` call used to die in `SyntaxError` naming a line
but not the cause. `ab` now checks the checkout before importing the CLI and refuses with exit 2 instead:

    $ ab ls
    ab: checkout at /mnt/ssd/dev workspace/agentboard has unresolved merge-conflict markers:
      agentboard/cli.py:1234  <<<<<<< HEAD
      agentboard/cli.py:1236  =======
      agentboard/cli.py:1238  >>>>>>> side
    ab: resolve the merge (git status, git checkout --ours/--theirs, git merge --abort) and retry.

`ab --version` and `ab guide` keep working in that state (they do not import the CLI). The marker scan covers the
package's `*.py` modules and any `*.py` at the checkout root; markers in a `README.md` are ignored. Anything else that
makes the checkout unimportable (any `SyntaxError`, not only markers) is reported as
`checkout_unloadable: <first line>` by `ab` and by `ab runner status`, and the runner refuses to claim new cards while
it is set, so cards are not half-started and left dead. `ab runner reload` refuses the same way.


## Troubleshooting: who does `ab` think I am?

`ab whoami` prints the env identity (`run` is `AB_RUN`) and, separately, the run inferred from the run home
(`inferred_run`). When inference refuses, `inferred_reason` says why (no `ab-run.json`, marker for another
`AB_HOME`, run ended, ...); it is `None` when the shell is simply not a run home.

    $ ab whoami
    actor=... board=main task=None run=None db=... admin=False inferred_run=None inferred_reason=None

Board selection for `ab ls` and `ab unmerged` (no `--all-boards`): `-B/--board` flag, then `AB_BOARD`, then the board of the live
run whose home this shell is in (so a worker shell with no `AB_*` lists its own board, not `main`), then `main`.
