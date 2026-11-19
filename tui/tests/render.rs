//! Render every screen into ratatui's TestBackend from the fixture snapshot and assert on the text.
//! The fixture is real `ab snapshot --json` output (tests/make_fixture.py); tests/test_tui_fixture.py in the Python
//! suite validates it against agentboard/snapshot.schema.json.
use ab_tui::ab::Ab;
use ab_tui::app::{App, Mode, Screen};
use ab_tui::config::{Config, Panel};
use ab_tui::model::{Detail, Snapshot};
use ratatui::backend::TestBackend;
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Terminal;
use std::time::{Duration, Instant};

const FIXTURE: &str = include_str!("fixtures/snapshot.json");

fn app(fx: bool) -> App {
    let cfg = Config { owner: Some("lead".into()), panel: vec![Panel { title: "Digest".into(), command: "true".into(), refresh: 5 }], ..Config::default() };
    let mut app = App::new(cfg, Ab::new(None, None), fx);
    let snap: Snapshot = serde_json::from_str(FIXTURE).expect("fixture parses");
    app.clock = Some(snap.generated_at);
    app.ram = "free RAM 37.8 GiB · room for heavy work 29.8 GiB".into();
    app.gpu = "RTX 3060, 12 %, 4100 MiB".into();
    app.panels = vec!["line one of the digest\nline two".into()];
    app.on_snapshot(snap, Duration::from_millis(97));
    app
}

fn draw(app: &mut App, w: u16, h: u16) -> String {
    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
    term.draw(|f| app.draw(f, Duration::from_millis(16))).unwrap();
    let buf = term.backend().buffer();
    let mut out = String::new();
    for y in 0..buf.area.height {
        for x in 0..buf.area.width {
            out.push_str(buf[(x, y)].symbol());
        }
        out.push('\n');
    }
    out
}

fn key(app: &mut App, c: char) {
    app.on_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
}

fn has(screen: &str, needles: &[&str]) {
    for n in needles {
        assert!(screen.contains(n), "missing {n:?} in:\n{screen}");
    }
}

fn id_of(app: &App, title: &str) -> String {
    app.snap.tasks.iter().find(|t| t.title.starts_with(title)).unwrap_or_else(|| panic!("no task {title}")).id.clone()
}

#[test]
fn fixture_has_every_required_snapshot_key() {
    let schema: serde_json::Value = serde_json::from_str(include_str!("../../agentboard/snapshot.schema.json")).unwrap();
    let fixture: serde_json::Value = serde_json::from_str(FIXTURE).unwrap();
    for key in schema["required"].as_array().unwrap() {
        assert!(fixture.get(key.as_str().unwrap()).is_some(), "fixture lacks required key {key}");
    }
    assert_eq!(fixture["schema"], 1);
}

#[test]
fn header_shows_runner_gauges_pace_cooldowns_ram_gpu() {
    let mut a = app(false);
    let s = draw(&mut a, 150, 44);
    has(
        &s,
        &[
            "agentboard",
            "runner running",
            "slots",
            "2/3",
            "window always",
            "claude 5h",
            "38%",
            "7d",
            "64%",
            "pace 58%",
            "plan guard",
            "cooldowns",
            "opencode-free manual 24:5",
            "free RAM 37.8 GiB",
            "gpu RTX 3060",
        ],
    );
    has(&s, &["1 Board", "2 Running (2)", "4 Decisions (2)", "7 Ops"]);
}

#[test]
fn board_lanes_with_counts_types_and_waits_on() {
    let mut a = app(false);
    let s = draw(&mut a, 220, 44);
    has(
        &s,
        &[
            "ready 4",
            "running 2/3",
            "waiting 4",
            "review 3",
            "done 2",
            "dead 1",
            "Survey the retry",
            "Port the usage",
            "Flaky integration test",
            "↳ waits on",
            "⏸ parked on file results.json",
            "PAUSED",
            "web",
            "caps claude-sub",
        ],
    );
    // dependency of the release card is the build card, shown by id prefix
    let build = id_of(&a, "Add a health endpoint");
    has(&s, &[&build.chars().skip(2).take(4).collect::<String>()]);
}

