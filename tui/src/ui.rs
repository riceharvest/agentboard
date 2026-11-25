//! Rendering. Pure functions of `App`; the only mutation is recording where things were drawn (for effects and the mouse).
use crate::app::{App, Hit, Mode, Screen, LANES, OPS_SECTIONS};
use crate::model::{value_id, Task};
use crate::theme::{self, base, bold, dim, fg};
use crate::util::{bar, cut_cols, fmt_clock, fmt_dur, fmt_hms, fmt_num, gauge_color, md_lines, pad, text_width, trunc, wrap};
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Sparkline, Wrap};
use ratatui::Frame;

/// Left-to-right layouts (the perpendicular of the default top-to-bottom direction).
const ACROSS: Direction = Direction::Vertical.perpendicular();
use serde_json::Value;

pub fn render(app: &mut App, f: &mut Frame) {
    let area = f.area();
    app.rows.clear();
    app.hits.clear();
    f.render_widget(Block::default().style(base()), area);
    if area.width < 72 || area.height < 16 {
        let msg = format!("terminal too small ({}x{}): agentboard needs at least 72x16", area.width, area.height);
        f.render_widget(Paragraph::new(msg).style(fg(theme::WARN)), area);
        return;
    }
    let hh = if area.height >= 26 { 6 } else { 5 };
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(hh), Constraint::Length(1), Constraint::Min(5), Constraint::Length(1)])
        .split(area);
    header(app, f, rows[0]);
    tabs(app, f, rows[1]);
    app.content = rows[2];
    match app.screen {
        Screen::Board => board(app, f, rows[2]),
        Screen::Running => running(app, f, rows[2]),
        Screen::Card => card(app, f, rows[2]),
        Screen::Decisions => decisions(app, f, rows[2]),
        Screen::Usage => usage(app, f, rows[2]),
        Screen::Feed => feed(app, f, rows[2]),
        Screen::Ops => ops(app, f, rows[2]),
        Screen::Panels => panels(app, f, rows[2]),
    }
    footer(app, f, rows[3]);
    overlays(app, f, area);
}

fn block(title: &str, focused: bool) -> Block<'static> {
    let color = if focused { theme::ACCENT } else { Color::Rgb(70, 80, 100) };
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(fg(color))
        .title(Span::styled(format!(" {title} "), if focused { fg(theme::ACCENT).add_modifier(Modifier::BOLD) } else { dim() }))
        .style(base())
}

fn sp(s: impl Into<String>, st: Style) -> Span<'static> {
    Span::styled(s.into(), st)
}

/// "2026-11-19T10:03:41+00:00" -> epoch seconds (UTC offset honoured).
pub fn parse_iso(s: &str) -> Option<f64> {
    let (date, rest) = s.split_once('T')?;
    let mut d = date.split('-').map(|x| x.parse::<i64>().ok());
    let (y, m, dd) = (d.next()??, d.next()??, d.next()??);
    let time: String = rest.chars().take(8).collect();
    let mut t = time.split(':').map(|x| x.parse::<i64>().ok());
    let (hh, mm, ss) = (t.next()??, t.next()??, t.next()??);
    let (y2, m2) = if m <= 2 { (y - 1, m + 9) } else { (y, m - 3) };
    let era = y2.div_euclid(400);
    let yoe = y2 - era * 400;
    let doy = (153 * m2 + 2) / 5 + dd - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    let mut off = 0i64;
    if let Some(pos) = rest.rfind(['+', '-']) {
        if pos >= 8 {
            let o = &rest[pos + 1..];
            let (oh, om) = o.split_once(':').unwrap_or((o, "0"));
            off = (oh.parse::<i64>().unwrap_or(0) * 3600 + om.parse::<i64>().unwrap_or(0) * 60) * if &rest[pos..pos + 1] == "-" { -1 } else { 1 };
        }
    }
    Some((days * 86400 + hh * 3600 + mm * 60 + ss - off) as f64)
}

// ---------------------------------------------------------------------------------------------- header
fn header(app: &App, f: &mut Frame, area: Rect) {
    let snap = &app.snap;
    let r = &snap.runner;
    let age = app.age();
    let mut lines: Vec<Line> = vec![];

    // runner + slots
    let (dot, state, col) = if !app.have_snap {
        ("○", "connecting".to_string(), theme::DIM)
    } else if !r.running {
        ("●", "stopped".to_string(), theme::BAD)
    } else if r.paused {
        ("●", "paused".to_string(), theme::WARN)
    } else if r.draining {
        ("●", "draining".to_string(), theme::WARN)
    } else {
        ("●", r.state.clone().unwrap_or_else(|| "running".into()), theme::GOOD)
    };
    let mut l1 = vec![sp(format!(" {dot} "), fg(col)), sp("runner ", dim()), sp(state, fg(col).add_modifier(Modifier::BOLD))];
    if let Some(pid) = r.pid.filter(|_| r.running) {
        l1.push(sp(format!("  pid {pid}"), dim()));
    }
    if let Some(s) = r.started_at.filter(|_| r.running) {
        l1.push(sp(format!("  up {}", fmt_dur(app.now() - s)), dim()));
    }
    if r.running && r.heartbeat_age_s.is_some_and(|h| h + age > 60.0) {
        l1.push(sp(format!("  heartbeat {} ago", fmt_dur(r.heartbeat_age_s.unwrap_or(0.0) + age)), fg(theme::WARN)));
    }
    if !r.stop.is_empty() {
        l1.push(sp(format!("  {} requested", r.stop), fg(theme::WARN)));
    }
    let total = r.slots_effective.max(1);
    l1.push(sp("   slots ", dim()));
    l1.extend(bar(10, r.active as f64 / total as f64, None, if r.active >= total { theme::WARN } else { theme::ACCENT }));
    l1.push(sp(format!(" {}/{}", r.active, r.slots_effective), bold()));
    if r.windowed || !snap.active_windows.is_empty() {
        l1.push(sp(format!("  window {}", snap.active_windows.join(",")), fg(theme::ACCENT)));
    } else {
        l1.push(sp("  window none", dim()));
    }
    l1.push(sp(format!("   ready {}", r.ready), if r.ready > 0 { base() } else { dim() }));
    lines.push(Line::from(l1));

    // provider plan gauges
    let mut l2 = vec![sp(" claude ", dim())];
    match &snap.plan.claude_plan_pct {
        Some(p) => {
            let stale = if p.stale { " (stale)" } else { "" };
            l2.push(sp("5h ", dim()));
            l2.extend(bar(14, p.five_hour / 100.0, None, gauge_color(p.five_hour / 100.0)));
            l2.push(sp(format!(" {:.0}%", p.five_hour), bold()));
            if let Some(reset) = p.five_hour_resets.as_deref().and_then(parse_iso) {
                l2.push(sp(format!(" resets in {}", fmt_dur(reset - app.now())), dim()));
            }
            l2.push(sp("   7d ", dim()));
            let pace = snap.plan.weekly_pace_pct;
            let over = pace.is_some_and(|x| p.seven_day > x);
            l2.extend(bar(14, p.seven_day / 100.0, pace.map(|x| x / 100.0), if over { theme::WARN } else { gauge_color(p.seven_day / 100.0) }));
            l2.push(sp(format!(" {:.0}%", p.seven_day), bold()));
            if let Some(x) = pace {
                l2.push(sp(format!(" pace {x:.0}%"), if over { fg(theme::WARN) } else { dim() }));
            }
            l2.push(sp(stale, fg(theme::WARN)));
        }
        None => l2.push(sp("no plan reading yet", dim())),
    }
    if let Some(g) = snap.plan.guards.first() {
        let used: usize = l2.iter().map(|s| s.content.chars().count()).sum();
        let room = (area.width as usize).saturating_sub(used + 6);
        l2.push(sp(format!("  ⚠ {}", trunc(&format!("{}: {}", g.bucket, g.reason), room)), fg(theme::WARN)));
    }
    lines.push(Line::from(l2));

    // cooldowns
    let mut l3 = vec![sp(" cooldowns ", dim())];
    if snap.limits.is_empty() {
        l3.push(sp("none", dim()));
    }
    for (i, l) in snap.limits.iter().enumerate() {
        if i > 0 {
            l3.push(sp("  ·  ", dim()));
        }
        l3.push(sp(format!("{} {} ", l.scope, l.kind.clone().unwrap_or_default()), fg(theme::WARN)));
        l3.push(sp(fmt_clock(l.remaining_s - age), bold()));
    }
    lines.push(Line::from(l3));

    // machine
    let ram = if app.ram.is_empty() { format!("{:.1} GiB free (stop below {:.1})", r.free_gib, r.min_free_gib) } else { app.ram.clone() };
    let mut l4 = vec![if ram.starts_with("free RAM") { sp(" ", dim()) } else { sp(" ram ", dim()) }, sp(ram, base())];
    if !app.gpu.is_empty() {
        l4.push(sp("   gpu ", dim()));
        l4.push(sp(app.gpu.clone(), base()));
    }
    lines.push(Line::from(l4));

    let title = format!("agentboard · {} · poll {} ms", if snap.actor.is_empty() { "…" } else { &snap.actor }, app.took.as_millis());
    let inner_h = area.height.saturating_sub(2) as usize;
    lines.truncate(inner_h);
    f.render_widget(Paragraph::new(lines).style(base()).block(block(&title, false)), area);
}

