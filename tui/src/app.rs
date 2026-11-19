//! Application state, key handling and the diff logic that decides when a bell or an effect fires.
//! No board logic lives here: every action is an `ab` verb run by `workers::run_action`.
use crate::ab::Ab;
use crate::config::Config;
use crate::fx::Fx;
use crate::model::{Detail, Event, Snapshot, Task};
use crate::workers::{self, Msg, PollCmd};
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;
use ratatui::Frame;
use std::collections::{HashMap, VecDeque};
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

pub const LANES: [&str; 6] = ["ready", "running", "waiting", "review", "done", "dead"];
pub const FEED_MAX: usize = 1500;
pub const OPS_SECTIONS: [&str; 6] = ["Runner", "Boards", "Watchers", "Schedules", "Tiers", "Caps"];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Screen {
    Board,
    Running,
    Card,
    Decisions,
    Usage,
    Feed,
    Ops,
    Panels,
}

impl Screen {
    pub const ALL: [Screen; 8] = [Screen::Board, Screen::Running, Screen::Card, Screen::Decisions, Screen::Usage, Screen::Feed, Screen::Ops, Screen::Panels];
    pub fn title(self) -> &'static str {
        match self {
            Screen::Board => "Board",
            Screen::Running => "Running",
            Screen::Card => "Card",
            Screen::Decisions => "Decisions",
            Screen::Usage => "Usage",
            Screen::Feed => "Feed",
            Screen::Ops => "Ops",
            Screen::Panels => "Panels",
        }
    }
    pub fn index(self) -> usize {
        Screen::ALL.iter().position(|s| *s == self).unwrap_or(0)
    }
    pub fn from_name(n: &str) -> Option<Screen> {
        Screen::ALL.iter().copied().find(|s| s.title().eq_ignore_ascii_case(n))
    }
}

#[derive(Clone, Debug)]
pub enum PromptKind {
    Note(String),
    Priority(String),
    Tier(String),
    Harness(String),
    Defer(String),
    Answer(String),
    Raw,
}

#[derive(Clone, Debug)]
pub struct Prompt {
    pub title: String,
    pub hint: String,
    pub buf: String,
    pub kind: PromptKind,
}

#[derive(Clone, Debug)]
pub struct Confirm {
    pub text: String,
    pub label: String,
    pub args: Vec<String>,
}

#[derive(Clone, Debug)]
pub enum Mode {
    Normal,
    Filter,
    Palette,
    Prompt(Prompt),
    Confirm(Confirm),
    Help,
}

#[derive(Clone, Debug)]
pub enum Hit {
    Tab(Screen),
    Lane(usize, usize),
    Run(usize),
    Decision(usize),
    Feed(usize),
}

#[derive(Clone, Debug)]
pub enum Suspend {
    Shell(String),
    Editor(String),
}

#[derive(Clone, Copy, Debug)]
enum PendingKind {
    Done,
    Dead,
}

#[derive(Default, Clone, Debug)]
pub struct Sel {
    pub board: usize,
    pub lane: usize,
    pub lane_idx: [usize; 6],
    pub run: usize,
    pub dec: usize,
    pub feed: usize,
    pub ops: usize,
    pub ops_idx: [usize; 6],
    pub card_scroll: u16,
    pub usage_scroll: u16,
}

pub struct App {
    pub snap: Snapshot,
    pub have_snap: bool,
    pub err: Option<String>,
    pub took: Duration,
    pub cfg: Config,
    pub ab: Ab,
    pub screen: Screen,
    pub sel: Sel,
    pub mode: Mode,
    pub input: String,
    pub filter: String,
    pub show_all_manual: bool,
    pub card: Option<String>,
    pub detail: Option<(String, Detail, Instant)>,
    pub detail_err: Option<String>,
    detail_wanted: Option<(String, Instant)>,
    pub feed: VecDeque<Event>,
    pub feed_last: i64,
    pub toast: Option<(String, Instant, bool)>,
    pub ram: String,
    pub gpu: String,
    pub panels: Vec<String>,
    pub fx: Fx,
    pub clock: Option<f64>,
    pub quit: bool,
    pub suspend: Option<Suspend>,
    pub bell: Option<String>,
    pub tx_poll: Option<Sender<PollCmd>>,
    pub tx_msg: Option<Sender<Msg>>,
    tail_sent: Option<i64>,
    // geometry recorded by the last render: effects and the mouse need to know where things are
    pub rows: HashMap<String, Rect>,
    pub hits: Vec<(Rect, Hit)>,
    pub content: Rect,
    pub feed_area: Rect,
    pending: Vec<(PendingKind, String, Instant)>,
    transition_pending: bool,
    startup_pending: bool,
    feed_fresh: usize,
    pub frame_ms: VecDeque<f64>,
}