#[test]
fn board_filter_and_board_switch() {
    let mut a = app(false);
    key(&mut a, '/');
    for c in "harvester".chars() {
        key(&mut a, c);
    }
    let s = draw(&mut a, 220, 44);
    has(&s, &["Port the usage harvester", "filter: harvester"]);
    assert!(!s.contains("Survey the retry"), "filter should hide other cards:\n{s}");
    a.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    a.filter.clear();
    key(&mut a, 'b'); // main
    key(&mut a, 'b'); // web
    let s = draw(&mut a, 220, 44);
    has(&s, &["board: web", "Landing page copy"]);
    assert!(!s.contains("Survey the retry"));
}

#[test]
fn running_screen_rows_bars_and_live_tail() {
    let mut a = app(false);
    a.set_screen(Screen::Running);
    let s = draw(&mut a, 150, 44);
    has(
        &s,
        &[
            "running 2/3",
            "claude:sonnet-5",
            "pi:local-27b",
            "claude-sub",
            "21m00s/",
            "45m00s",
            "Port the usage harvester",
            "output · run 1",
            "Found the new JSONL layout",
            "committing",
        ],
    );
}

#[test]
fn elapsed_ticks_between_polls() {
    let mut a = app(false);
    a.set_screen(Screen::Running);
    a.clock = Some(a.snap.generated_at + 120.0);
    let s = draw(&mut a, 150, 44);
    has(&s, &["23m00s/"]);
}

fn card_detail(id: &str, body: &str) -> Detail {
    serde_json::from_value(serde_json::json!({
        "id": id, "title": "Survey the retry strategies of other task boards", "body": body, "status": "open", "state": "ready",
        "result": "Compared three boards; see notes.", "deps": [], "children": [],
        "notes": [{"id": 1, "author": "worker-1", "body": "Found two boards worth comparing", "ts": 1795077000.0}],
        "runs": [{"id": 10, "harness": "claude", "model": "sonnet-5", "outcome": "done", "started_at": 1795070000.0, "ended_at": 1795073000.0, "input": 41000, "output": 8800}]
    }))
    .unwrap()
}

#[test]
fn card_detail_body_notes_runs_and_dependency_tree() {
    let mut a = app(false);
    let id = id_of(&a, "Survey the retry");
    a.open_card(id.clone());
    a.detail = Some((id, card_detail("x", "# Plan\nRead **three** public boards\n- compare `leases`\n- write notes"), Instant::now()));
    let s = draw(&mut a, 150, 44);
    has(
        &s,
        &[
            "card t_",
            "── Body ──",
            "Plan",
            "Read three public boards",
            "• compare leases",
            "── Notes (1) ──",
            "worker-1",
            "Found two boards worth comparing",
            "── Result ──",
            "Compared three boards",
            "── Runs ──",
            "#10",
            "done",
            "in 41.0k out 8.8k",
            "── Depends on ──",
            "── Blocks ──",
            "Add a health endpoint",
        ],
    );
    // a card with dependencies shows them with state
    let rel = id_of(&a, "Cut release");
    a.open_card(rel);
    let s = draw(&mut a, 150, 44);
    has(&s, &["Depends on", "Add a health endpoint", "Does a shorter lease"]);
}

#[test]
fn decisions_queue_lists_owner_cards_with_options_and_default() {
    let mut a = app(false);
    a.set_screen(Screen::Decisions);
    let id = a.selected_id().unwrap();
    a.detail = Some((id, card_detail("x", "Options:\n- A: 2 slots (safe)\n- B: 4 slots\n\nDefault: A if nobody answers"), Instant::now()));
    let s = draw(&mut a, 150, 44);
    has(
        &s,
        &[
            "waiting for lead (2)",
            "Decide: default slot count",
            "Decide: drop python",
            "options and default",
            "A: 2 slots",
            "Default: A if nobody answers",
            "press a to answer",
        ],
    );
    assert!(!s.contains("Cut release"), "only manual cards of the owner belong here");
    key(&mut a, 'a');
    assert!(matches!(a.mode, Mode::Prompt(_)));
    let s = draw(&mut a, 150, 44);
    has(&s, &["Answer ", "your answer; it is posted as a note"]);
}

