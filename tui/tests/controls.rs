//! Every control of the TUI, driven through its key handlers, against a throwaway AB_HOME with the real `ab` CLI.
//! Never touches the real board: the home is a fresh temp dir and the child environment is pinned to it.
use ab_tui::ab::Ab;
use ab_tui::app::{App, Mode, Screen};
use ab_tui::config::Config;
use ab_tui::model::Snapshot;
use ab_tui::workers::Msg;
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::path::PathBuf;
use std::sync::mpsc::{channel, Receiver};
use std::time::Duration;

struct Env {
    ab: Ab,
    app: App,
    rx: Receiver<Msg>,
    home: PathBuf,
}

fn ab_script() -> String {
    format!("{}/../ab", env!("CARGO_MANIFEST_DIR"))
}

impl Env {
    fn new() -> Env {
        let home = std::env::temp_dir().join(format!(
            "ab-tui-controls-{}-{}",
            std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));
        std::fs::create_dir_all(&home).unwrap();
        let mut ab = Ab::new(Some(home.clone()), Some("tui-test".into()));
        ab.bin = ab_script();
        let mut app = App::new(Config { owner: Some("lead".into()), ..Config::default() }, ab.clone(), false);
        let (tx, rx) = channel();
        app.tx_msg = Some(tx);
        Env { ab, app, rx, home }
    }

    fn cli(&self, args: &[&str]) -> String {
        let a: Vec<String> = args.iter().map(|s| s.to_string()).collect();
        self.ab.run(&a, Duration::from_secs(60)).unwrap_or_else(|e| panic!("ab {args:?}: {e}"))
    }

    fn add(&self, title: &str, extra: &[&str]) -> String {
        let mut a = vec!["add", title];
        a.extend_from_slice(extra);
        self.cli(&a).split_whitespace().next().unwrap().to_string()
    }

    fn snap(&mut self) -> Snapshot {
        let s: Snapshot = serde_json::from_str(&self.cli(&["snapshot", "--json", "--closed", "50"])).unwrap();
        self.app.on_snapshot(s.clone(), Duration::from_millis(1));
        s
    }

    fn key(&mut self, c: char) {
        self.app.on_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
    }

    fn code(&mut self, k: KeyCode) {
        self.app.on_key(KeyEvent::new(k, KeyModifiers::NONE));
    }

    fn type_text(&mut self, s: &str) {
        for c in s.chars() {
            self.key(c);
        }
    }

    /// Wait for the action the last key started and hand its result to the app, like the main loop does.
    fn finish(&mut self) -> String {
        loop {
            match self.rx.recv_timeout(Duration::from_secs(60)).expect("the action must report back") {
                Msg::Action(label, r) => {
                    let ok = r.is_ok();
                    self.app.on_msg(Msg::Action(label.clone(), r.clone()));
                    assert!(ok, "{label} failed: {r:?}");
                    return label;
                }
                other => self.app.on_msg(other),
            }
        }
    }

    fn select(&mut self, id: &str) {
        self.snap();
        self.app.set_screen(Screen::Board);
        let lanes = self.app.lanes();
        let (lane, idx) = lanes.iter().enumerate().find_map(|(l, ts)| ts.iter().position(|t| t.id == id).map(|i| (l, i))).expect("card on the board");
        self.app.sel.lane = lane;
        self.app.sel.lane_idx[lane] = idx;
    }

    fn task(&mut self, id: &str) -> ab_tui::model::Task {
        self.snap().tasks.into_iter().find(|t| t.id == id).unwrap_or_else(|| panic!("{id} not in the snapshot"))
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.home);
    }
}