// ---------------------------------------------------------------------------------------------- tabs / footer
fn tabs(app: &mut App, f: &mut Frame, area: Rect) {
    let mut spans = vec![];
    let mut x = area.x;
    let dec = app.decisions().len();
    for (i, s) in Screen::ALL.iter().enumerate() {
        let badge = match s {
            Screen::Running if app.running_children() > 0 => format!(" ({})", app.running_children()),
            Screen::Decisions if dec > 0 => format!(" ({dec})"),
            _ => String::new(),
        };
        let label = format!(" {} {}{} ", i + 1, s.title(), badge);
        let w = label.chars().count() as u16;
        let style = if *s == app.screen {
            sel()
        } else if *s == Screen::Decisions && dec > 0 {
            fg(theme::WARN).add_modifier(Modifier::BOLD)
        } else {
            dim()
        };
        app.hits.push((Rect { x, y: area.y, width: w.min(area.right().saturating_sub(x)), height: 1 }, Hit::Tab(*s)));
        spans.push(sp(label, style));
        x += w;
    }
    let mut right = String::new();
    right.push_str(&format!("  board: {}", app.board_name().unwrap_or("all")));
    if !app.filter.is_empty() {
        right.push_str(&format!("  filter: {}", app.filter));
    }
    spans.push(sp(right, fg(theme::ACCENT)));
    f.render_widget(Paragraph::new(Line::from(spans)).style(base()), area);
}

fn sel() -> Style {
    theme::sel()
}

fn hints(app: &App) -> &'static str {
    match app.screen {
        Screen::Board => {
            "↑↓ ←→ move · Enter open · b board · r retry · x cancel · n note · P prio · T tier · D defer · e/s editor/shell · / filter · : palette · ? help"
        }
        Screen::Running => "↑↓ select run · Enter open card · K kill run · x cancel card · n note · e/s editor/shell · ? help",
        Screen::Card => "↑↓ scroll · r retry · x cancel · K kill · n note · P prio · T tier · H harness · D defer · a answer · e/s editor/shell",
        Screen::Decisions => "↑↓ select · a answer · Enter open · n note · m all manual cards · drag/y copy · ? help",
        Screen::Usage => "↑↓ scroll · b board · ? help",
        Screen::Feed => "↑↓ select · Enter open card · / filter · ? help",
        Screen::Ops => "←→ section · ↑↓ select · Enter toggle · p pause/resume · d drain · R reload · S stop · g start · +/- slots · P pacer · w worktree · J/K tiers",
        Screen::Panels => "panels come from tui.toml in the board home · ? help",
    }
}

fn footer(app: &App, f: &mut Frame, area: Rect) {
    let line = match &app.mode {
        Mode::Filter => {
            Line::from(vec![sp("/", fg(theme::ACCENT)), sp(app.input.clone(), base()), sp("▌", fg(theme::ACCENT)), sp("   Enter keep · Esc clear", dim())])
        }
        Mode::Palette => {
            let sugg: Vec<&str> = App::COMMANDS.iter().copied().filter(|c| c.starts_with(app.input.split(' ').next().unwrap_or(""))).take(10).collect();
            Line::from(vec![sp(":", fg(theme::ACCENT)), sp(app.input.clone(), base()), sp("▌", fg(theme::ACCENT)), sp(format!("   {}", sugg.join(" ")), dim())])
        }
        _ => {
            if let Some((t, _, err)) = &app.toast {
                Line::from(sp(format!(" {t}"), if *err { fg(theme::BAD) } else { fg(theme::GOOD) }))
            } else if let Some(e) = &app.err {
                Line::from(sp(format!(" snapshot error: {e}"), fg(theme::BAD)))
            } else {
                Line::from(sp(format!(" {}", hints(app)), dim()))
            }
        }
    };
    f.render_widget(Paragraph::new(line).style(base()), area);
}

// ---------------------------------------------------------------------------------------------- board lanes
fn task_line(t: &Task, w: usize, selected: bool, focused: bool) -> Line<'static> {
    let kc = theme::kind_color(t.kind());
    let id: String = t.id.chars().skip(2).take(4).collect();
    let mut spans = if t.is_negative() {
        let used = 1 + 4 + 1 + 3 + 1;
        vec![
            sp("▌", dim()),
            sp(id, dim()),
            sp(" ", base()),
            sp("NEG", dim().add_modifier(Modifier::BOLD)),
            sp(" ", base()),
            sp(pad(&t.title, w.saturating_sub(used)), dim()),
        ]
    } else {
        let used = 1 + 4 + 1 + 1 + 1;
        vec![
            sp("▌", fg(kc)),
            sp(id, dim()),
            sp(" ", base()),
            sp(theme::tier_tag(&t.tier), fg(theme::tier_color(&t.tier)).add_modifier(Modifier::BOLD)),
            sp(" ", base()),
            sp(pad(&t.title, w.saturating_sub(used)), if t.kind().is_empty() { base() } else { fg(kc) }),
        ]
    };
    if selected {
        let bg = if focused { theme::SEL_BG } else { Color::Rgb(30, 38, 56) };
        for s in spans.iter_mut() {
            s.style = s.style.bg(bg);
        }
    }
    Line::from(spans)
}

fn dep_title(app: &App, id: &str) -> String {
    app.task(id).map(|x| x.title.clone()).unwrap_or_else(|| id.chars().skip(2).take(4).collect::<String>())
}

/// What a card is itself waiting on, in a few words (used for the second card in a wait chain).
fn why_short(app: &App, t: &Task) -> String {
    match t.state.as_str() {
        "running" | "claimed" => match t.park.as_ref().filter(|p| p.active()) {
            Some(p) => format!("running as {}", p.what()),
            None => "running now".into(),
        },
        "ready" => t.wait_reason.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| "ready, queued".into()),
        "stuck" => "a dependency is dead".into(),
        "dead" | "cancelled" => "dead".into(),
        "waiting" => {
            if !t.deps_open.is_empty() {
                format!("waits on {}", t.deps_open.iter().map(|d| dep_title(app, d)).collect::<Vec<_>>().join(" · "))
            } else if let Some(nb) = t.not_before {
                format!("starts in {}", fmt_dur((nb - app.now()).max(0.0)))
            } else {
                "waiting".into()
            }
        }
        other => other.to_string(),
    }
}

/// Extra lines under a waiting card: each card it waits on, then what that card is itself waiting on.
fn wait_chain(app: &App, t: &Task) -> Vec<String> {
    fn walk(app: &App, t: &Task, depth: usize, seen: &mut Vec<String>, out: &mut Vec<String>) {
        let ind = format!("  {}", "  ".repeat(depth));
        for d in t.deps_open.iter().take(3) {
            if seen.contains(d) {
                continue;
            }
            seen.push(d.clone());
            let Some(dt) = app.task(d) else {
                out.push(format!("{ind}{} {}", if depth == 0 { "↳ waits on" } else { "⤷ waits on" }, dep_title(app, d)));
                continue;
            };
            out.push(format!("{ind}{} {}", if depth == 0 { "↳ waits on" } else { "⤷ waits on" }, dt.title));
            if !dt.deps_open.is_empty() && depth < 2 {
                walk(app, dt, depth + 1, seen, out);
            } else {
                out.push(format!("{ind}  ⤷ {}", why_short(app, dt)));
            }
        }
    }
    let mut out = vec![];
    walk(app, t, 0, &mut vec![t.id.clone()], &mut out);
    out
}