#[test]
fn usage_screen_sparklines_and_split() {
    let mut a = app(false);
    a.set_screen(Screen::Usage);
    let s = draw(&mut a, 150, 44);
    has(&s, &["claude-sub", "opencode-free", "5h", "24h", "7d", "LIMITED manual", "held: plan guard", "by harness", "sonnet-5", "local-27b"]);
    assert!(s.chars().any(|c| "▂▃▄▅▆▇█".contains(c)), "sparklines should draw bars:\n{s}");
}

#[test]
fn feed_streams_events_newest_first() {
    let mut a = app(false);
    a.set_screen(Screen::Feed);
    let s = draw(&mut a, 150, 44);
    has(&s, &["events ·", "created", "claimed", "done", "dead", "watch_set", "Flaky integration test", "segfault in the arm64 build"]);
    let first = s.lines().nth(8).unwrap_or("");
    assert!(first.contains("limit") || first.contains("config") || first.contains("message"), "newest event first: {first}");
}

#[test]
fn ops_screen_shows_runner_boards_watchers_schedules_tiers_caps() {
    let mut a = app(false);
    a.set_screen(Screen::Ops);
    let s = draw(&mut a, 150, 44);
    has(
        &s,
        &[
            "Runner",
            "Boards",
            "Watchers",
            "Schedules",
            "Tiers",
            "Caps",
            "setting 2",
            "on-dead",
            "on dead (all boards)",
            "always",
            "ACTIVE",
            "weakest → strongest",
            "claude-sub",
            "max_output=400000",
            "web",
            "PAUSED",
        ],
    );
}

#[test]
fn panels_screen_runs_configured_commands() {
    let mut a = app(false);
    a.set_screen(Screen::Panels);
    let s = draw(&mut a, 150, 44);
    has(&s, &["Digest · every 5s", "line one of the digest"]);
}

#[test]
fn help_palette_and_confirm_overlays() {
    let mut a = app(false);
    key(&mut a, '?');
    has(&draw(&mut a, 150, 44), &["keys · ? or Esc closes", "command palette", "answer a decision card"]);
    a.on_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    key(&mut a, ':');
    for c in "sl".chars() {
        key(&mut a, c);
    }
    has(&draw(&mut a, 150, 44), &[":sl", "slots"]);
    a.on_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    key(&mut a, 'x');
    has(&draw(&mut a, 150, 44), &["confirm", "Cancel ", "any other key"]);
}

#[test]
fn tab_and_number_keys_switch_screens() {
    let mut a = app(false);
    a.on_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
    assert_eq!(a.screen, Screen::Running);
    key(&mut a, '6');
    assert_eq!(a.screen, Screen::Feed);
    a.on_key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::NONE));
    assert_eq!(a.screen, Screen::Usage);
}

#[test]
fn small_terminal_and_empty_board_do_not_panic() {
    let mut a = app(false);
    has(&draw(&mut a, 40, 10), &["terminal too small"]);
    let mut empty = App::new(Config::default(), Ab::new(None, None), false);
    for s in Screen::ALL {
        empty.set_screen(s);
        let out = draw(&mut empty, 120, 30);
        assert!(out.contains("connecting"), "{s:?}:\n{out}");
    }
    empty.err = Some("boom".into());
    has(&draw(&mut empty, 120, 30), &["snapshot error: boom"]);
}

