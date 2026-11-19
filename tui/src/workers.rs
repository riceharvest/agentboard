//! Background threads: the snapshot poller, the system/GPU/panel readers and one-shot action runners.
//! The UI thread never waits on a subprocess; everything comes back as a `Msg`.
use crate::ab::{self, Ab};
use crate::config::Config;
use crate::model::{Detail, Snapshot};
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

pub enum Msg {
    Snapshot(Box<Snapshot>, Duration),
    SnapshotErr(String),
    Detail(String, Result<Box<Detail>, String>),
    Action(String, Result<String, String>),
    Ram(String),
    Gpu(String),
    Panel(usize, String),
}

pub enum PollCmd {
    Refresh,
    /// Follow this board only (None = every board).
    Board(Option<String>),
    /// Include the live tail of this run in the snapshot.
    Tail(Option<i64>),
    Stop,
}

pub fn spawn_poller(ab: Ab, cfg: Config, tx: Sender<Msg>, rx: Receiver<PollCmd>) {
    std::thread::spawn(move || {
        let mut board: Option<String> = None;
        let mut tail: Option<i64> = None;
        let mut cursor: Option<i64> = None;
        loop {
            let started = Instant::now();
            let mut args: Vec<String> = vec![];
            if let Some(b) = &board {
                args.extend(["-B".into(), b.clone()]);
            }
            args.extend(["snapshot".into(), "--json".into(), "--closed".into(), cfg.closed.to_string()]);
            // first call seeds the feed with the latest events, later calls only ask for what is new
            args.extend(["--events-after".into(), cursor.unwrap_or(0).to_string(), "--events-limit".into(), "200".into()]);
            if let Some(r) = tail {
                args.extend(["--tail".into(), format!("{r}:{}", cfg.tail_bytes)]);
            }
            match ab.run(&args, Duration::from_secs(20)) {
                Ok(text) => match serde_json::from_str::<Snapshot>(&text) {
                    Ok(s) => {
                        cursor = Some(s.events_cursor);
                        if tx.send(Msg::Snapshot(Box::new(s), started.elapsed())).is_err() {
                            return;
                        }
                    }
                    Err(e) => {
                        let _ = tx.send(Msg::SnapshotErr(format!("bad snapshot json: {e}")));
                    }
                },
                Err(e) => {
                    let _ = tx.send(Msg::SnapshotErr(e));
                }
            }
            // wait for the next tick, or a command that wakes us early
            let wait = Duration::from_millis(cfg.poll_ms).saturating_sub(started.elapsed()).max(Duration::from_millis(50));
            match rx.recv_timeout(wait) {
                Ok(PollCmd::Refresh) | Err(RecvTimeoutError::Timeout) => {}
                Ok(PollCmd::Board(b)) => {
                    board = b;
                    cursor = None;
                }
                Ok(PollCmd::Tail(t)) => tail = t,
                Ok(PollCmd::Stop) | Err(RecvTimeoutError::Disconnected) => return,
            }
        }
    });
}

/// `ab show ID --json` for the card screen.
pub fn fetch_detail(ab: Ab, id: String, board: Option<String>, tx: Sender<Msg>) {
    std::thread::spawn(move || {
        let mut args: Vec<String> = vec![];
        if let Some(b) = board {
            args.extend(["-B".into(), b]);
        }
        args.extend(["show".into(), id.clone(), "--json".into()]);
        let r =
            ab.run(&args, Duration::from_secs(15)).and_then(|t| serde_json::from_str::<Detail>(&t).map(Box::new).map_err(|e| format!("bad show json: {e}")));
        let _ = tx.send(Msg::Detail(id, r));
    });
}

/// Run one `ab` verb; the result lands in the toast line.
pub fn run_action(ab: Ab, label: String, args: Vec<String>, tx: Sender<Msg>) {
    std::thread::spawn(move || {
        let r = ab.run(&args, Duration::from_secs(60)).map(|s| s.lines().next().unwrap_or("").trim().to_string());
        let _ = tx.send(Msg::Action(label, r));
    });
}