fn second_line(app: &App, t: &Task) -> Option<String> {
    if t.state == "stuck" {
        return Some("  ✖ a dependency is dead".into());
    }
    if t.state == "ready" {
        if let Some(why) = t.wait_reason.as_deref().filter(|s| !s.is_empty()) {
            return Some(format!("  ↳ {why}"));
        }
    }
    if t.state == "waiting" {
        if let Some(p) = t.park.as_ref().filter(|p| p.active()) {
            return Some(format!("  ⏸ parked on {}", p.what()));
        }
        if !t.deps_open.is_empty() {
            return Some(format!("  ↳ waits on {}", t.deps_open.iter().map(|d| dep_title(app, d)).collect::<Vec<_>>().join(" · ")));
        }
        if let Some(nb) = t.not_before {
            return Some(format!("  ↳ starts in {}", fmt_dur(nb - app.now())));
        }
    }
    if t.state == "claimed" {
        if let Some(c) = app.snap.runner.children.iter().find(|c| c.task_id == t.id) {
            let stall = c.stall.as_ref().map(|s| format!("  ⚠ {s}")).unwrap_or_default();
            return Some(format!("  ▶ {}:{} {}{}", c.harness, c.model.clone().unwrap_or_default(), fmt_dur(c.elapsed_s + app.age()), stall));
        }
    }
    if t.state == "running" {
        if let Some(p) = t.park.as_ref().filter(|p| p.active()) {
            let el = p.at.map(|at| format!(" {}", fmt_dur((app.now() - at).max(0.0)))).unwrap_or_default();
            return Some(format!("  ⏸ {}{el}", p.what()));
        }
    }
    if t.is_negative() {
        return Some("  – negative outcome".into());
    }
    if t.state == "dead" && t.attempts > 0 {
        return Some(format!("  ✖ after {} attempt(s)", t.attempts));
    }
    None
}

fn board(app: &mut App, f: &mut Frame, area: Rect) {
    let parts = Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(2), Constraint::Min(3)]).split(area);
    // summary lines
    let snap = &app.snap;
    let mut s1 = vec![];
    for (i, b) in snap.boards.iter().enumerate() {
        if app.sel.board != 0 && app.sel.board - 1 != i {
            continue;
        }
        s1.push(sp(format!(" {}", b.name), fg(theme::ACCENT).add_modifier(Modifier::BOLD)));
        if b.paused {
            s1.push(sp(" PAUSED", fg(theme::WARN).add_modifier(Modifier::BOLD)));
        }
        s1.push(sp(format!(" run {}/{}", b.running, b.max_slots.map(|m| m.to_string()).unwrap_or_else(|| "∞".into())), base()));
        let c = |k: &str| b.counts.get(k).copied().unwrap_or(0);
        let neg = snap.tasks.iter().filter(|t| t.board == b.name && t.is_negative()).count() as i64;
        let dead_err = (c("dead") - neg).max(0);
        let neg_txt = if neg > 0 { format!(" neg {neg}") } else { String::new() };
        s1.push(sp(format!(" · open {} done {} dead {dead_err}{neg_txt}", c("open") + c("claimed"), c("done")), dim()));
        if let Some(t) = &b.default_tier {
            s1.push(sp(format!(" · tier {t}"), dim()));
        }
        s1.push(sp("   ", dim()));
    }
    let mut caps = vec![sp(" caps ", dim())];
    if snap.caps.is_empty() {
        caps.push(sp("none", dim()));
    }
    for (b, c) in &snap.caps {
        let mut parts = vec![];
        if let Some(o) = c.get("max_output").and_then(Value::as_i64) {
            parts.push(format!("≤{} out/{}", fmt_num(o), c.get("window").and_then(Value::as_str).unwrap_or("5h")));
        }
        if let Some(p) = c.get("max_5h_pct").and_then(Value::as_f64) {
            parts.push(format!("plan ≤{p:.0}%"));
        }
        let hit = snap.usage.iter().find(|u| &u.bucket == b).and_then(|u| u.cap_hit.clone());
        caps.push(sp(format!("{b} {}  ", parts.join(" ")), if hit.is_some() { fg(theme::WARN) } else { dim() }));
    }
    f.render_widget(Paragraph::new(vec![Line::from(s1), Line::from(caps)]).style(base()), parts[0]);

    let lanes = app.lanes();
    let n = ((parts[1].width / 21) as usize).clamp(2, 6);
    let first = app.sel.lane.saturating_sub(n - 1).min(6 - n);
    let cols = Layout::default().direction(ACROSS).constraints(vec![Constraint::Ratio(1, n as u32); n]).split(parts[1]);
    let mut recorded: Vec<(String, Rect)> = vec![];
    let mut hits: Vec<(Rect, Hit)> = vec![];
    for (ci, col) in cols.iter().enumerate() {
        let li = first + ci;
        let tasks = &lanes[li];
        let focused = li == app.sel.lane && app.screen == Screen::Board;
        let count = if li == 1 {
            format!("{}/{}", tasks.len(), app.snap.runner.slots_effective)
        } else if li == 5 {
            let neg = tasks.iter().filter(|t| t.is_negative()).count();
            let err = tasks.len() - neg;
            if neg > 0 { format!("{err} (+{neg} neg)") } else { err.to_string() }
        } else {
            tasks.len().to_string()
        };
        let blk = block(&format!("{} {}", LANES[li], count), focused);
        let inner = blk.inner(*col);
        f.render_widget(blk, *col);
        let w = inner.width as usize;
        // keep the selected card in view
        let selected = app.sel.lane_idx[li];
        let heights: Vec<usize> = tasks.iter().map(|t| 1 + usize::from(second_line(app, t).is_some())).collect();
        let (mut start, mut acc) = (0usize, 0usize);
        for i in 0..=selected.min(tasks.len().saturating_sub(1)) {
            acc += heights.get(i).copied().unwrap_or(1);
            while acc > inner.height as usize && start < i {
                acc -= heights[start];
                start += 1;
            }
        }
        let mut y = inner.y;
        for (i, t) in tasks.iter().enumerate().skip(start) {
            if y >= inner.bottom() {
                break;
            }
            let is_sel = i == selected && app.screen == Screen::Board;
            let rect = Rect { x: inner.x, y, width: inner.width, height: 1 };
            f.render_widget(Paragraph::new(task_line(t, w, is_sel, focused)), rect);
            recorded.push((t.id.clone(), rect));
            hits.push((rect, Hit::Lane(li, i)));
            y += 1;
            let extras: Vec<String> = if t.state == "waiting" && !t.deps_open.is_empty() && t.park.as_ref().map_or(true, |p| !p.active()) {
                wait_chain(app, t)
            } else {
                second_line(app, t).into_iter().collect()
            };
            let dead = (t.state == "dead" && !t.is_negative()) || t.state == "stuck";
            let st = if dead {
                fg(theme::BAD)
            } else if t.state == "ready" && t.wait_reason.as_deref().is_some_and(|s| !s.is_empty()) {
                fg(theme::WARN)
            } else {
                dim()
            };
            for extra in extras {
                if y >= inner.bottom() {
                    break;
                }
                f.render_widget(Paragraph::new(sp(trunc(&extra, w), st)), Rect { x: inner.x, y, width: inner.width, height: 1 });
                y += 1;
            }
        }
        if tasks.is_empty() {
            f.render_widget(Paragraph::new(sp("—", dim())), Rect { x: inner.x + 1, y: inner.y, width: inner.width.saturating_sub(1), height: 1 });
        }
    }
    app.rows.extend(recorded);
    app.hits.extend(hits);
}