#[test]
fn effects_run_over_many_frames_without_piling_up() {
    let mut a = app(true);
    let mut term = Terminal::new(TestBackend::new(150, 44)).unwrap();
    for i in 0..60 {
        if i == 5 {
            // a card finishes and another dies: effects attach to their rows
            let mut snap = a.snap.clone();
            let run1 = snap.tasks.iter_mut().find(|t| t.title.starts_with("Port the usage")).unwrap();
            run1.status = "done".into();
            run1.state = "done".into();
            let ready = snap.tasks.iter_mut().find(|t| t.title.starts_with("Does a shorter")).unwrap();
            ready.status = "dead".into();
            ready.state = "dead".into();
            a.on_snapshot(snap, Duration::from_millis(50));
        }
        term.draw(|f| a.draw(f, Duration::from_millis(40))).unwrap();
    }
    assert!(a.bell.is_some(), "a dead card rings the bell");
    // only the decisions pulse (repeating) may still be running after 2.4 s of frames
    let s: String = term.backend().buffer().content().iter().map(|c| c.symbol()).collect();
    assert!(s.contains("Decisions"));
}

#[test]
fn new_decision_card_rings_the_bell() {
    let mut a = app(false);
    a.bell = None;
    let mut snap = a.snap.clone();
    let mut card = snap.tasks.iter().find(|t| t.title.starts_with("Decide: default")).unwrap().clone();
    card.id = "t_newdecision".into();
    card.title = "Decide: something new".into();
    snap.tasks.push(card);
    a.on_snapshot(snap, Duration::from_millis(10));
    assert_eq!(a.bell.as_deref(), Some("decision waiting: Decide: something new"));
}

#[test]
fn config_parses_panels_and_defaults() {
    let c: Config = toml_from("poll_ms = 500\nfx = false\n[[panel]]\ntitle = \"GPU\"\ncommand = \"nvidia-smi -L\"\nrefresh = 3\n");
    assert_eq!(c.poll_ms, 500);
    assert!(!c.fx);
    assert!(c.bell);
    assert_eq!(c.panel[0].title, "GPU");
    assert_eq!(c.panel[0].refresh, 3);
}

fn toml_from(s: &str) -> Config {
    let p = std::env::temp_dir().join(format!("ab-tui-cfg-{}.toml", std::process::id()));
    std::fs::write(&p, s).unwrap();
    let c = Config::load(&p).unwrap();
    let _ = std::fs::remove_file(&p);
    c
}

