use ab_tui::ab::{self, Ab};
use ab_tui::app::{App, Suspend};
use ab_tui::config::Config;
use ab_tui::workers::{self, Msg, PollCmd};
use ratatui::crossterm::event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyEventKind};
use ratatui::crossterm::execute;
use ratatui::DefaultTerminal;
use std::io::Write;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::time::{Duration, Instant};

static STOP: AtomicBool = AtomicBool::new(false);

/// True once the controlling terminal is gone (pty closed, ssh/tmux dropped). Without this check the event
/// loop spun a core forever: a hung-up tty keeps `poll` readable, so the inner read loop never returned to
/// the STOP check.
fn tty_gone() -> bool {
    // `isatty` still says yes on a hung-up pty, but poll(2) reports POLLHUP/POLLERR on it even with no events asked for.
    let mut fd = libc::pollfd { fd: libc::STDIN_FILENO, events: 0, revents: 0 };
    let r = unsafe { libc::poll(&mut fd, 1, 0) };
    r < 0 || fd.revents & (libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0 || unsafe { libc::isatty(libc::STDIN_FILENO) == 0 }
}

extern "C" fn on_signal(_: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

const HELP: &str = "ab-tui: live dashboard and control panel for agentboard

usage: ab-tui [--board-home DIR] [--config FILE] [--no-fx] [--as ACTOR] [--owner NAME] [--poll-ms N]

  --board-home DIR  use this AB_HOME (default: $AB_HOME or ~/.agentboard)
  --config FILE     settings file (default: <board home>/tui.toml)
  --no-fx           turn the tachyonfx effects off
  --as ACTOR        run the `ab` verbs as this actor
  --owner NAME      show decision cards owned by NAME (default: the actor)
  --poll-ms N       snapshot interval in milliseconds (default 1000)

Press ? inside for the keys. Everything is read from `ab snapshot --json` and every action is an `ab` verb.";

struct Args {
    home: Option<PathBuf>,
    config: Option<PathBuf>,
    no_fx: bool,
    actor: Option<String>,
    owner: Option<String>,
    poll_ms: Option<u64>,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args { home: None, config: None, no_fx: false, actor: None, owner: None, poll_ms: None };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut val = |name: &str| it.next().ok_or_else(|| format!("{name} needs a value"));
        match arg.as_str() {
            "--board-home" | "--home" => a.home = Some(PathBuf::from(val(&arg)?)),
            "--config" => a.config = Some(PathBuf::from(val(&arg)?)),
            "--no-fx" => a.no_fx = true,
            "--as" => a.actor = Some(val(&arg)?),
            "--owner" => a.owner = Some(val(&arg)?),
            "--poll-ms" => a.poll_ms = Some(val(&arg)?.parse().map_err(|_| "--poll-ms needs a number".to_string())?),
            "-h" | "--help" => {
                println!("{HELP}");
                std::process::exit(0);
            }
            "-V" | "--version" => {
                println!("ab-tui {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument {other} (try --help)")),
        }
    }
    Ok(a)
}

fn home_dir(args: &Args) -> PathBuf {
    args.home.clone().or_else(|| std::env::var_os("AB_HOME").map(PathBuf::from)).unwrap_or_else(|| {
        let h = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
        h.join(".agentboard")
    })
}

fn enter() -> DefaultTerminal {
    let t = ratatui::init();
    let _ = execute!(std::io::stdout(), EnableMouseCapture);
    t
}

fn leave() {
    let _ = execute!(std::io::stdout(), DisableMouseCapture);
    ratatui::restore();
}

fn notify(app: &App, text: &str) {
    let mut out = std::io::stdout();
    let _ = out.write_all(b"\x07");
    let _ = out.flush();
    let cmd = if let Some(c) = &app.cfg.notify_command {
        Some(Command::new("sh").arg("-c").arg(c).env("AB_TUI_MSG", text).spawn())
    } else if ab::which("notify-send") {
        Some(Command::new("notify-send").arg("agentboard").arg(text).spawn())
    } else {
        None
    };
    if let Some(Ok(mut child)) = cmd {
        std::thread::spawn(move || {
            let _ = child.wait();
        });
    }
}

fn run_suspended(terminal: &mut DefaultTerminal, what: Suspend) -> DefaultTerminal {
    let _ = terminal;
    leave();
    let (cmd, dir) = match what {
        Suspend::Shell(d) => {
            println!("ab-tui: shell in {d}, exit to come back");
            (std::env::var("SHELL").unwrap_or_else(|_| "sh".into()), (d, None))
        }
        Suspend::Editor(d) => (std::env::var("VISUAL").or_else(|_| std::env::var("EDITOR")).unwrap_or_else(|_| "vi".into()), (d, Some("."))),
    };
    let mut c = Command::new("sh");
    c.arg("-c").arg(format!("{cmd} {}", dir.1.unwrap_or(""))).current_dir(&dir.0).env("AB_TUI", "1");
    let _ = c.status();
    let mut t = enter();
    let _ = t.clear();
    t
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("ab-tui: {e}");
            std::process::exit(2);
        }
    };
    let home = home_dir(&args);
    let cfg_path = args.config.clone().unwrap_or_else(|| home.join("tui.toml"));
    let mut cfg = match Config::load(&cfg_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("ab-tui: {e}");
            std::process::exit(2);
        }
    };
    if let Some(o) = &args.owner {
        cfg.owner = Some(o.clone());
    }
    if let Some(p) = args.poll_ms {
        cfg.poll_ms = p.max(200);
    }
    let abc = Ab::new(Some(home.clone()), args.actor.clone());
    if !abc.bin.contains('/') && !ab::which(&abc.bin) {
        eprintln!("ab-tui: the `ab` command is not on PATH (set AB_BIN to its path)");
        std::process::exit(2);
    }

    unsafe {
        libc::signal(libc::SIGTERM, on_signal as *const () as libc::sighandler_t);
        libc::signal(libc::SIGHUP, on_signal as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, on_signal as *const () as libc::sighandler_t);
    }
    // a panic must not leave the terminal in raw mode on the alternate screen
    let hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = execute!(std::io::stdout(), DisableMouseCapture);
        ratatui::restore();
        hook(info);
    }));

    let (tx, rx) = mpsc::channel::<Msg>();
    let (ptx, prx) = mpsc::channel::<PollCmd>();
    workers::spawn_poller(abc.clone(), cfg.clone(), tx.clone(), prx);
    workers::spawn_sys(cfg.clone(), tx.clone());
    let mut app = App::new(cfg, abc, !args.no_fx);
    app.tx_poll = Some(ptx.clone());
    app.tx_msg = Some(tx);

    // A hangup that lands while the main thread is inside crossterm's poll() leaves it busy-reading EOF
    // forever (it ignores its own timeout), so the loop checks above never run: a watchdog ends the process.
    std::thread::spawn(|| loop {
        std::thread::sleep(Duration::from_secs(1));
        if tty_gone() {
            std::process::exit(0);
        }
    });
    let mut terminal = enter();
    let mut last = Instant::now();
    let result: std::io::Result<()> = (|| loop {
        if STOP.load(Ordering::SeqCst) || app.quit || tty_gone() {
            return Ok(());
        }
        while let Ok(m) = rx.try_recv() {
            app.on_msg(m);
        }
        let dt = last.elapsed();
        last = Instant::now();
        let t0 = Instant::now();
        terminal.draw(|f| app.draw(f, dt))?;
        app.note_frame(t0.elapsed().as_secs_f64() * 1000.0);
        if let Some(text) = app.bell.take() {
            notify(&app, &text);
        }
        if let Some(s) = app.suspend.take() {
            terminal = run_suspended(&mut terminal, s);
            app.refresh();
            continue;
        }
        let wait = if app.fx.running() { Duration::from_millis(33) } else { Duration::from_millis(100) };
        if tty_gone() {
            return Ok(());
        }
        if event::poll(wait)? {
            loop {
                // crossterm's read() spins forever on EOF, so never call it on a dead tty
                if tty_gone() {
                    return Ok(());
                }
                match event::read()? {
                    Event::Key(k) if k.kind != KeyEventKind::Release => app.on_key(k),
                    Event::Mouse(m) => app.on_mouse(m),
                    _ => {}
                }
                if !event::poll(Duration::ZERO)? || STOP.load(Ordering::SeqCst) || tty_gone() {
                    break;
                }
            }
        }
        app.tick();
    })();
    let _ = ptx.send(PollCmd::Stop);
    leave();
    if let Err(e) = result {
        eprintln!("ab-tui: {e}");
        std::process::exit(1);
    }
}
