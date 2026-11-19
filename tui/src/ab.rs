//! Every read and every action goes through the `ab` CLI. The child environment is sanitised: a TUI started from
//! inside an agent session must not inherit AB_TASK / AB_RUN / AB_BOARD, and AB_HOME is pinned to --board-home.
use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Ab {
    pub bin: String,
    pub home: Option<PathBuf>,
    pub actor: Option<String>,
}

impl Ab {
    pub fn new(home: Option<PathBuf>, actor: Option<String>) -> Ab {
        Ab { bin: std::env::var("AB_BIN").unwrap_or_else(|_| "ab".into()), home, actor }
    }

    pub fn command(&self, args: &[String]) -> Command {
        let mut c = Command::new(&self.bin);
        for k in ["AB_TASK", "AB_RUN", "AB_BOARD", "AB_JSON"] {
            c.env_remove(k);
        }
        if let Some(h) = &self.home {
            c.env("AB_HOME", h);
        }
        if let Some(a) = &self.actor {
            c.env("AB_ACTOR", a);
        }
        c.args(args).stdin(Stdio::null());
        c
    }

    /// Run `ab ARGS`, return stdout, or the first useful line of stderr on failure. Hard timeout.
    pub fn run(&self, args: &[String], timeout: Duration) -> Result<String, String> {
        run_command(self.command(args), timeout)
    }
}

pub fn run_command(mut c: Command, timeout: Duration) -> Result<String, String> {
    c.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = c.spawn().map_err(|e| format!("cannot start {:?}: {e}", c.get_program()))?;
    let pid = child.id();
    let mut so = child.stdout.take();
    let mut se = child.stderr.take();
    // drain both pipes on threads so a big snapshot never blocks the child
    let (tx, rx) = mpsc::channel();
    let t1 = tx.clone();
    std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(o) = so.as_mut() {
            let mut b = Vec::new();
            let _ = o.read_to_end(&mut b);
            s = String::from_utf8_lossy(&b).into_owned();
        }
        let _ = t1.send((0, s));
    });
    std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(o) = se.as_mut() {
            let mut b = Vec::new();
            let _ = o.read_to_end(&mut b);
            s = String::from_utf8_lossy(&b).into_owned();
        }
        let _ = tx.send((1, s));
    });
    let (mut out, mut err) = (None, None);
    let deadline = std::time::Instant::now() + timeout;
    let mut status = None;
    while status.is_none() || out.is_none() || err.is_none() {
        let left = deadline.saturating_duration_since(std::time::Instant::now());
        if left.is_zero() {
            unsafe { libc::kill(pid as i32, libc::SIGKILL) };
            let _ = child.wait();
            return Err(format!("timed out after {}s", timeout.as_secs()));
        }
        if status.is_none() {
            status = child.try_wait().map_err(|e| e.to_string())?;
        }
        match rx.recv_timeout(left.min(Duration::from_millis(20))) {
            Ok((0, s)) => out = Some(s),
            Ok((_, s)) => err = Some(s),
            Err(_) => {}
        }
    }
    let (out, err) = (out.unwrap_or_default(), err.unwrap_or_default());
    match status {
        Some(s) if s.success() => Ok(out),
        _ => {
            let line = err.lines().rev().find(|l| !l.trim().is_empty()).or_else(|| out.lines().next()).unwrap_or("failed");
            Err(line.trim().to_string())
        }
    }
}

/// Run a shell command line (panels, gpu, ram) with a timeout; stdout, trimmed.
pub fn shell(cmd: &str, timeout: Duration) -> Result<String, String> {
    let mut c = Command::new("sh");
    c.arg("-c").arg(cmd).stdin(Stdio::null());
    run_command(c, timeout).map(|s| s.trim_end().to_string())
}

pub fn which(name: &str) -> bool {
    std::env::var_os("PATH").is_some_and(|p| std::env::split_paths(&p).any(|d| d.join(name).is_file()))
}