#[test]
fn split_args_honours_quotes() {
    use ab_tui::app::split_args;
    assert_eq!(split_args(r#"--after t_1 --why "not yet" 'x y'"#), vec!["--after", "t_1", "--why", "not yet", "x y"]);
}

#[test]
fn child_environment_is_sanitised() {
    let ab = Ab::new(Some("/tmp/throwaway".into()), Some("me".into()));
    let cmd = ab.command(&["snapshot".into()]);
    let envs: Vec<_> = cmd.get_envs().map(|(k, v)| (k.to_string_lossy().to_string(), v.map(|x| x.to_string_lossy().to_string()))).collect();
    for k in ["AB_TASK", "AB_RUN", "AB_BOARD"] {
        assert!(envs.contains(&(k.to_string(), None)), "{k} must be removed from the child env: {envs:?}");
    }
    assert!(envs.contains(&("AB_HOME".into(), Some("/tmp/throwaway".into()))));
    assert!(envs.contains(&("AB_ACTOR".into(), Some("me".into()))));
}

#[test]
fn slow_terminal_switches_effects_off() {
    let mut a = app(true);
    let mut term = Terminal::new(TestBackend::new(150, 44)).unwrap();
    term.draw(|f| a.draw(f, Duration::from_millis(16))).unwrap(); // starts the sweep-in
    assert!(a.fx.running());
    for _ in 0..24 {
        a.note_frame(80.0);
    }
    assert!(!a.fx.enabled && !a.fx.running(), "24 frames of 80 ms must turn effects off");
    assert!(a.toast.as_ref().is_some_and(|(m, _, _)| m.contains("effects switched off")));
    // fast frames never trip it
    let mut b = app(true);
    term.draw(|f| b.draw(f, Duration::from_millis(16))).unwrap();
    for _ in 0..48 {
        b.note_frame(4.0);
    }
    assert!(b.fx.enabled);
}

#[test]
fn no_fx_flag_means_no_effects() {
    let mut a = app(false);
    let mut term = Terminal::new(TestBackend::new(150, 44)).unwrap();
    term.draw(|f| a.draw(f, Duration::from_millis(16))).unwrap();
    assert!(!a.fx.running());
}

#[test]
fn bad_config_is_reported_not_ignored() {
    let p = std::env::temp_dir().join(format!("ab-tui-badcfg-{}.toml", std::process::id()));
    std::fs::write(&p, "poll_ms = \"fast\"").unwrap();
    assert!(Config::load(&p).is_err());
    let _ = std::fs::remove_file(&p);
    assert!(Config::load(std::path::Path::new("/nonexistent/tui.toml")).unwrap().poll_ms == 1000, "a missing file means defaults");
}

#[test]
fn nvidia_csv_is_made_readable() {
    assert_eq!(ab_tui::workers::fmt_nvidia("NVIDIA GeForce RTX 3060, 2, 1153, 12288"), "NVIDIA GeForce RTX 3060 2% 1.1/12.0 GiB");
    assert_eq!(ab_tui::workers::fmt_nvidia("something else"), "something else");
}

#[test]
fn usage_screen_renders_pacer_launch_budgets() {
    let mut a = app(false);
    let mut v: serde_json::Value = serde_json::from_str(FIXTURE).unwrap();
    let buckets = v["usage"].as_array_mut().unwrap();
    // Inject pacer / launch_budget / budget_why onto buckets via real JSON deserialization
    if let Some(b0) = buckets.get_mut(0) {
        b0["launch_budget"] = serde_json::json!(1);
        b0["budget_why"] = serde_json::json!("pace");
        b0["pacer"] = serde_json::json!({
            "budget": 1, "why": "pace", "interval": "10m", "interval_s": 600.0,
            "used_in_interval": 0, "remaining": 1, "detail": "plan pace"
        });
    }
    if let Some(b1) = buckets.get_mut(1) {
        b1["launch_budget"] = serde_json::json!(3);
        b1["budget_why"] = serde_json::json!("aimd");
        b1["pacer"] = serde_json::json!({
            "budget": 3, "why": "aimd", "interval": "10m", "interval_s": 600.0,
            "used_in_interval": 1, "remaining": 2, "detail": "aimd window"
        });
    }
    if let Some(b2) = buckets.get_mut(2) {
        b2["launch_budget"] = serde_json::json!(0);
        b2["budget_why"] = serde_json::json!("pace");
        b2["pacer"] = serde_json::json!({
            "budget": 0, "why": "pace", "interval": "10m", "interval_s": 600.0,
            "used_in_interval": 0, "remaining": 0, "detail": "over pace"
        });
    }
    let snap: Snapshot = serde_json::from_value(v).expect("snapshot with pacer fields parses");
    a.on_snapshot(snap, Duration::from_millis(10));
    a.set_screen(Screen::Usage);
    let s = draw(&mut a, 150, 44);
    has(&s, &["1/10m pace", "3/10m aimd", "0/10m pace", "LIMITED manual", "held: plan guard"]);
}

#[test]
fn negative_outcome_dead_card_is_neutral_and_skips_glitch_while_real_dead_glitches() {
    let mut a = app(true);
    let mut term = Terminal::new(TestBackend::new(220, 44)).unwrap();
    // Initial draw so startup effect attaches and task rows are recorded
    term.draw(|f| a.draw(f, Duration::from_millis(16))).unwrap();
    a.bell = None;

    // Transition one ready card to dead + outcome=negative: must NOT trigger glitch_dead or bell
    let mut snap = a.snap.clone();
    let neg_id = snap.tasks.iter().find(|t| t.title.starts_with("Does a shorter")).unwrap().id.clone();
    {
        let t = snap.tasks.iter_mut().find(|t| t.id == neg_id).unwrap();
        t.status = "dead".into();
        t.state = "dead".into();
        t.attempts = 1;
        t.labels.insert("outcome".into(), "negative".into());
    }
    if let Some(main_board) = snap.boards.iter_mut().find(|b| b.name == "main") {
        *main_board.counts.entry("dead".into()).or_insert(0) += 1;
    }
    a.on_snapshot(snap.clone(), Duration::from_millis(10));
    term.draw(|f| a.draw(f, Duration::from_millis(16))).unwrap();
    assert!(a.fx.dead_glitches().is_empty(), "negative outcome must not trigger glitch_dead: {:?}", a.fx.dead_glitches());
    assert!(a.bell.is_none(), "negative outcome must not ring the dead bell: {:?}", a.bell);

    // Render board with fx off to inspect text cleanly
    a.fx.disable();
    let s = draw(&mut a, 220, 44);
    // Original fixture has 1 ordinary dead card; adding 1 negative dead card keeps failure count at 1 (+1 neg)
    has(&s, &["dead 1 (+1 neg)", "NEG", "– negative outcome"]);

    // Card detail for the negative card shows NEG badge
    a.open_card(neg_id.clone());
    let detail_screen = draw(&mut a, 150, 44);
    has(&detail_screen, &["NEG", "outcome=negative"]);

    // Now re-enable fx and transition another card to an ordinary dead card: MUST trigger glitch_dead and bell
    a.set_screen(Screen::Board);
    a.fx.enabled = true;
    let err_id = snap.tasks.iter().find(|t| t.title.starts_with("Survey the retry")).unwrap().id.clone();
    {
        let t = snap.tasks.iter_mut().find(|t| t.id == err_id).unwrap();
        t.status = "dead".into();
        t.state = "dead".into();
        t.attempts = 3;
    }
    a.on_snapshot(snap, Duration::from_millis(10));
    term.draw(|f| a.draw(f, Duration::from_millis(16))).unwrap();
    assert!(a.fx.dead_glitches().contains(&err_id), "ordinary dead card must trigger glitch_dead: {:?}", a.fx.dead_glitches());
    assert!(!a.fx.dead_glitches().contains(&neg_id), "negative card must still not be glitched");
    assert!(a.bell.as_deref().is_some_and(|b| b.starts_with("dead:")), "ordinary dead card rings the bell: {:?}", a.bell);
}

#[test]
fn card_detail_renders_worktree_metadata_and_wait_reason() {
    let mut a = app(false);
    let id = id_of(&a, "Survey the retry");
    {
        let t = a.snap.tasks.iter_mut().find(|t| t.id == id).unwrap();
        t.labels.insert("worktree_branch".into(), "ab/t_demo01".into());
        t.labels.insert("worktree_dir".into(), "/tmp/ab-worktrees/t_demo01".into());
        t.labels.insert("worktree".into(), "off".into());
        t.wait_reason = Some("workdir busy (auto worktree off)".into());
    }
    a.open_card(id.clone());
    a.detail = Some((id, card_detail("x", "Body text"), Instant::now()));
    let s = draw(&mut a, 150, 44);
    has(
        &s,
        &[
            "worktree ab/t_demo01",
            "/tmp/ab-worktrees/t_demo01",
            "worktree=off",
            "wait workdir busy (auto worktree off)",
        ],
    );
}

#[test]
fn card_detail_renders_park_condition_and_note() {
    let mut a = app(false);
    let id = id_of(&a, "Score the overnight eval");
    a.open_card(id.clone());
    a.detail = Some((id, card_detail("x", "Body text"), Instant::now()));
    let s = draw(&mut a, 220, 44);
    has(&s, &["park parked on file results.json", "score it and report the number"]);
}

#[test]
fn ops_screen_displays_pacer_and_auto_worktree_state() {
    let mut a = app(false);
    a.snap.runner.pacer = Some(true);
    a.snap.runner.pace_interval = Some("10m".into());
    a.snap.runner.launch_per_tick = Some(2);
    a.snap.runner.auto_worktree = Some(false);
    a.set_screen(Screen::Ops);
    let s = draw(&mut a, 150, 44);
    has(&s, &["pacer on (10m, 2/tick)", "auto_worktree off", "P pacer", "w worktree"]);

    a.snap.runner.pacer = Some(false);
    a.snap.runner.auto_worktree = Some(true);
    let s2 = draw(&mut a, 150, 44);
    has(&s2, &["pacer off (10m, 2/tick)", "auto_worktree on"]);
}