impl App {
    pub fn new(cfg: Config, ab: Ab, fx_on: bool) -> App {
        let panels = vec![String::from("…"); cfg.panel.len()];
        App {
            snap: Snapshot::default(),
            have_snap: false,
            err: None,
            took: Duration::ZERO,
            ab,
            screen: Screen::Board,
            sel: Sel::default(),
            mode: Mode::Normal,
            input: String::new(),
            filter: String::new(),
            show_all_manual: false,
            card: None,
            detail: None,
            detail_err: None,
            detail_wanted: None,
            feed: VecDeque::new(),
            feed_last: 0,
            toast: None,
            ram: String::new(),
            gpu: String::new(),
            panels,
            fx: Fx::new(fx_on && cfg.fx),
            clock: None,
            quit: false,
            suspend: None,
            bell: None,
            tx_poll: None,
            tx_msg: None,
            tail_sent: None,
            rows: HashMap::new(),
            hits: vec![],
            content: Rect::default(),
            feed_area: Rect::default(),
            pending: vec![],
            transition_pending: false,
            startup_pending: true,
            feed_fresh: 0,
            frame_ms: VecDeque::new(),
            cfg,
        }
    }

    pub fn now(&self) -> f64 {
        self.clock.unwrap_or_else(|| std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0))
    }

    /// Seconds since the snapshot was taken: lets elapsed times and countdowns tick between polls.
    pub fn age(&self) -> f64 {
        if self.snap.generated_at > 0.0 {
            (self.now() - self.snap.generated_at).max(0.0)
        } else {
            0.0
        }
    }

    pub fn owner(&self) -> String {
        self.cfg.owner.clone().unwrap_or_else(|| self.snap.actor.clone())
    }

    pub fn toast(&mut self, text: impl Into<String>, err: bool) {
        self.toast = Some((text.into(), Instant::now(), err));
    }

    // ------------------------------------------------------------------ data views
    pub fn board_name(&self) -> Option<&str> {
        if self.sel.board == 0 {
            None
        } else {
            self.snap.boards.get(self.sel.board - 1).map(|b| b.name.as_str())
        }
    }

    pub fn matches_filter(&self, t: &Task) -> bool {
        if self.filter.is_empty() {
            return true;
        }
        let f = self.filter.to_lowercase();
        let hay = format!(
            "{} {} {} {} {} {}",
            t.id,
            t.title,
            t.tier,
            t.state,
            t.harness.clone().unwrap_or_default(),
            t.labels.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join(" ")
        )
        .to_lowercase();
        f.split_whitespace().all(|w| hay.contains(w))
    }

    pub fn lane_of(t: &Task) -> usize {
        match t.state.as_str() {
            "ready" if t.runnable => 0,
            "ready" => 3,
            "claimed" => 1,
            "done" => 4,
            "dead" | "cancelled" => 5,
            _ => 2,
        }
    }

    pub fn lanes(&self) -> [Vec<&Task>; 6] {
        let mut lanes: [Vec<&Task>; 6] = Default::default();
        let board = self.board_name();
        for t in &self.snap.tasks {
            if board.is_some_and(|b| b != t.board) || !self.matches_filter(t) {
                continue;
            }
            lanes[Self::lane_of(t)].push(t);
        }
        for l in [4, 5] {
            lanes[l].sort_by(|a, b| b.updated_at.partial_cmp(&a.updated_at).unwrap_or(std::cmp::Ordering::Equal));
        }
        lanes
    }

    pub fn decisions(&self) -> Vec<&Task> {
        let me = self.owner();
        self.snap
            .tasks
            .iter()
            .filter(|t| !t.runnable && matches!(t.status.as_str(), "open" | "claimed"))
            .filter(|t| self.show_all_manual || t.labels.get("owner").is_some_and(|o| *o == me))
            .filter(|t| self.matches_filter(t))
            .collect()
    }

    pub fn task(&self, id: &str) -> Option<&Task> {
        self.snap.tasks.iter().find(|t| t.id == id)
    }

    pub fn selected_id(&self) -> Option<String> {
        match self.screen {
            Screen::Board => self.lanes()[self.sel.lane].get(self.sel.lane_idx[self.sel.lane]).map(|t| t.id.clone()),
            Screen::Running => self.snap.runner.children.get(self.sel.run).map(|c| c.task_id.clone()),
            Screen::Decisions => self.decisions().get(self.sel.dec).map(|t| t.id.clone()),
            Screen::Card => self.card.clone(),
            Screen::Feed => self.feed.iter().rev().nth(self.sel.feed).and_then(|e| e.task_id.clone()),
            _ => None,
        }
    }

    fn board_of_task(&self, id: &str) -> Option<String> {
        self.task(id).map(|t| t.board.clone())
    }

    pub fn running_children(&self) -> usize {
        self.snap.runner.children.len()
    }

    // ------------------------------------------------------------------ messages from workers
    pub fn on_msg(&mut self, m: Msg) {
        match m {
            Msg::Snapshot(s, took) => self.on_snapshot(*s, took),
            Msg::SnapshotErr(e) => self.err = Some(e),
            Msg::Detail(id, r) => match r {
                Ok(d) => {
                    self.detail_err = None;
                    self.detail = Some((id, *d, Instant::now()));
                }
                Err(e) => self.detail_err = Some(e),
            },
            Msg::Action(label, r) => {
                match r {
                    Ok(s) => self.toast(format!("{label}: {}", if s.is_empty() { "ok" } else { &s }), false),
                    Err(e) => self.toast(format!("{label} failed: {e}"), true),
                }
                self.refresh();
                self.detail_wanted = None;
                self.detail = None;
            }
            Msg::Ram(s) => self.ram = s,
            Msg::Gpu(s) => self.gpu = s,
            Msg::Panel(i, s) => {
                if let Some(p) = self.panels.get_mut(i) {
                    *p = s;
                }
            }
        }
    }

    pub fn on_snapshot(&mut self, mut s: Snapshot, took: Duration) {
        self.err = None;
        self.took = took;
        let first = !self.have_snap;
        let events = std::mem::take(&mut s.events);
        // what changed since the last snapshot decides the bells and the effects
        let old: HashMap<String, String> = self.snap.tasks.iter().map(|t| (t.id.clone(), t.status.clone())).collect();
        let before_dec: Vec<String> = self.decisions().iter().map(|t| t.id.clone()).collect();
        let gen = s.generated_at;
        if !first {
            for t in &s.tasks {
                let recent = t.updated_at.is_some_and(|u| u > gen - 15.0);
                let changed = match old.get(&t.id) {
                    Some(prev) => *prev != t.status,
                    None => recent,
                };
                if !changed {
                    continue;
                }
                match t.status.as_str() {
                    "done" => self.pending.push((PendingKind::Done, t.id.clone(), Instant::now())),
                    "dead" if !t.is_negative() => {
                        self.pending.push((PendingKind::Dead, t.id.clone(), Instant::now()));
                        self.ring(format!("dead: {}", t.title));
                    }
                    _ => {}
                }
            }
        }
        self.snap = s;
        if !first {
            let new_dec: Vec<(String, String)> =
                self.decisions().iter().filter(|t| !before_dec.contains(&t.id)).map(|t| (t.id.clone(), t.title.clone())).collect();
            for (_, title) in new_dec {
                self.ring(format!("decision waiting: {title}"));
            }
        }
        self.have_snap = true;
        // feed: the first poll seeds history quietly; later events are live
        let mut fresh = 0;
        for e in events {
            if e.id > self.feed_last {
                self.feed_last = e.id;
                self.feed.push_back(e);
                fresh += 1;
            }
        }
        while self.feed.len() > FEED_MAX {
            self.feed.pop_front();
        }
        if !first && fresh > 0 {
            self.feed_fresh = fresh.min(12);
        }
        self.clamp_selection();
    }

    fn ring(&mut self, text: String) {
        if self.cfg.bell {
            self.bell = Some(text);
        }
    }

    pub fn clamp_selection(&mut self) {
        let lanes = self.lanes();
        let lens: Vec<usize> = lanes.iter().map(Vec::len).collect();
        for (i, n) in lens.iter().enumerate() {
            self.sel.lane_idx[i] = self.sel.lane_idx[i].min(n.saturating_sub(1));
        }
        self.sel.run = self.sel.run.min(self.snap.runner.children.len().saturating_sub(1));
        let nd = self.decisions().len();
        self.sel.dec = self.sel.dec.min(nd.saturating_sub(1));
        self.sel.feed = self.sel.feed.min(self.feed.len().saturating_sub(1));
        self.sel.board = self.sel.board.min(self.snap.boards.len());
        let lens = self.ops_lens();
        for (i, n) in lens.iter().enumerate() {
            self.sel.ops_idx[i] = self.sel.ops_idx[i].min(n.saturating_sub(1));
        }
    }

    pub fn ops_lens(&self) -> [usize; 6] {
        [0, self.snap.boards.len(), self.snap.watchers.len(), self.snap.schedules.len(), self.snap.tiers.order.len(), self.snap.caps.len()]
    }

    // ------------------------------------------------------------------ talking to workers
    pub fn refresh(&mut self) {
        if let Some(t) = &self.tx_poll {
            let _ = t.send(PollCmd::Refresh);
        }
    }

    pub fn set_screen(&mut self, s: Screen) {
        if s != self.screen {
            self.screen = s;
            self.transition_pending = true;
            if s == Screen::Card && self.card.is_none() {
                self.card = self.selected_id_for_card();
            }
        }
        self.after_navigation();
    }

    fn selected_id_for_card(&self) -> Option<String> {
        self.lanes().iter().flatten().next().map(|t| t.id.clone()).or_else(|| self.snap.tasks.first().map(|t| t.id.clone()))
    }

    pub fn open_card(&mut self, id: String) {
        self.card = Some(id);
        self.sel.card_scroll = 0;
        self.detail = None;
        self.set_screen(Screen::Card);
    }

    /// Called after any move: fetch card details for the card in view and follow the selected run's output.
    pub fn after_navigation(&mut self) {
        let want = match self.screen {
            Screen::Card | Screen::Decisions => self.selected_id(),
            _ => None,
        };
        if let Some(id) = want {
            let have = self.detail.as_ref().is_some_and(|(d, _, at)| *d == id && at.elapsed() < Duration::from_secs(6));
            let asked = self.detail_wanted.as_ref().is_some_and(|(d, at)| *d == id && at.elapsed() < Duration::from_secs(6));
            if !have && !asked {
                if let Some(tx) = self.tx_msg.clone() {
                    let b = self.board_of_task(&id);
                    workers::fetch_detail(self.ab.clone(), id.clone(), b, tx);
                }
                self.detail_wanted = Some((id, Instant::now()));
            }
        }
        let tail = if self.screen == Screen::Running { self.snap.runner.children.get(self.sel.run).map(|c| c.run) } else { None };
        if tail != self.tail_sent {
            self.tail_sent = tail;
            if let Some(t) = &self.tx_poll {
                let _ = t.send(PollCmd::Tail(tail));
            }
        }
    }

    pub fn tick(&mut self) {
        if let Some((_, at, _)) = &self.toast {
            if at.elapsed() > Duration::from_secs(6) {
                self.toast = None;
            }
        }
        self.after_navigation();
    }

    pub fn act(&mut self, label: impl Into<String>, args: Vec<String>) {
        let label = label.into();
        match self.tx_msg.clone() {
            Some(tx) => {
                self.toast(format!("{label} …"), false);
                workers::run_action(self.ab.clone(), label, args, tx);
            }
            None => self.toast(format!("{label}: not connected"), true),
        }
    }

    pub fn ask(&mut self, text: impl Into<String>, label: impl Into<String>, args: Vec<String>) {
        self.mode = Mode::Confirm(Confirm { text: text.into(), label: label.into(), args });
    }

    fn task_verb(&self, id: &str, verb: &[&str]) -> Vec<String> {
        let mut a: Vec<String> = vec![];
        if let Some(b) = self.board_of_task(id) {
            a.push("-B".into());
            a.push(b);
        }
        a.extend(verb.iter().map(|s| s.to_string()));
        a
    }

    fn with_id(&self, id: &str, verb: &[&str], tail: &[String]) -> Vec<String> {
        let mut a = self.task_verb(id, verb);
        a.push(id.to_string());
        a.extend(tail.iter().cloned());
        a
    }

    fn short(id: &str) -> String {
        id.chars().take(8).collect()
    }

    fn prompt(&mut self, title: &str, hint: &str, buf: &str, kind: PromptKind) {
        self.mode = Mode::Prompt(Prompt { title: title.into(), hint: hint.into(), buf: buf.into(), kind });
    }

    // ------------------------------------------------------------------ actions on the selected card
    fn need_card(&mut self) -> Option<String> {
        let id = self.selected_id();
        if id.is_none() {
            self.toast("no card selected", true);
        }
        id
    }

    pub fn do_retry(&mut self) {
        if let Some(id) = self.need_card() {
            let args = self.with_id(&id, &["retry"], &[]);
            self.ask(format!("Retry {} (reopen with fresh attempts)?", Self::short(&id)), format!("retry {}", Self::short(&id)), args);
        }
    }

    pub fn do_cancel(&mut self) {
        if let Some(id) = self.need_card() {
            let args = self.with_id(&id, &["cancel"], &[]);
            self.ask(format!("Cancel {} for good? A running agent is killed.", Self::short(&id)), format!("cancel {}", Self::short(&id)), args);
        }
    }

    pub fn do_kill(&mut self) {
        let run = match self.screen {
            Screen::Running => self.snap.runner.children.get(self.sel.run).map(|c| c.run),
            _ => self.selected_id().and_then(|id| self.task(&id).and_then(|t| t.run_id)),
        };
        match run {
            Some(r) => self.ask(
                format!("Kill run {r}? The card goes back to the queue, no attempt counted."),
                format!("kill run {r}"),
                vec!["kill".into(), r.to_string()],
            ),
            None => self.toast("that card has no live run", true),
        }
    }

    pub fn do_prompt(&mut self, which: char) {
        let Some(id) = self.need_card() else { return };
        let cur = self.task(&id).cloned();
        match which {
            'n' => self.prompt(&format!("Note on {}", Self::short(&id)), "Enter to post, Esc to cancel", "", PromptKind::Note(id)),
            'P' => {
                let v = cur.map(|t| t.priority.to_string()).unwrap_or_default();
                self.prompt(&format!("Priority of {}", Self::short(&id)), "integer, higher runs first", &v, PromptKind::Priority(id))
            }
            'T' => {
                let v = cur.map(|t| t.tier).unwrap_or_default();
                let hint = format!("one of: {}", self.snap.tiers.order.join(", "));
                self.prompt(&format!("Tier of {}", Self::short(&id)), &hint, &v, PromptKind::Tier(id))
            }
            'H' => {
                let v = cur.and_then(|t| t.harness).unwrap_or_default();
                self.prompt(&format!("Harness of {}", Self::short(&id)), "harness name (see `ab harness`)", &v, PromptKind::Harness(id))
            }
            'D' => self.prompt(
                &format!("Defer {}", Self::short(&id)),
                "e.g. --until +2h   or   --after t_xxxx --why \"reason\"",
                "--until +1h",
                PromptKind::Defer(id),
            ),
            'a' => {
                self.prompt(&format!("Answer {}", Self::short(&id)), "your answer; it is posted as a note and the card is closed", "", PromptKind::Answer(id))
            }
            _ => {}
        }
    }

    pub fn submit_prompt(&mut self, p: Prompt) {
        let text = p.buf.trim().to_string();
        let empty_ok = matches!(p.kind, PromptKind::Harness(_));
        if text.is_empty() && !empty_ok {
            return;
        }
        match p.kind {
            PromptKind::Note(id) => {
                let a = self.with_id(&id, &["note"], &[text]);
                self.act(format!("note on {}", Self::short(&id)), a)
            }
            PromptKind::Priority(id) => {
                let a = self.with_id(&id, &["edit"], &["--priority".into(), text]);
                self.act(format!("priority of {}", Self::short(&id)), a)
            }
            PromptKind::Tier(id) => {
                let a = self.with_id(&id, &["edit"], &["--tier".into(), text]);
                self.act(format!("tier of {}", Self::short(&id)), a)
            }
            PromptKind::Harness(id) => {
                let a = self.with_id(&id, &["edit"], &["--harness".into(), text]);
                self.act(format!("harness of {}", Self::short(&id)), a)
            }
            PromptKind::Defer(id) => {
                let tail = split_args(&text);
                let a = self.with_id(&id, &["defer"], &tail);
                self.act(format!("defer {}", Self::short(&id)), a)
            }
            PromptKind::Answer(id) => {
                let a = self.task_verb(&id, &["decision", "answer"]);
                let mut a = a;
                a.push(id.clone());
                a.push(text);
                self.act(format!("answer {}", Self::short(&id)), a)
            }
            PromptKind::Raw => {
                let a = split_args(&text);
                self.act(format!("ab {}", text.chars().take(24).collect::<String>()), a)
            }
        }
    }

    pub fn do_suspend(&mut self, editor: bool) {
        let Some(id) = self.need_card() else { return };
        let dir = self
            .task(&id)
            .and_then(|t| t.labels.get("worktree_dir").filter(|d| std::path::Path::new(d.as_str()).is_dir()).cloned().or_else(|| t.workdir.clone()))
            .or_else(|| self.board_of_task(&id).and_then(|b| self.snap.boards.iter().find(|x| x.name == b).and_then(|x| x.default_workdir.clone())));
        match dir {
            Some(d) if std::path::Path::new(&d).is_dir() => self.suspend = Some(if editor { Suspend::Editor(d) } else { Suspend::Shell(d) }),
            Some(d) => self.toast(format!("workdir {d} does not exist here"), true),
            None => self.toast("that card has no workdir", true),
        }
    }

    // ------------------------------------------------------------------ runner / ops
    pub fn set_config_bool(&mut self, key: &str, val: bool) {
        let v = if val { "true" } else { "false" };
        self.act(format!("{key} {v}"), vec!["config".into(), "set".into(), key.into(), v.into()]);
    }
    pub fn runner_verb(&mut self, verb: &str) {
        match verb {
            "pause" | "resume" | "start" | "run" => self.act(format!("runner {verb}"), vec!["runner".into(), verb.into()]),
            "drain" | "reload" | "stop" => {
                let text = match verb {
                    "drain" => "Drain the runner? It starts nothing new and stops when the running agents finish.",
                    "reload" => "Reload the runner? It re-executes with the code on disk and keeps running agents.",
                    _ => "Stop the runner after the running agents finish?",
                };
                self.ask(text, format!("runner {verb}"), vec!["runner".into(), verb.into()]);
            }
            _ => {}
        }
    }

    pub fn slots_delta(&mut self, d: i64) {
        let n = (self.snap.runner.slots_setting + d).max(0);
        self.act(format!("slots {n}"), vec!["runner".into(), "slots".into(), n.to_string()]);
    }

    pub fn ops_enter(&mut self) {
        let i = self.sel.ops_idx[self.sel.ops];
        match self.sel.ops {
            1 => {
                if let Some(b) = self.snap.boards.get(i).cloned() {
                    let flag = if b.paused { "--resume" } else { "--pause" };
                    let args = vec!["boards".into(), "set".into(), b.name.clone(), flag.into()];
                    if b.paused {
                        self.act(format!("resume board {}", b.name), args)
                    } else {
                        self.ask(format!("Pause board {}? Nothing on it starts until resumed.", b.name), format!("pause board {}", b.name), args)
                    }
                }
            }
            2 => {
                if let Some(w) = self.snap.watchers.get(i).cloned() {
                    let verb = if w.enabled { "disable" } else { "enable" };
                    self.act(format!("{verb} watcher {}", w.name), vec!["watch".into(), verb.into(), w.name]);
                }
            }
            3 => {
                if let Some(s) = self.snap.schedules.get(i).cloned() {
                    let verb = if s.enabled { "disable" } else { "enable" };
                    self.act(format!("{verb} schedule {}", s.name), vec!["sched".into(), verb.into(), s.name]);
                }
            }
            _ => {}
        }
    }

    pub fn tier_move(&mut self, d: i64) {
        let i = self.sel.ops_idx[4];
        let mut order = self.snap.tiers.order.clone();
        let j = i as i64 + d;
        if j < 0 || j as usize >= order.len() {
            return;
        }
        order.swap(i, j as usize);
        self.sel.ops_idx[4] = j as usize;
        self.act("reorder tiers", vec!["tiers".into(), "order".into(), order.join(",")]);
    }

    pub fn select_board(&mut self, idx: usize) {
        self.sel.board = idx.min(self.snap.boards.len());
        let name = self.board_name().map(String::from);
        if let Some(t) = &self.tx_poll {
            let _ = t.send(PollCmd::Board(name));
        }
        self.clamp_selection();
    }

    // ------------------------------------------------------------------ palette
    pub const COMMANDS: [&'static str; 32] = [
        "board",
        "running",
        "card",
        "decisions",
        "usage",
        "feed",
        "ops",
        "panels",
        "pause",
        "resume",
        "drain",
        "reload",
        "stop",
        "start",
        "slots",
        "pacer",
        "auto_worktree",
        "retry",
        "cancel",
        "kill",
        "defer",
        "note",
        "prio",
        "tier",
        "harness",
        "answer",
        "refresh",
        "fx",
        "bell",
        "ab",
        "help",
        "quit",
    ];

    pub fn palette_exec(&mut self, line: &str) {
        let line = line.trim();
        let (cmd, rest) = line.split_once(' ').map(|(a, b)| (a, b.trim())).unwrap_or((line, ""));
        if cmd.is_empty() {
            return;
        }
        if let Some(s) = Screen::from_name(cmd) {
            if s == Screen::Board && !rest.is_empty() {
                let idx = if rest == "all" { Some(0) } else { self.snap.boards.iter().position(|b| b.name == rest).map(|i| i + 1) };
                match idx {
                    Some(i) => self.select_board(i),
                    None => self.toast(format!("no board {rest}"), true),
                }
            }
            self.set_screen(s);
            return;
        }
        match cmd {
            "pause" | "resume" | "start" | "drain" | "reload" | "stop" => self.runner_verb(cmd),
            "slots" => match rest {
                "+" => self.slots_delta(1),
                "-" => self.slots_delta(-1),
                n if n.parse::<u32>().is_ok() => self.act(format!("slots {n}"), vec!["runner".into(), "slots".into(), n.into()]),
                _ => self.toast("usage: slots N | slots + | slots -", true),
            },
            "pacer" | "auto_worktree" => {
                let cur = if cmd == "pacer" { self.snap.runner.pacer } else { self.snap.runner.auto_worktree }.unwrap_or(true);
                match rest {
                    "" => self.set_config_bool(cmd, !cur),
                    "on" | "true" | "1" => self.set_config_bool(cmd, true),
                    "off" | "false" | "0" => self.set_config_bool(cmd, false),
                    _ => self.toast(format!("usage: {cmd} [on|off]"), true),
                }
            }
            "retry" => self.do_retry(),
            "cancel" => self.do_cancel(),
            "kill" => self.do_kill(),
            "defer" | "note" | "prio" | "tier" | "harness" | "answer" => {
                let which = match cmd {
                    "defer" => 'D',
                    "note" => 'n',
                    "prio" => 'P',
                    "tier" => 'T',
                    "harness" => 'H',
                    _ => 'a',
                };
                self.do_prompt(which);
                if !rest.is_empty() {
                    if let Mode::Prompt(mut p) = std::mem::replace(&mut self.mode, Mode::Normal) {
                        p.buf = rest.to_string();
                        self.submit_prompt(p);
                    }
                }
            }
            "refresh" => self.refresh(),
            "fx" => {
                let on = rest != "off";
                if on {
                    self.fx.enabled = true;
                } else {
                    self.fx.disable();
                }
                self.toast(format!("effects {}", if on { "on" } else { "off" }), false);
            }
            "bell" => {
                self.cfg.bell = rest != "off";
                self.toast(format!("bell {}", if self.cfg.bell { "on" } else { "off" }), false);
            }
            "ab" => {
                if rest.is_empty() {
                    self.toast("usage: ab VERB ARGS...", true)
                } else {
                    self.act(format!("ab {}", rest.chars().take(24).collect::<String>()), split_args(rest))
                }
            }
            "help" | "?" => self.mode = Mode::Help,
            "quit" | "q" => self.quit = true,
            _ => self.toast(format!("unknown command: {cmd} (try ?)"), true),
        }
    }

    // ------------------------------------------------------------------ keys
    pub fn on_key(&mut self, k: KeyEvent) {
        if k.modifiers.contains(KeyModifiers::CONTROL) && k.code == KeyCode::Char('c') {
            self.quit = true;
            return;
        }
        match self.mode.clone() {
            Mode::Help => {
                if matches!(k.code, KeyCode::Esc | KeyCode::Char('?') | KeyCode::Char('q') | KeyCode::Enter) {
                    self.mode = Mode::Normal;
                }
            }
            Mode::Confirm(c) => match k.code {
                KeyCode::Char('y') | KeyCode::Char('Y') | KeyCode::Enter => {
                    self.mode = Mode::Normal;
                    self.act(c.label, c.args);
                }
                _ => self.mode = Mode::Normal,
            },
            Mode::Prompt(mut p) => match k.code {
                KeyCode::Esc => self.mode = Mode::Normal,
                KeyCode::Enter => {
                    self.mode = Mode::Normal;
                    self.submit_prompt(p);
                }
                KeyCode::Backspace => {
                    p.buf.pop();
                    self.mode = Mode::Prompt(p);
                }
                KeyCode::Char(c) if !k.modifiers.contains(KeyModifiers::CONTROL) => {
                    p.buf.push(c);
                    self.mode = Mode::Prompt(p);
                }
                _ => {}
            },
            Mode::Filter | Mode::Palette => {
                let palette = matches!(self.mode, Mode::Palette);
                match k.code {
                    KeyCode::Esc => {
                        if !palette {
                            self.filter.clear();
                        }
                        self.input.clear();
                        self.mode = Mode::Normal;
                        self.clamp_selection();
                    }
                    KeyCode::Enter => {
                        let line = std::mem::take(&mut self.input);
                        self.mode = Mode::Normal;
                        if palette {
                            self.palette_exec(&line);
                        }
                    }
                    KeyCode::Tab if palette => {
                        if let Some(c) = Self::COMMANDS.iter().find(|c| c.starts_with(self.input.as_str()) && !self.input.contains(' ')) {
                            self.input = format!("{c} ");
                        }
                    }
                    KeyCode::Backspace => {
                        self.input.pop();
                        if !palette {
                            self.filter = self.input.clone();
                            self.clamp_selection();
                        }
                    }
                    KeyCode::Char(c) if !k.modifiers.contains(KeyModifiers::CONTROL) => {
                        self.input.push(c);
                        if !palette {
                            self.filter = self.input.clone();
                            self.clamp_selection();
                        }
                    }
                    _ => {}
                }
            }
            Mode::Normal => self.normal_key(k),
        }
    }

    fn normal_key(&mut self, k: KeyEvent) {
        let shift = k.modifiers.contains(KeyModifiers::SHIFT);
        match k.code {
            KeyCode::Char('q') => self.quit = true,
            KeyCode::Char('?') => self.mode = Mode::Help,
            KeyCode::Char(':') => {
                self.input.clear();
                self.mode = Mode::Palette;
            }
            KeyCode::Char('/') => {
                self.input = self.filter.clone();
                self.mode = Mode::Filter;
            }
            KeyCode::Esc => {
                self.filter.clear();
                self.clamp_selection();
            }
            KeyCode::Tab => self.set_screen(Screen::ALL[(self.screen.index() + 1) % 8]),
            KeyCode::BackTab => self.set_screen(Screen::ALL[(self.screen.index() + 7) % 8]),
            KeyCode::Char(c @ '1'..='8') => self.set_screen(Screen::ALL[c as usize - '1' as usize]),
            KeyCode::F(5) => self.refresh(),
            KeyCode::Char('b') if self.screen != Screen::Ops => {
                let n = self.snap.boards.len() + 1;
                self.select_board((self.sel.board + 1) % n);
            }
            KeyCode::Char('j') | KeyCode::Down => self.move_sel(1),
            KeyCode::Char('k') | KeyCode::Up if !shift => self.move_sel(-1),
            KeyCode::Char('g') | KeyCode::Home if self.screen != Screen::Ops => self.move_to(0),
            KeyCode::Char('G') | KeyCode::End => self.move_to(usize::MAX),
            KeyCode::PageDown => self.move_sel(10),
            KeyCode::PageUp => self.move_sel(-10),
            KeyCode::Char('h') | KeyCode::Left => self.move_lane(-1),
            KeyCode::Char('l') | KeyCode::Right => self.move_lane(1),
            _ => self.screen_key(k),
        }
        self.after_navigation();
    }

    fn screen_key(&mut self, k: KeyEvent) {
        let c = match k.code {
            KeyCode::Char(c) => c,
            KeyCode::Enter => '\n',
            _ => return,
        };
        if self.screen == Screen::Ops {
            match c {
                '\n' | ' ' => self.ops_enter(),
                'J' => self.tier_move(1),
                'K' => self.tier_move(-1),
                'p' => {
                    let verb = if self.snap.runner.paused { "resume" } else { "pause" };
                    self.runner_verb(verb)
                }
                'd' => self.runner_verb("drain"),
                'R' => self.runner_verb("reload"),
                'S' => self.runner_verb("stop"),
                'g' => self.runner_verb("start"),
                '+' | '=' => self.slots_delta(1),
                '-' => self.slots_delta(-1),
                'P' => self.set_config_bool("pacer", !self.snap.runner.pacer.unwrap_or(true)),
                'w' => self.set_config_bool("auto_worktree", !self.snap.runner.auto_worktree.unwrap_or(true)),
                'b' => {}
                _ => {}
            }
            return;
        }
        match c {
            '\n' => {
                if let Some(id) = self.selected_id() {
                    if self.screen == Screen::Card {
                        return;
                    }
                    self.open_card(id);
                }
            }
            'r' => self.do_retry(),
            'x' => self.do_cancel(),
            'K' => self.do_kill(),
            'n' | 'P' | 'T' | 'H' | 'D' | 'a' => self.do_prompt(c),
            'e' => self.do_suspend(true),
            's' => self.do_suspend(false),
            'm' if self.screen == Screen::Decisions => {
                self.show_all_manual = !self.show_all_manual;
                self.clamp_selection();
            }
            ':' => {}
            _ => {}
        }
    }

    fn move_lane(&mut self, d: i64) {
        match self.screen {
            Screen::Board => self.sel.lane = (self.sel.lane as i64 + d).clamp(0, 5) as usize,
            Screen::Ops => self.sel.ops = (self.sel.ops as i64 + d).clamp(0, 5) as usize,
            _ => {}
        }
    }

    pub fn move_sel(&mut self, d: i64) {
        let step = |cur: usize, len: usize| -> usize { (cur as i64 + d).clamp(0, len.saturating_sub(1) as i64) as usize };
        match self.screen {
            Screen::Board => {
                let lens: Vec<usize> = self.lanes().iter().map(Vec::len).collect();
                let l = self.sel.lane;
                self.sel.lane_idx[l] = step(self.sel.lane_idx[l], lens[l]);
            }
            Screen::Running => self.sel.run = step(self.sel.run, self.snap.runner.children.len()),
            Screen::Decisions => self.sel.dec = step(self.sel.dec, self.decisions().len()),
            Screen::Feed => self.sel.feed = step(self.sel.feed, self.feed.len()),
            Screen::Ops => {
                let lens = self.ops_lens();
                let s = self.sel.ops;
                self.sel.ops_idx[s] = step(self.sel.ops_idx[s], lens[s]);
            }
            Screen::Card => self.sel.card_scroll = (self.sel.card_scroll as i64 + d).max(0) as u16,
            Screen::Usage => self.sel.usage_scroll = (self.sel.usage_scroll as i64 + d).max(0) as u16,
            Screen::Panels => {}
        }
    }

    fn move_to(&mut self, pos: usize) {
        let d = if pos == 0 { -100000 } else { 100000 };
        self.move_sel(d);
    }

    pub fn on_mouse(&mut self, m: MouseEvent) {
        match m.kind {
            MouseEventKind::ScrollDown => self.move_sel(1),
            MouseEventKind::ScrollUp => self.move_sel(-1),
            MouseEventKind::Down(MouseButton::Left) => {
                let hit = self
                    .hits
                    .iter()
                    .rev()
                    .find(|(r, _)| m.column >= r.x && m.column < r.x + r.width && m.row >= r.y && m.row < r.y + r.height)
                    .map(|(_, h)| h.clone());
                match hit {
                    Some(Hit::Tab(s)) => self.set_screen(s),
                    Some(Hit::Lane(l, i)) => {
                        self.sel.lane = l;
                        self.sel.lane_idx[l] = i;
                    }
                    Some(Hit::Run(i)) => self.sel.run = i,
                    Some(Hit::Decision(i)) => self.sel.dec = i,
                    Some(Hit::Feed(i)) => self.sel.feed = i,
                    None => {}
                }
                self.after_navigation();
            }
            _ => {}
        }
    }

    // ------------------------------------------------------------------ drawing with effects
    /// Widgets first, then effects on top of the finished buffer (that is how tachyonfx works).
    pub fn draw(&mut self, f: &mut Frame, dt: Duration) {
        crate::ui::render(self, f);
        if !self.fx.enabled {
            self.pending.clear();
            return;
        }
        let full = f.area();
        if self.startup_pending {
            self.startup_pending = false;
            self.fx.startup(full);
        }
        if self.transition_pending {
            self.transition_pending = false;
            self.fx.transition(self.content);
        }
        if self.feed_fresh > 0 {
            let n = self.feed_fresh as u16;
            self.feed_fresh = 0;
            if self.screen == Screen::Feed && self.feed_area.height > 0 {
                let a = Rect { height: n.min(self.feed_area.height), ..self.feed_area };
                self.fx.feed_in(a);
            }
        }
        let mut keep = vec![];
        for (kind, id, at) in std::mem::take(&mut self.pending) {
            match self.rows.get(&id) {
                Some(r) => match kind {
                    PendingKind::Done => self.fx.flash_done(&id, *r),
                    PendingKind::Dead => self.fx.glitch_dead(&id, *r),
                },
                None if at.elapsed() < Duration::from_secs(3) => keep.push((kind, id, at)),
                None => {}
            }
        }
        self.pending = keep;
        let tab = self.hits.iter().find(|(_, h)| matches!(h, Hit::Tab(Screen::Decisions))).map(|(r, _)| *r);
        match tab {
            Some(r) if !self.decisions().is_empty() && self.screen != Screen::Decisions => self.fx.pulse(r),
            _ => self.fx.stop_pulse(),
        }
        self.fx.process(dt, f.buffer_mut(), full);
    }

    /// Average draw time over the last frames while effects ran: used to switch effects off on slow terminals.
    pub fn note_frame(&mut self, ms: f64) {
        if self.fx.running() {
            self.frame_ms.push_back(ms);
            if self.frame_ms.len() > 24 {
                self.frame_ms.pop_front();
            }
            if self.frame_ms.len() >= 24 && self.frame_ms.iter().sum::<f64>() / 24.0 > 45.0 {
                self.fx.disable();
                self.frame_ms.clear();
                self.toast("terminal is slow: effects switched off (:fx on to retry)", true);
            }
        }
    }
}

/// Split a command line into words, honouring simple "double" and 'single' quotes.
pub fn split_args(s: &str) -> Vec<String> {
    let mut out = vec![];
    let mut cur = String::new();
    let mut q: Option<char> = None;
    let mut any = false;
    for c in s.chars() {
        match (q, c) {
            (Some(x), c) if c == x => q = None,
            (Some(_), c) => cur.push(c),
            (None, '"') | (None, '\'') => {
                q = Some(c);
                any = true;
            }
            (None, c) if c.is_whitespace() => {
                if !cur.is_empty() || any {
                    out.push(std::mem::take(&mut cur));
                    any = false;
                }
            }
            (None, c) => cur.push(c),
        }
    }
    if !cur.is_empty() || any {
        out.push(cur);
    }
    out
}