// ---------------------------------------------------------------------------------------------- running
fn running(app: &mut App, f: &mut Frame, area: Rect) {
    let kids = &app.snap.runner.children;
    let jobs = app.snap.runner.jobs.clone();
    let job_rows = if jobs.is_empty() { 0 } else { jobs.len() as u16 + 1 };
    let top_h = (kids.len() as u16 + job_rows + 3).clamp(4, area.height / 2);
    let parts = Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(top_h), Constraint::Min(4)]).split(area);
    let blk = block(&format!("running {} agents / {} slots, {} background jobs", kids.len(), app.snap.runner.slots_effective, jobs.len()), true);
    let inner = blk.inner(parts[0]);
    f.render_widget(blk, parts[0]);
    let w = inner.width as usize;
    let mut lines = vec![Line::from(sp(
        format!(" {:<4} {:<8} {:<26} {:<7} {:<14} {:<30} title", "run", "task", "harness:model", "effort", "bucket", "elapsed / timeout"),
        dim(),
    ))];
    let age = app.age();
    let mut hits = vec![];
    for (i, c) in kids.iter().enumerate() {
        let el = c.elapsed_s + age;
        let frac = c.timeout_s.map(|t| el / t.max(1.0)).unwrap_or(0.0);
        let hm = format!("{}:{}", c.harness, c.model.clone().unwrap_or_default());
        let kind = app.task(&c.task_id).map(|t| t.kind().to_string()).unwrap_or_default();
        let mut spans = vec![
            sp(format!(" {:<4} ", c.run), dim()),
            sp(format!("{:<8} ", c.task_id.chars().take(8).collect::<String>()), fg(theme::kind_color(&kind))),
            sp(format!("{} ", pad(&hm, 26)), base()),
            sp(format!("{:<7} ", c.effort.clone().unwrap_or_default()), dim()),
            sp(format!("{} ", pad(&c.bucket.clone().unwrap_or_default(), 14)), fg(theme::ACCENT)),
        ];
        spans.extend(bar(12, frac, None, gauge_color(frac)));
        spans.push(sp(format!(" {:>6}/{:<6} ", fmt_dur(el), c.timeout_s.map(fmt_dur).unwrap_or_else(|| "∞".into())), base()));
        if let Some(st) = &c.stall {
            spans.push(sp(format!("⚠ {st}  "), fg(theme::BAD)));
        }
        spans.push(sp(trunc(c.title.as_deref().unwrap_or(""), w.saturating_sub(100).max(10)), base()));
        let mut line = Line::from(spans);
        if i == app.sel.run {
            line = line.patch_style(Style::default().bg(theme::SEL_BG));
        }
        let rect = Rect { x: inner.x, y: inner.y + 1 + i as u16, width: inner.width, height: 1 };
        if rect.bottom() <= inner.bottom() {
            hits.push((rect, Hit::Run(i)));
        }
        lines.push(line);
    }
    if kids.is_empty() && jobs.is_empty() {
        lines.push(Line::from(sp("  nothing is running", dim())));
    }
    if !jobs.is_empty() {
        lines.push(Line::from(sp(format!(" {:<4} {:<8} {:<26} parked cards, work runs in the background", "", "task", "job"), dim())));
        let now = app.now();
        for j in &jobs {
            let what = match (j.pid, &j.file) {
                (Some(p), _) => format!("pid {p}"),
                (_, Some(f)) => format!("file {}", f.rsplit('/').next().unwrap_or(f)),
                _ => "a job".into(),
            };
            let el = j.since.map(|t| fmt_dur((now - t).max(0.0))).unwrap_or_default();
            lines.push(Line::from(vec![
                sp(format!(" {:<4} ", "⏸"), dim()),
                sp(format!("{:<8} ", j.task_id.chars().take(8).collect::<String>()), base()),
                sp(format!("{} ", pad(&what, 26)), base()),
                sp(format!("{:>8}  ", el), base()),
                sp(trunc(j.title.as_deref().unwrap_or(""), w.saturating_sub(60).max(10)), base()),
            ]));
        }
    }
    f.render_widget(Paragraph::new(lines).style(base()), inner);
    app.hits.extend(hits);

    // live tail of the selected run
    let sel_run = app.snap.runner.children.get(app.sel.run);
    let title = match sel_run {
        Some(c) => format!("output · run {} · {} · {}", c.run, c.task_id, c.harness),
        None => "output".into(),
    };
    let blk = block(&title, false);
    let inner = blk.inner(parts[1]);
    f.render_widget(blk, parts[1]);
    let mut lines: Vec<Line> = vec![];
    match (&app.snap.tail, sel_run) {
        (Some(t), Some(c)) if t.run == c.run => {
            if let Some(e) = &t.error {
                lines.push(Line::from(sp(format!("  {e}"), fg(theme::BAD))));
            }
            let err_lines: Vec<&str> = t.err.lines().filter(|l| !l.trim().is_empty()).collect();
            let err_h = err_lines.len().min(3);
            let out_h = (inner.height as usize).saturating_sub(err_h + usize::from(err_h > 0));
            let all: Vec<&str> = t.out.lines().collect();
            let from = all.len().saturating_sub(out_h);
            for l in &all[from..] {
                lines.push(Line::from(sp(format!(" {}", trunc(l, inner.width as usize - 2)), base())));
            }
            if err_h > 0 {
                lines.push(Line::from(sp(" stderr", dim())));
                for l in &err_lines[err_lines.len() - err_h..] {
                    lines.push(Line::from(sp(format!(" {}", trunc(l, inner.width as usize - 2)), fg(theme::WARN))));
                }
            }
        }
        (_, Some(_)) => lines.push(Line::from(sp("  loading output …", dim()))),
        _ => lines.push(Line::from(sp("  select a run to follow its output", dim()))),
    }
    f.render_widget(Paragraph::new(lines).style(base()), inner);
}

// ---------------------------------------------------------------------------------------------- card detail
fn state_color(state: &str) -> Color {
    match state {
        "done" => theme::GOOD,
        "dead" | "stuck" | "cancelled" => theme::BAD,
        "claimed" | "running" => theme::ACCENT,
        "waiting" => theme::WARN,
        _ => theme::FG,
    }
}