fn meminfo() -> String {
    let text = std::fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let kb = |key: &str| text.lines().find(|l| l.starts_with(key)).and_then(|l| l.split_whitespace().nth(1)).and_then(|v| v.parse::<f64>().ok());
    match (kb("MemAvailable:"), kb("MemTotal:")) {
        (Some(a), Some(t)) => format!("{:.1} of {:.1} GiB available", a / 1048576.0, t / 1048576.0),
        _ => "n/a".into(),
    }
}

/// Free-RAM text: the configured command, else `agent-resource-gate status` if installed, else /proc/meminfo.
fn ram_text(cmd: &Option<String>, gate: bool) -> String {
    let run = |c: &str| ab::shell(c, Duration::from_secs(4)).ok();
    let text = match (cmd, gate) {
        (Some(c), _) => run(c),
        (None, true) => run("agent-resource-gate status"),
        _ => None,
    };
    match text {
        Some(t) if !t.trim().is_empty() => {
            // keep the lines that carry the numbers: "free RAM ..." and "room for heavy work ..."
            let mut keep: Vec<&str> = t.lines().filter(|l| l.starts_with("free RAM") || l.starts_with("room for") || l.starts_with("must stay")).collect();
            if keep.is_empty() {
                keep = t.lines().take(2).collect();
            }
            keep.iter().map(|l| l.split_whitespace().collect::<Vec<_>>().join(" ")).collect::<Vec<_>>().join(" · ")
        }
        _ => meminfo(),
    }
}

pub fn spawn_sys(cfg: Config, tx: Sender<Msg>) {
    let gate = ab::which("agent-resource-gate");
    let (c, t) = (cfg.clone(), tx.clone());
    std::thread::spawn(move || loop {
        if t.send(Msg::Ram(ram_text(&c.ram_command, gate))).is_err() {
            return;
        }
        std::thread::sleep(Duration::from_secs(5));
    });
    let default_gpu = cfg.gpu_command.is_none();
    let gpu = cfg.gpu_command.clone().or_else(|| {
        ab::which("nvidia-smi").then(|| "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits".to_string())
    });
    if let Some(cmd) = gpu {
        let (t, every) = (tx.clone(), cfg.gpu_refresh.max(1));
        std::thread::spawn(move || loop {
            let line = ab::shell(&cmd, Duration::from_secs(5))
                .map(|s| s.lines().take(2).map(|l| if default_gpu { fmt_nvidia(l) } else { l.to_string() }).collect::<Vec<_>>().join(" | "))
                .unwrap_or_else(|e| format!("gpu: {e}"));
            if t.send(Msg::Gpu(line)).is_err() {
                return;
            }
            std::thread::sleep(Duration::from_secs(every));
        });
    }
    for (i, p) in cfg.panel.iter().enumerate() {
        if p.command.trim().is_empty() {
            continue;
        }
        let (t, cmd, every) = (tx.clone(), p.command.clone(), p.refresh.max(1));
        std::thread::spawn(move || loop {
            let text = ab::shell(&cmd, Duration::from_secs(15)).unwrap_or_else(|e| format!("error: {e}"));
            if t.send(Msg::Panel(i, text)).is_err() {
                return;
            }
            std::thread::sleep(Duration::from_secs(every));
        });
    }
}

/// "NVIDIA GeForce RTX 3060, 2, 1153, 12288" (name, util %, used MiB, total MiB) -> "NVIDIA GeForce RTX 3060 2% 1.1/12.0 GiB".
pub fn fmt_nvidia(line: &str) -> String {
    let f: Vec<&str> = line.split(',').map(str::trim).collect();
    if let [name, util, used, total] = f[..] {
        if let (Ok(u), Ok(m), Ok(t)) = (util.parse::<f64>(), used.parse::<f64>(), total.parse::<f64>()) {
            return format!("{name} {u:.0}% {:.1}/{:.1} GiB", m / 1024.0, t / 1024.0);
        }
    }
    line.to_string()
}