#[test]
fn every_control_works_against_a_throwaway_board() {
    let mut e = Env::new();
    assert!(e.home.starts_with(std::env::temp_dir()), "tests only ever touch a temp AB_HOME");
    let t1 = e.add("first card", &["--body", "do the thing", "--tier", "low"]);
    let t2 = e.add("second card", &["--tier", "low"]);
    let dec = e.add("Decide: pick one", &["--manual", "--label", "owner=lead", "--body", "Options:\n- A\n- B\nDefault: A"]);
    e.cli(&["watch", "add", "on-dead", "--on", "dead", "--brief", "look"]);
    e.cli(&["sched", "add", "night", "--window", "00:00-23:59", "--slots", "2"]);
    e.cli(&["boards", "create", "web"]);

    // note (n): Enter opens the prompt, text is posted through `ab note`
    e.select(&t1);
    e.key('n');
    assert!(matches!(e.app.mode, Mode::Prompt(_)));
    e.type_text("a note from the tui");
    e.code(KeyCode::Enter);
    e.finish();
    assert!(e.cli(&["show", &t1, "--json"]).contains("a note from the tui"));

    // priority (P), tier (T), harness (H)
    e.select(&t1);
    e.key('P');
    for _ in 0..4 {
        e.code(KeyCode::Backspace);
    }
    e.type_text("7");
    e.code(KeyCode::Enter);
    e.finish();
    assert_eq!(e.task(&t1).priority, 7);
    e.select(&t1);
    e.key('T');
    for _ in 0..8 {
        e.code(KeyCode::Backspace);
    }
    e.type_text("max");
    e.code(KeyCode::Enter);
    e.finish();
    assert_eq!(e.task(&t1).tier, "max");
    e.select(&t1);
    e.key('H');
    e.type_text("claude");
    e.code(KeyCode::Enter);
    e.finish();
    assert_eq!(e.task(&t1).harness.as_deref(), Some("claude"));

    // defer (D) parks the card until a time
    e.select(&t2);
    e.key('D');
    e.code(KeyCode::Enter); // the prompt starts with `--until +1h`
    e.finish();
    let t = e.task(&t2);
    assert_eq!(t.state, "waiting", "deferred card waits: {t:?}");
    assert!(t.not_before.is_some());

    // cancel (x) asks first; any key but y backs out
    e.select(&t1);
    e.key('x');
    assert!(matches!(e.app.mode, Mode::Confirm(_)));
    e.key('n');
    assert!(matches!(e.app.mode, Mode::Normal));
    assert_eq!(e.task(&t1).status, "open", "a declined confirmation changes nothing");
    e.select(&t1);
    e.key('x');
    e.key('y');
    e.finish();
    assert_eq!(e.task(&t1).status, "cancelled");

    // retry (r) reopens it (after confirmation); closed cards are on the board's dead lane
    e.select(&t1);
    e.key('r');
    assert!(matches!(e.app.mode, Mode::Confirm(_)));
    e.key('y');
    e.finish();
    assert_eq!(e.task(&t1).status, "open");

    // kill (K) on a card that is not running says so instead of acting
    e.select(&t1);
    e.key('K');
    assert!(e.app.toast.as_ref().is_some_and(|(m, _, err)| *err && m.contains("no live run")));

    // answer a decision card inline: note + done
    e.snap();
    e.app.set_screen(Screen::Decisions);
    assert_eq!(e.app.selected_id().as_deref(), Some(dec.as_str()));
    e.key('a');
    e.type_text("B, because it is smaller");
    e.code(KeyCode::Enter);
    e.finish();
    let shown = e.cli(&["show", &dec, "--json"]);
    assert!(shown.contains("B, because it is smaller") && shown.contains("\"status\": \"done\""), "{shown}");

    // runner controls from the palette: pause, resume, slots N, slots +
    for (cmd, check) in [("pause", true), ("resume", false)] {
        e.key(':');
        e.type_text(cmd);
        e.code(KeyCode::Enter);
        e.finish();
        assert_eq!(e.snap().runner.paused, check, "{cmd}");
    }
    e.key(':');
    e.type_text("slots 4");
    e.code(KeyCode::Enter);
    e.finish();
    assert_eq!(e.snap().runner.slots_setting, 4);
    e.app.set_screen(Screen::Ops);
    e.key('+');
    e.finish();
    assert_eq!(e.snap().runner.slots_setting, 5);
    e.key('-');
    e.finish();
    assert_eq!(e.snap().runner.slots_setting, 4);
    // drain asks first
    e.key('d');
    assert!(matches!(e.app.mode, Mode::Confirm(_)));
    e.code(KeyCode::Esc);
    assert!(!e.snap().runner.draining);

    // pacer (P) and auto_worktree (w) toggles via `ab config set`
    assert_eq!(e.snap().runner.pacer, Some(true));
    e.key('P');
    e.finish();
    assert_eq!(e.snap().runner.pacer, Some(false));
    e.key('P');
    e.finish();
    assert_eq!(e.snap().runner.pacer, Some(true));

    assert_eq!(e.snap().runner.auto_worktree, Some(true));
    e.key('w');
    e.finish();
    assert_eq!(e.snap().runner.auto_worktree, Some(false));
    e.key('w');
    e.finish();
    assert_eq!(e.snap().runner.auto_worktree, Some(true));

    // board pause / resume (Ops > Boards)
    e.app.sel.ops = 1;
    e.app.sel.ops_idx[1] = e.snap().boards.iter().position(|b| b.name == "web").unwrap();
    e.code(KeyCode::Enter);
    assert!(matches!(e.app.mode, Mode::Confirm(_)));
    e.key('y');
    e.finish();
    assert!(e.snap().boards.iter().find(|b| b.name == "web").unwrap().paused);
    e.code(KeyCode::Enter);
    e.finish();
    assert!(!e.snap().boards.iter().find(|b| b.name == "web").unwrap().paused);

    // watcher and schedule toggles
    e.app.sel.ops = 2;
    e.code(KeyCode::Enter);
    e.finish();
    assert!(!e.snap().watchers[0].enabled);
    e.code(KeyCode::Enter);
    e.finish();
    assert!(e.snap().watchers[0].enabled);
    e.app.sel.ops = 3;
    e.code(KeyCode::Enter);
    e.finish();
    assert!(!e.snap().schedules[0].enabled);

    // tier reorder with J
    e.app.sel.ops = 4;
    e.app.sel.ops_idx[4] = 0;
    let before = e.snap().tiers.order.clone();
    e.key('J');
    e.finish();
    let after = e.snap().tiers.order.clone();
    assert_eq!(after[1], before[0], "J moves the tier down one place: {before:?} -> {after:?}");
    assert_eq!(after[0], before[1]);

    // raw passthrough from the palette
    e.key(':');
    e.type_text(&format!("ab note {t2} \"raw verb works\""));
    e.code(KeyCode::Enter);
    e.finish();
    assert!(e.cli(&["show", &t2, "--json"]).contains("raw verb works"));

    // a failing verb reports its error in the toast instead of crashing
    e.key(':');
    e.type_text("ab show t_doesnotexist");
    e.code(KeyCode::Enter);
    match e.rx.recv_timeout(Duration::from_secs(30)).unwrap() {
        Msg::Action(_, r) => assert!(r.is_err()),
        _ => panic!("expected an action result"),
    }
}