fn card(app: &mut App, f: &mut Frame, area: Rect) {
    let Some(id) = app.card.clone() else {
        f.render_widget(Paragraph::new("  no card open: press Enter on a card in Board, Running or Decisions").style(dim()), area);
        return;
    };
    let t = app.task(&id).cloned();
    let detail = app.detail.as_ref().filter(|(d, _, _)| *d == id).map(|(_, d, _)| d.clone());
    let cols = Layout::default().direction(ACROSS).constraints([Constraint::Percentage(60), Constraint::Percentage(40)]).split(area);

    // left: title, meta, body, notes, result
    let blk = block(&format!("card {id}"), true);
    let inner = blk.inner(cols[0]);
    f.render_widget(blk, cols[0]);
    let mut lines: Vec<Line> = vec![];
    let title =
        detail.as_ref().map(|d| d.title.clone()).or_else(|| t.as_ref().map(|t| t.title.clone())).unwrap_or_else(|| "(not in the current snapshot)".into());
    lines.push(Line::from(sp(title, bold())));
    if let Some(t) = &t {
        let kc = theme::kind_color(t.kind());
        let state_span = if t.is_negative() {
            sp("NEG", dim().add_modifier(Modifier::BOLD))
        } else {
            sp(t.state.clone(), fg(state_color(&t.state)).add_modifier(Modifier::BOLD))
        };
        let mut m = vec![
            state_span,
            sp(format!(" · {} · prio {} · ", t.board, t.priority), dim()),
            sp(format!("tier {}", t.tier), fg(theme::tier_color(&t.tier))),
            sp(format!(" · attempts {}/{}", t.attempts, t.max_attempts), dim()),
        ];
        if let Some(h) = &t.harness {
            m.push(sp(format!(" · {h}{}", t.model.as_ref().map(|x| format!(":{x}")).unwrap_or_default()), dim()));
        }
        lines.push(Line::from(m));
        let mut l = vec![];
        for (k, v) in &t.labels {
            if matches!(k.as_str(), "worktree_branch" | "worktree_dir" | "worktree") {
                continue;
            }
            l.push(sp(format!("{k}={v} "), if k == "type" { fg(kc) } else { dim() }));
        }
        if let Some(w) = &t.workdir {
            l.push(sp(format!(" ⌂ {w}"), dim()));
        }
        if !l.is_empty() {
            lines.push(Line::from(l));
        }
        let wt_branch = t.labels.get("worktree_branch").map(String::as_str);
        let wt_dir = t.labels.get("worktree_dir").map(String::as_str);
        let wt_flag = t.labels.get("worktree").map(String::as_str);
        if wt_branch.is_some() || wt_dir.is_some() || wt_flag.is_some() {
            let mut wt = vec![sp("worktree ", dim())];
            if let Some(b) = wt_branch {
                wt.push(sp(b.to_string(), fg(theme::ACCENT).add_modifier(Modifier::BOLD)));
                if wt_dir.is_some() || wt_flag.is_some() {
                    wt.push(sp(" · ", dim()));
                }
            }
            if let Some(d) = wt_dir {
                wt.push(sp(d.to_string(), dim()));
                if wt_flag.is_some() {
                    wt.push(sp(" · ", dim()));
                }
            }
            if let Some(fl) = wt_flag {
                let st = if fl.eq_ignore_ascii_case("off") { fg(theme::WARN) } else { dim() };
                wt.push(sp(format!("worktree={fl}"), st));
            }
            lines.push(Line::from(wt));
        }
        if let Some(why) = t.wait_reason.as_deref().filter(|s| !s.is_empty()) {
            lines.push(Line::from(vec![sp("wait ", fg(theme::WARN).add_modifier(Modifier::BOLD)), sp(why.to_string(), fg(theme::WARN))]));
        }
        if let Some(p) = &t.park {
            let head = if p.active() { format!("parked on {}", p.what()) } else { format!("park woken ({})", p.woke.clone().unwrap_or_else(|| p.what())) };
            lines.push(Line::from(vec![sp("park ", fg(theme::WARN).add_modifier(Modifier::BOLD)), sp(head, fg(theme::WARN))]));
            if let Some(n) = &p.note {
                lines.push(Line::from(vec![sp("note ", dim()), sp(n.clone(), base())]));
            }
        }
    }
    lines.push(Line::from(""));
    match &detail {
        Some(d) => {
            lines.push(Line::from(sp("── Body ──", fg(theme::ACCENT))));
            lines.extend(md_lines(&d.body, inner.width as usize));
            lines.push(Line::from(""));
            lines.push(Line::from(sp(format!("── Notes ({}) ──", d.notes.len()), fg(theme::ACCENT))));
            for n in &d.notes {
                lines.push(Line::from(vec![
                    sp(format!("{} ", fmt_hms(n.ts)), dim()),
                    sp(format!("{}: ", n.author), fg(theme::WARN)),
                    sp(n.body.clone(), base()),
                ]));
            }
            if let Some(r) = &d.result {
                lines.push(Line::from(""));
                lines.push(Line::from(sp("── Result ──", fg(theme::ACCENT))));
                lines.extend(md_lines(r, inner.width as usize));
            }
        }
        None => match &app.detail_err {
            Some(e) => lines.push(Line::from(sp(format!("could not load the card: {e}"), fg(theme::BAD)))),
            None => lines.push(Line::from(sp("loading card …", dim()))),
        },
    }
    f.render_widget(Paragraph::new(lines).style(base()).wrap(Wrap { trim: false }).scroll((app.sel.card_scroll, 0)), inner);

    // right: runs history, dependency tree
    let blk = block("runs · dependencies", false);
    let inner = blk.inner(cols[1]);
    f.render_widget(blk, cols[1]);
    let mut lines: Vec<Line> = vec![Line::from(sp("── Runs ──", fg(theme::ACCENT)))];
    match &detail {
        Some(d) if !d.runs.is_empty() => {
            for r in &d.runs {
                let out = r.outcome.clone().unwrap_or_else(|| "running".into());
                let oc = match out.as_str() {
                    "done" => theme::GOOD,
                    "running" => theme::ACCENT,
                    _ => theme::BAD,
                };
                let dur = match (r.started_at, r.ended_at) {
                    (Some(a), Some(b)) => fmt_dur(b - a),
                    (Some(a), None) => fmt_dur(app.now() - a),
                    _ => "-".into(),
                };
                lines.push(Line::from(vec![
                    sp(format!("#{} ", r.id), dim()),
                    sp(format!("{}:{} ", r.harness.clone().unwrap_or_default(), r.model.clone().unwrap_or_default()), base()),
                    sp(out, fg(oc).add_modifier(Modifier::BOLD)),
                    sp(format!("  {dur}  in {} out {}", fmt_num(r.input.unwrap_or(0)), fmt_num(r.output.unwrap_or(0))), dim()),
                ]));
            }
        }
        Some(_) => lines.push(Line::from(sp("  no runs yet", dim()))),
        None => lines.push(Line::from(sp("  …", dim()))),
    }
    if let Some(t) = &t {
        if let Some(r) = &t.result_head {
            if detail.is_none() {
                lines.push(Line::from(sp(format!("result: {}", trunc(r, inner.width as usize - 8)), dim())));
            }
        }
    }
    lines.push(Line::from(""));
    lines.push(Line::from(sp("── Depends on ──", fg(theme::ACCENT))));
    let deps: Vec<String> = t.as_ref().map(|t| t.deps.clone()).or_else(|| detail.as_ref().map(|d| d.deps.iter().map(value_id).collect())).unwrap_or_default();
    if deps.is_empty() {
        lines.push(Line::from(sp("  none", dim())));
    }
    for (i, d) in deps.iter().enumerate() {
        let dt = app.task(d);
        let is_neg = dt.is_some_and(Task::is_negative);
        let (st, title) = dt.map(|x| (x.state.clone(), x.title.clone())).unwrap_or(("done?".into(), "(closed, not listed)".into()));
        let open = t.as_ref().is_some_and(|t| t.deps_open.contains(d));
        let (badge, col) = if !open {
            ("done".into(), theme::GOOD)
        } else if is_neg {
            ("NEG".into(), theme::DIM)
        } else {
            (st.clone(), state_color(&st))
        };
        lines.push(Line::from(vec![
            sp(if i + 1 == deps.len() { " └ " } else { " ├ " }, dim()),
            sp(format!("{} ", d.chars().take(8).collect::<String>()), dim()),
            sp(format!("[{badge}] "), fg(col)),
            sp(trunc(&title, (inner.width as usize).saturating_sub(24)), base()),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(sp("── Blocks ──", fg(theme::ACCENT))));
    let blocks: Vec<&Task> = app.snap.tasks.iter().filter(|x| x.deps.contains(&id)).collect();
    if blocks.is_empty() {
        lines.push(Line::from(sp("  nothing waits on this card", dim())));
    }
    for (i, b) in blocks.iter().enumerate() {
        let (badge, col) = if b.is_negative() { ("NEG", theme::DIM) } else { (b.state.as_str(), state_color(&b.state)) };
        lines.push(Line::from(vec![
            sp(if i + 1 == blocks.len() { " └ " } else { " ├ " }, dim()),
            sp(format!("{} ", b.id.chars().take(8).collect::<String>()), dim()),
            sp(format!("[{badge}] "), fg(col)),
            sp(trunc(&b.title, (inner.width as usize).saturating_sub(24)), base()),
        ]));
    }
    f.render_widget(Paragraph::new(lines).style(base()), inner);
}

// ---------------------------------------------------------------------------------------------- decisions
fn decisions(app: &mut App, f: &mut Frame, area: Rect) {
    let list: Vec<Task> = app.decisions().into_iter().cloned().collect();
    let cols = Layout::default().direction(ACROSS).constraints([Constraint::Percentage(42), Constraint::Percentage(58)]).split(area);
    let blk = block(&format!("waiting for {} ({})", if app.show_all_manual { "anyone".to_string() } else { app.owner() }, list.len()), true);
    let inner = blk.inner(cols[0]);
    f.render_widget(blk, cols[0]);
    let mut lines = vec![];
    let mut hits = vec![];
    for (i, t) in list.iter().enumerate() {
        let age = t.created_at.map(|c| fmt_dur(app.now() - c)).unwrap_or_default();
        let mut l = Line::from(vec![
            sp(" ▌", fg(theme::kind_color(t.kind()))),
            sp(format!("{} ", t.id.chars().take(8).collect::<String>()), dim()),
            sp(format!("p{} ", t.priority), fg(theme::WARN)),
            sp(trunc(&t.title, (inner.width as usize).saturating_sub(24)), base()),
            sp(format!("  {age}"), dim()),
        ]);
        if i == app.sel.dec {
            l = l.patch_style(Style::default().bg(theme::SEL_BG));
        }
        hits.push((Rect { x: inner.x, y: inner.y + i as u16, width: inner.width, height: 1 }, Hit::Decision(i)));
        lines.push(l);
    }
    if list.is_empty() {
        lines.push(Line::from(sp(format!("  nothing waits for {}. all clear.", app.owner()), fg(theme::GOOD))));
    }
    f.render_widget(Paragraph::new(lines).style(base()), inner);
    app.hits.extend(hits.into_iter().filter(|(r, _)| r.bottom() <= inner.bottom()));

    let blk = block("options and default", false);
    let inner = blk.inner(cols[1]);
    f.render_widget(blk, cols[1]);
    let mut lines = vec![];
    if let Some(t) = list.get(app.sel.dec) {
        lines.push(Line::from(sp(t.title.clone(), bold())));
        lines.push(Line::from(""));
        match app.detail.as_ref().filter(|(d, _, _)| *d == t.id) {
            Some((_, d, _)) => {
                for l in md_lines(&d.body, inner.width as usize) {
                    let text: String = l.spans.iter().map(|s| s.content.as_ref()).collect();
                    if text.trim_start().to_lowercase().starts_with("default") {
                        lines.push(Line::from(sp(text, fg(theme::GOOD).add_modifier(Modifier::BOLD))));
                    } else {
                        lines.push(l);
                    }
                }
                if !d.notes.is_empty() {
                    lines.push(Line::from(""));
                    lines.push(Line::from(sp("notes", fg(theme::ACCENT))));
                    for n in d.notes.iter().rev().take(4).rev() {
                        lines.push(Line::from(vec![sp(format!("{}: ", n.author), fg(theme::WARN)), sp(n.body.clone(), base())]));
                    }
                }
            }
            None => lines.push(Line::from(sp("loading …", dim()))),
        }
        lines.push(Line::from(""));
        lines.push(Line::from(sp("press a to answer: your text is posted as a note and the card is closed", dim())));
        lines.push(Line::from(sp("drag to select and copy · y copies the selection or the whole card", dim())));
    }
    // wrap here instead of in the Paragraph, so every drawn row is known and a mouse selection maps back to text
    let w = inner.width as usize;
    let mut rows: Vec<Line> = vec![];
    for l in lines {
        let text: String = l.spans.iter().map(|s| s.content.as_ref()).collect();
        if w == 0 || text_width(&text) <= w {
            rows.push(l);
        } else {
            let st = l.style.patch(l.spans.first().map(|s| s.style).unwrap_or_default());
            rows.extend(wrap(&text, w).into_iter().map(|part| Line::from(Span::styled(part, st))));
        }
    }
    let cur = list.get(app.sel.dec).map(|t| t.id.clone());
    if app.dec_sel.is_some() && app.dec_sel_card != cur {
        app.dec_sel = None;
    }
    app.dec_lines = rows.iter().map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect()).collect();
    app.dec_area = inner;
    if let Some((a, b)) = app.dec_sel {
        let (s, e) = if a <= b { (a, b) } else { (b, a) };
        for r in s.0..=e.0.min(rows.len().saturating_sub(1)) {
            let Some(text) = app.dec_lines.get(r) else { break };
            let from = if r == s.0 { s.1 } else { 0 };
            let to = if r == e.0 { e.1 + 1 } else { usize::MAX };
            let st = rows[r].style.patch(rows[r].spans.first().map(|x| x.style).unwrap_or_default());
            rows[r] = Line::from(vec![
                Span::styled(cut_cols(text, 0, from), st),
                Span::styled(cut_cols(text, from, to), theme::sel()),
                Span::styled(cut_cols(text, to, usize::MAX), st),
            ]);
        }
    }
    f.render_widget(Paragraph::new(rows).style(base()), inner);
}

// ---------------------------------------------------------------------------------------------- usage
fn usage(app: &mut App, f: &mut Frame, area: Rect) {
    let snap = &app.snap;
    let split_h = (snap.usage_split.len() as u16 + 3).clamp(4, area.height / 3);
    let parts = Layout::default().direction(Direction::Vertical).constraints([Constraint::Min(5), Constraint::Length(split_h)]).split(area);
    let blk = block("buckets · output tokens per hour, last 7 days (oldest left)", true);
    let inner = blk.inner(parts[0]);
    f.render_widget(blk, parts[0]);
    let per = 4u16;
    let skip = app.sel.usage_scroll as usize;
    for (i, b) in snap.usage.iter().enumerate().skip(skip) {
        let y = inner.y + (i - skip) as u16 * per;
        if y + per > inner.bottom() {
            break;
        }
        let budget_span = b.launch_budget.or_else(|| b.pacer.as_ref().map(|p| p.budget)).map(|n| {
            let iv = b
                .pacer
                .as_ref()
                .map(|p| p.interval.as_str())
                .filter(|s| !s.is_empty())
                .or(snap.runner.pace_interval.as_deref())
                .unwrap_or("10m");
            let why = b.budget_why.as_deref().or_else(|| b.pacer.as_ref().map(|p| p.why.as_str())).unwrap_or("");
            let txt = if why.is_empty() { format!("{n}/{iv}") } else { format!("{n}/{iv} {why}") };
            let col = if n == 0 { theme::WARN } else { theme::ACCENT };
            sp(format!("{} ", pad(&txt, 14)), fg(col))
        });
        let mut head_spans = vec![
            sp(format!(" {:<16}", b.bucket), fg(theme::ACCENT).add_modifier(Modifier::BOLD)),
            sp(format!("5h {:>7} / {:<4}", fmt_num(b.h5.output), b.h5.calls), base()),
            sp(format!("24h {:>7} / {:<5}", fmt_num(b.h24.output), b.h24.calls), base()),
            sp(format!("7d {:>7} / {:<6}", fmt_num(b.d7.output), b.d7.calls), base()),
            sp(format!("runs {:<3}", b.d7.runs), dim()),
        ];
        if let Some(bs) = budget_span {
            head_spans.push(bs);
        }
        let used: usize = head_spans.iter().map(|s| s.content.chars().count()).sum();
        let room = (inner.width as usize).saturating_sub(used);
        let state = match (&b.limit, &b.cap_hit) {
            (Some(l), _) => (trunc(&format!("LIMITED {}", l.kind.clone().unwrap_or_default()), room), theme::BAD),
            (None, Some(c)) => (trunc(&format!("held: {c}"), room), theme::WARN),
            _ => ("ok".to_string(), theme::GOOD),
        };
        head_spans.push(sp(state.0, fg(state.1)));
        let head = Line::from(head_spans);
        f.render_widget(Paragraph::new(head), Rect { x: inner.x, y, width: inner.width, height: 1 });
        let data: Vec<u64> = snap.usage_hourly.get(&b.bucket).map(|v| v.iter().map(|x| (*x).max(0) as u64).collect()).unwrap_or_default();
        let w = inner.width as usize - 2;
        let tail: Vec<u64> = if data.len() > w { data[data.len() - w..].to_vec() } else { data };
        let color = if b.limit.is_some() { theme::BAD } else { theme::kind_color("dive") };
        f.render_widget(Sparkline::default().data(tail).style(fg(color)), Rect { x: inner.x + 1, y: y + 1, width: inner.width - 2, height: 2 });
    }
    if snap.usage.is_empty() {
        f.render_widget(Paragraph::new(" no usage recorded yet").style(dim()), inner);
    }

    let blk = block("last 7 days by harness · model · bucket", false);
    let inner = blk.inner(parts[1]);
    f.render_widget(blk, parts[1]);
    let mut lines =
        vec![Line::from(sp(format!(" {:<12} {:<26} {:<16} {:>7} {:>9} {:>9}", "harness", "model", "bucket", "calls", "output", "fresh in"), dim()))];
    for r in &snap.usage_split {
        lines.push(Line::from(sp(
            format!(
                " {:<12} {:<26} {:<16} {:>7} {:>9} {:>9}{}",
                pad(r.harness.as_deref().unwrap_or("-"), 12),
                pad(r.model.as_deref().unwrap_or("-"), 26),
                pad(r.bucket.as_deref().unwrap_or("-"), 16),
                r.calls,
                fmt_num(r.output),
                fmt_num(r.fresh_in),
                r.cost_usd.filter(|c| *c > 0.0).map(|c| format!("  ${c:.2}")).unwrap_or_default()
            ),
            base(),
        )));
    }
    f.render_widget(Paragraph::new(lines).style(base()), inner);
}

// ---------------------------------------------------------------------------------------------- feed
fn feed_text(app: &App, e: &crate::model::Event) -> String {
    let title = e.task_id.as_deref().and_then(|id| app.task(id)).map(|t| t.title.clone()).unwrap_or_default();
    let mut extra = String::new();
    if let Value::Object(o) = &e.payload {
        for key in ["title", "result", "why", "body", "name", "scope", "reason", "msg"] {
            if let Some(v) = o.get(key) {
                let s = match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
                if key == "title" && !title.is_empty() {
                    continue;
                }
                extra = format!("{key}: {s}");
                break;
            }
        }
    }
    format!("{title}  {extra}")
}

fn feed(app: &mut App, f: &mut Frame, area: Rect) {
    let blk = block(&format!("events · {} (newest first)", app.feed.len()), true);
    let inner = blk.inner(area);
    f.render_widget(blk, area);
    app.feed_area = inner;
    let filter = app.filter.to_lowercase();
    let events: Vec<&crate::model::Event> = app
        .feed
        .iter()
        .rev()
        .filter(|e| {
            filter.is_empty() || {
                let hay = format!("{} {} {} {}", e.kind, e.task_id.clone().unwrap_or_default(), e.actor.clone().unwrap_or_default(), feed_text(app, e))
                    .to_lowercase();
                filter.split_whitespace().all(|w| hay.contains(w))
            }
        })
        .collect();
    let h = inner.height as usize;
    let top = app.sel.feed.saturating_sub(h.saturating_sub(1)).min(events.len().saturating_sub(h));
    let mut lines = vec![];
    let mut hits = vec![];
    for (i, e) in events.iter().enumerate().skip(top).take(h) {
        let is_neg = e.kind == "dead" && e.payload.get("outcome").and_then(Value::as_str) == Some("negative");
        let (kind_lbl, kc) = if is_neg { ("dead (neg)", theme::DIM) } else { (e.kind.as_str(), theme::event_color(&e.kind)) };
        let mut l = Line::from(vec![
            sp(format!(" {} ", fmt_hms(e.ts)), dim()),
            sp(format!("{:<13}", pad(kind_lbl, 13)), fg(kc).add_modifier(Modifier::BOLD)),
            sp(format!("{:<9}", e.task_id.as_deref().map(|t| t.chars().take(8).collect::<String>()).unwrap_or_default()), dim()),
            sp(format!("{:<11}", pad(e.actor.as_deref().unwrap_or(""), 11)), fg(theme::WARN)),
            sp(trunc(&feed_text(app, e), (inner.width as usize).saturating_sub(40)), base()),
        ]);
        if i == app.sel.feed {
            l = l.patch_style(Style::default().bg(theme::SEL_BG));
        }
        hits.push((Rect { x: inner.x, y: inner.y + (i - top) as u16, width: inner.width, height: 1 }, Hit::Feed(i)));
        lines.push(l);
    }
    if events.is_empty() {
        lines.push(Line::from(sp("  no events yet", dim())));
    }
    f.render_widget(Paragraph::new(lines).style(base()), inner);
    app.hits.extend(hits);
}

// ---------------------------------------------------------------------------------------------- ops
fn ops(app: &mut App, f: &mut Frame, area: Rect) {
    let rows = Layout::default().direction(Direction::Vertical).constraints([Constraint::Ratio(1, 3); 3]).split(area);
    let mut cells = vec![];
    for r in rows.iter() {
        let c = Layout::default().direction(ACROSS).constraints([Constraint::Percentage(50); 2]).split(*r);
        cells.extend(c.iter().copied());
    }
    let snap = &app.snap;
    let age = app.age();
    for (i, cell) in cells.iter().enumerate() {
        let focused = app.sel.ops == i;
        let blk = block(OPS_SECTIONS[i], focused);
        let inner = blk.inner(*cell);
        f.render_widget(blk, *cell);
        let cur = app.sel.ops_idx[i];
        let w = inner.width as usize;
        let mark = |idx: usize, line: Line<'static>| -> Line<'static> {
            if focused && idx == cur {
                line.patch_style(Style::default().bg(theme::SEL_BG))
            } else {
                line
            }
        };
        let mut lines: Vec<Line> = vec![];
        match i {
            0 => {
                let r = &snap.runner;
                lines.push(Line::from(vec![
                    sp(" state ", dim()),
                    sp(if r.running { r.state.clone().unwrap_or_default() } else { "stopped".into() }, bold()),
                    sp(format!("   pid {}   host {}", r.pid.map(|p| p.to_string()).unwrap_or("-".into()), r.host.clone().unwrap_or("-".into())), dim()),
                ]));
                lines.push(Line::from(vec![
                    sp(" slots ", dim()),
                    sp(format!("setting {} · effective {} · active {}", r.slots_setting, r.slots_effective, r.active), base()),
                    sp(if r.windowed { "  (window)" } else { "" }, dim()),
                ]));
                lines.push(Line::from(vec![
                    sp(" flags ", dim()),
                    sp(if r.paused { "PAUSED " } else { "" }, fg(theme::WARN)),
                    sp(if r.draining { "DRAINING " } else { "" }, fg(theme::WARN)),
                    sp(if r.stop.is_empty() { String::new() } else { format!("{} requested", r.stop) }, fg(theme::WARN)),
                    sp(if !r.paused && !r.draining && r.stop.is_empty() { "none" } else { "" }, dim()),
                ]));
                let pacer_st = match r.pacer {
                    Some(true) => ("on", theme::GOOD),
                    Some(false) => ("off", theme::WARN),
                    None => ("-", theme::DIM),
                };
                let wt_st = match r.auto_worktree {
                    Some(true) => ("on", theme::GOOD),
                    Some(false) => ("off", theme::WARN),
                    None => ("-", theme::DIM),
                };
                let mut cfg_spans = vec![sp(" pacer ", dim()), sp(pacer_st.0, fg(pacer_st.1).add_modifier(Modifier::BOLD))];
                if let Some(iv) = &r.pace_interval {
                    if let Some(lpt) = r.launch_per_tick.filter(|n| *n > 0) {
                        cfg_spans.push(sp(format!(" ({iv}, {lpt}/tick)"), dim()));
                    } else {
                        cfg_spans.push(sp(format!(" ({iv})"), dim()));
                    }
                }
                cfg_spans.push(sp("   auto_worktree ", dim()));
                cfg_spans.push(sp(wt_st.0, fg(wt_st.1).add_modifier(Modifier::BOLD)));
                lines.push(Line::from(cfg_spans));
                lines.push(Line::from(sp(format!(" ram free {:.1} GiB (stops starting below {:.1})  ready {}", r.free_gib, r.min_free_gib, r.ready), base())));
                if let Some(n) = &r.note {
                    lines.push(Line::from(sp(format!(" note {n}"), dim())));
                }
                lines.push(Line::from(sp(" p pause · d drain · R reload · S stop · g start · +/- slots · P pacer · w worktree", dim())));
            }
            1 => {
                for (idx, b) in snap.boards.iter().enumerate() {
                    let c = |k: &str| b.counts.get(k).copied().unwrap_or(0);
                    let neg = snap.tasks.iter().filter(|t| t.board == b.name && t.is_negative()).count() as i64;
                    let dead_err = (c("dead") - neg).max(0);
                    let neg_txt = if neg > 0 { format!(" neg {neg}") } else { String::new() };
                    let l = Line::from(vec![
                        sp(if b.paused { " ⏸ " } else { " ▶ " }, fg(if b.paused { theme::WARN } else { theme::GOOD })),
                        sp(format!("{:<12}", pad(&b.name, 12)), bold()),
                        sp(
                            format!(
                                "running {} / max {}  open {} done {} dead {dead_err}{neg_txt}",
                                b.running,
                                b.max_slots.map(|m| m.to_string()).unwrap_or("∞".into()),
                                c("open") + c("claimed"),
                                c("done"),
                            ),
                            base(),
                        ),
                        sp(if b.paused { "  PAUSED" } else { "" }, fg(theme::WARN)),
                    ]);
                    lines.push(mark(idx, l));
                }
            }
            2 => {
                for (idx, wch) in snap.watchers.iter().enumerate() {
                    let last = wch.last_fired.map(|t| format!("{} ago", fmt_dur(app.now() - t))).unwrap_or_else(|| "never".into());
                    let l = Line::from(vec![
                        sp(if wch.enabled { " ● " } else { " ○ " }, fg(if wch.enabled { theme::GOOD } else { theme::DIM })),
                        sp(format!("{} ", pad(&wch.name, 14)), bold()),
                        sp(
                            format!(
                                "on {}{}  fired {} · last {} · cooldown {} · tier {}",
                                wch.kinds.join(","),
                                if wch.all_boards { " (all boards)" } else { "" },
                                wch.fired.unwrap_or(0),
                                last,
                                wch.cooldown.clone().unwrap_or("-".into()),
                                wch.tier.clone().unwrap_or("-".into())
                            ),
                            base(),
                        ),
                    ]);
                    lines.push(mark(idx, l));
                }
                if snap.watchers.is_empty() {
                    lines.push(Line::from(sp(" no watchers", dim())));
                }
            }
            3 => {
                for (idx, s) in snap.schedules.iter().enumerate() {
                    let spec = match s.kind.as_str() {
                        "window" => format!(
                            "{}-{}{}",
                            s.spec.get("from").and_then(Value::as_str).unwrap_or("?"),
                            s.spec.get("to").and_then(Value::as_str).unwrap_or("?"),
                            s.spec.get("days").and_then(Value::as_str).map(|d| format!(" {d}")).unwrap_or_default()
                        ),
                        _ => s.spec.to_string(),
                    };
                    let l = Line::from(vec![
                        sp(if s.enabled { " ● " } else { " ○ " }, fg(if s.enabled { theme::GOOD } else { theme::DIM })),
                        sp(format!("{} ", pad(&s.name, 12)), bold()),
                        sp(trunc(&format!("{} {} → {}", s.kind, spec, s.action), w.saturating_sub(24)), base()),
                        sp(if s.active_now { "  ACTIVE" } else { "" }, fg(theme::GOOD)),
                    ]);
                    lines.push(mark(idx, l));
                }
                if snap.schedules.is_empty() {
                    lines.push(Line::from(sp(" no schedules", dim())));
                }
            }
            4 => {
                for (idx, t) in snap.tiers.order.iter().enumerate() {
                    let c = snap.tiers.tiers.get(t).map(|v| v.join(", ")).unwrap_or_default();
                    let l = Line::from(vec![
                        sp(format!(" {} ", idx + 1), dim()),
                        sp(format!("{:<8}", t), fg(theme::tier_color(t)).add_modifier(Modifier::BOLD)),
                        sp(trunc(&c, w.saturating_sub(14)), base()),
                    ]);
                    lines.push(mark(idx, l));
                }
                lines.push(Line::from(sp(
                    format!(" weakest → strongest · fallback {} · default {} · J/K move", snap.tiers.fallback, snap.tiers.default),
                    dim(),
                )));
            }
            _ => {
                for (b, c) in &snap.caps {
                    let kv: Vec<String> = c
                        .as_object()
                        .map(|o| o.iter().map(|(k, v)| format!("{k}={}", v.as_str().map(String::from).unwrap_or_else(|| v.to_string()))).collect())
                        .unwrap_or_default();
                    let hit = snap.usage.iter().find(|u| &u.bucket == b).and_then(|u| u.cap_hit.clone());
                    lines.push(Line::from(vec![sp(format!(" {:<14}", pad(b, 14)), bold()), sp(trunc(&kv.join(" "), w.saturating_sub(18)), base())]));
                    if let Some(h) = hit {
                        lines.push(Line::from(sp(format!("   held: {}", trunc(&h, w.saturating_sub(10))), fg(theme::WARN))));
                    }
                }
                if snap.caps.is_empty() {
                    lines.push(Line::from(sp(" no caps configured (ab caps)", dim())));
                }
                let _ = age;
            }
        }
        f.render_widget(Paragraph::new(lines).style(base()), inner);
    }
}

// ---------------------------------------------------------------------------------------------- panels
fn panels(app: &mut App, f: &mut Frame, area: Rect) {
    if app.cfg.panel.is_empty() {
        let text = vec![
            Line::from(""),
            Line::from(sp("  No custom panels configured.", bold())),
            Line::from(""),
            Line::from(sp("  Add them to tui.toml in the board home (a shell command, refreshed every N seconds):", dim())),
            Line::from(""),
            Line::from(sp("    [[panel]]", fg(theme::GOOD))),
            Line::from(sp("    title = \"Digest\"", fg(theme::GOOD))),
            Line::from(sp("    command = \"cat ~/digest.txt\"", fg(theme::GOOD))),
            Line::from(sp("    refresh = 30", fg(theme::GOOD))),
        ];
        f.render_widget(Paragraph::new(text).style(base()), area);
        return;
    }
    let n = app.cfg.panel.len();
    let cols = if n > 1 { 2 } else { 1 };
    let rows = n.div_ceil(cols);
    let row_areas = Layout::default().direction(Direction::Vertical).constraints(vec![Constraint::Ratio(1, rows as u32); rows]).split(area);
    for (i, p) in app.cfg.panel.iter().enumerate() {
        let r = row_areas[i / cols];
        let c = Layout::default().direction(ACROSS).constraints(vec![Constraint::Ratio(1, cols as u32); cols]).split(r);
        let cell = c[i % cols];
        let blk = block(&format!("{} · every {}s", p.title, p.refresh), false);
        let inner = blk.inner(cell);
        f.render_widget(blk, cell);
        let text = app.panels.get(i).cloned().unwrap_or_default();
        let lines: Vec<Line> =
            text.lines().take(inner.height as usize).map(|l| Line::from(sp(format!(" {}", trunc(l, inner.width as usize - 2)), base()))).collect();
        f.render_widget(Paragraph::new(lines).style(base()), inner);
    }
}

// ---------------------------------------------------------------------------------------------- overlays
fn centered(area: Rect, w: u16, h: u16) -> Rect {
    let w = w.min(area.width);
    let h = h.min(area.height);
    Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h }
}

fn overlays(app: &mut App, f: &mut Frame, area: Rect) {
    match &app.mode {
        Mode::Help => {
            let text = [
                ("Tab / Shift-Tab, 1-8", "switch screen (Board, Running, Card, Decisions, Usage, Feed, Ops, Panels)"),
                ("↑↓ ←→  or  j k h l", "move; g / G first / last; PgUp PgDn"),
                ("Enter", "open the selected card"),
                ("/  Esc", "filter the lists (words are ANDed); Esc clears"),
                (":", "command palette: pause resume drain reload slots N board NAME fx off ab VERB ..."),
                ("b", "cycle the board filter (all, then each board)"),
                ("r  x  K", "retry / cancel (asks first) / kill the live run (asks first)"),
                ("n  P  T  H  D", "note / priority / tier / harness / defer the selected card"),
                ("a", "answer a decision card (note + done)"),
                ("y / drag", "Decisions: copy the selection or the whole card"),
                ("e  s", "open the card's workdir in $EDITOR / a shell (returns here after)"),
                ("Ops screen", "p pause·resume  d drain  R reload  S stop  g start  +/- slots  P pacer  w worktree  J/K tiers"),
                ("F5", "refresh now"),
                ("?  q  Ctrl-C", "this help / quit / quit"),
            ];
            let rect = centered(area, 100, text.len() as u16 + 4);
            f.render_widget(Clear, rect);
            let lines: Vec<Line> =
                text.iter().map(|(k, v)| Line::from(vec![sp(format!(" {k:<24}"), fg(theme::ACCENT).add_modifier(Modifier::BOLD)), sp(*v, base())])).collect();
            f.render_widget(Paragraph::new(lines).style(base()).block(block("keys · ? or Esc closes", true)), rect);
        }
        Mode::Prompt(p) => {
            let rect = centered(area, 78, 6);
            f.render_widget(Clear, rect);
            let lines = vec![
                Line::from(vec![sp(" › ", fg(theme::ACCENT)), sp(p.buf.clone(), base()), sp("▌", fg(theme::ACCENT))]),
                Line::from(""),
                Line::from(sp(format!(" {}", p.hint), dim())),
            ];
            f.render_widget(Paragraph::new(lines).style(base()).block(block(&p.title, true)), rect);
        }
        Mode::Confirm(c) => {
            let rect = centered(area, 80, 6);
            f.render_widget(Clear, rect);
            let lines = vec![
                Line::from(sp(format!(" {}", c.text), bold())),
                Line::from(""),
                Line::from(vec![
                    sp(" y ", fg(theme::GOOD).add_modifier(Modifier::BOLD)),
                    sp("confirm      ", base()),
                    sp("any other key ", fg(theme::BAD)),
                    sp("cancels", base()),
                ]),
            ];
            f.render_widget(Paragraph::new(lines).style(base()).wrap(Wrap { trim: false }).block(block("confirm", true)), rect);
        }
        _ => {}
    }
}