#[test]
fn poller_reads_a_real_snapshot_and_follows_events_incrementally() {
    use ab_tui::workers::{spawn_poller, PollCmd};
    let e = Env::new();
    let t1 = e.add("polled card", &[]);
    let (tx, rx) = channel();
    let (ctx, crx) = channel();
    let cfg = Config { poll_ms: 300, ..Config::default() };
    spawn_poller(e.ab.clone(), cfg, tx, crx);
    let first = match rx.recv_timeout(Duration::from_secs(60)).unwrap() {
        Msg::Snapshot(s, _) => s,
        _ => panic!("expected a snapshot"),
    };
    assert!(first.tasks.iter().any(|t| t.id == t1));
    assert!(first.events.iter().any(|ev| ev.kind == "created"), "the first poll seeds the feed with history");
    let cursor = first.events_cursor;
    e.cli(&["note", &t1, "later"]);
    ctx.send(PollCmd::Refresh).unwrap();
    let second = loop {
        match rx.recv_timeout(Duration::from_secs(60)).unwrap() {
            Msg::Snapshot(s, _) if s.events.iter().any(|ev| ev.kind == "message") => break s,
            Msg::Snapshot(..) => continue,
            _ => continue,
        }
    };
    assert!(second.events.iter().all(|ev| ev.id > cursor), "later polls only carry events after the cursor");
    ctx.send(PollCmd::Stop).unwrap();
}
