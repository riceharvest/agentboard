use crate::theme;
use ratatui::style::{Color, Style};
use ratatui::text::Span;
use unicode_width::UnicodeWidthStr;

fn width(s: &str) -> usize {
    UnicodeWidthStr::width(s)
}

pub fn text_width(s: &str) -> usize {
    width(s)
}

/// Cut to at most `w` columns, with an ellipsis if something was dropped.
pub fn trunc(s: &str, w: usize) -> String {
    if w == 0 {
        return String::new();
    }
    let s = s.replace(['\n', '\r', '\t'], " ");
    if width(&s) <= w {
        return s;
    }
    let mut out = String::new();
    let mut used = 0;
    for ch in s.chars() {
        let cw = width(ch.encode_utf8(&mut [0; 4]));
        if used + cw + 1 > w {
            break;
        }
        out.push(ch);
        used += cw;
    }
    out.push('…');
    out
}

pub fn pad(s: &str, w: usize) -> String {
    let t = trunc(s, w);
    let n = w.saturating_sub(width(&t));
    format!("{t}{}", " ".repeat(n))
}

pub fn fmt_dur(s: f64) -> String {
    let s = s.max(0.0) as u64;
    match s {
        0..=59 => format!("{s}s"),
        60..=3599 => format!("{}m{:02}s", s / 60, s % 60),
        3600..=86399 => format!("{}h{:02}m", s / 3600, (s % 3600) / 60),
        _ => format!("{}d{:02}h", s / 86400, (s % 86400) / 3600),
    }
}

/// mm:ss style countdown for cooldowns.
pub fn fmt_clock(s: f64) -> String {
    let s = s.max(0.0) as u64;
    if s >= 3600 {
        format!("{}:{:02}:{:02}", s / 3600, (s % 3600) / 60, s % 60)
    } else {
        format!("{:02}:{:02}", s / 60, s % 60)
    }
}

pub fn fmt_num(n: i64) -> String {
    let f = n as f64;
    if n >= 1_000_000 {
        format!("{:.2}M", f / 1e6)
    } else if n >= 1_000 {
        format!("{:.1}k", f / 1e3)
    } else {
        n.to_string()
    }
}

/// HH:MM:SS (UTC) of an epoch time: no timezone database needed, and the feed is for ordering, not for clocks.
pub fn fmt_hms(ts: f64) -> String {
    let t = ts as i64;
    format!("{:02}:{:02}:{:02}", (t / 3600) % 24, (t / 60) % 60, t % 60)
}

/// `▕████░░░░▏` style gauge. `marker` (0..1) draws a tick, e.g. the weekly pace.
pub fn bar(width: usize, frac: f64, marker: Option<f64>, color: Color) -> Vec<Span<'static>> {
    let width = width.max(2);
    let frac = frac.clamp(0.0, 1.0);
    let filled = frac * width as f64;
    let full = filled.floor() as usize;
    let part = ((filled - full as f64) * 8.0) as usize;
    let mark = marker.map(|m| ((m.clamp(0.0, 1.0) * width as f64) as usize).min(width - 1));
    let eighths = ['▏', '▎', '▍', '▌', '▋', '▊', '▉'];
    let mut spans = vec![Span::styled("▕", theme::dim())];
    for i in 0..width {
        let (ch, style) = if i < full {
            ('█', Style::default().fg(color).bg(theme::BG))
        } else if i == full && part > 0 {
            (eighths[part - 1], Style::default().fg(color).bg(theme::BG))
        } else {
            ('░', Style::default().fg(Color::Rgb(60, 68, 86)).bg(theme::BG))
        };
        if mark == Some(i) {
            spans.push(Span::styled("┃", Style::default().fg(Color::White).bg(theme::BG)));
        } else {
            spans.push(Span::styled(ch.to_string(), style));
        }
    }
    spans.push(Span::styled("▏", theme::dim()));
    spans
}

pub fn gauge_color(frac: f64) -> Color {
    if frac >= 0.9 {
        theme::BAD
    } else if frac >= 0.7 {
        theme::WARN
    } else {
        theme::GOOD
    }
}

/// Very small markdown: headings, bullets, **bold**, `code`, fenced blocks, quotes.
pub fn md_lines(body: &str, width: usize) -> Vec<ratatui::text::Line<'static>> {
    use ratatui::style::Modifier;
    use ratatui::text::Line;
    let mut out = vec![];
    let mut fenced = false;
    for raw in body.lines() {
        if raw.trim_start().starts_with("```") {
            fenced = !fenced;
            continue;
        }
        if fenced {
            out.push(Line::from(Span::styled(
                format!("  {}", trunc(raw, width.saturating_sub(2))),
                Style::default().fg(Color::Rgb(170, 200, 160)).bg(theme::BG),
            )));
            continue;
        }
        let t = raw.trim_end();
        if let Some(h) = t.trim_start().strip_prefix('#') {
            let h = h.trim_start_matches('#').trim();
            out.push(Line::from(Span::styled(h.to_string(), theme::fg(theme::ACCENT).add_modifier(Modifier::BOLD))));
            continue;
        }
        let (prefix, rest) = if let Some(r) = t.trim_start().strip_prefix("- ").or_else(|| t.trim_start().strip_prefix("* ")) {
            ("  • ", r)
        } else if let Some(r) = t.strip_prefix("> ") {
            ("  ▎ ", r)
        } else {
            ("", t)
        };
        let mut spans = vec![Span::styled(prefix.to_string(), theme::fg(theme::ACCENT))];
        spans.extend(inline_md(rest));
        out.push(Line::from(spans));
    }
    out
}

fn inline_md(s: &str) -> Vec<Span<'static>> {
    use ratatui::style::Modifier;
    let mut spans = vec![];
    let mut buf = String::new();
    let (mut bold, mut code) = (false, false);
    let chars: Vec<char> = s.chars().collect();
    let mut i = 0;
    let flush = |buf: &mut String, spans: &mut Vec<Span<'static>>, bold: bool, code: bool| {
        if buf.is_empty() {
            return;
        }
        let st = if code {
            Style::default().fg(Color::Rgb(170, 200, 160)).bg(theme::BG)
        } else if bold {
            theme::base().add_modifier(Modifier::BOLD)
        } else {
            theme::base()
        };
        spans.push(Span::styled(std::mem::take(buf), st));
    };
    while i < chars.len() {
        if !code && chars[i] == '*' && chars.get(i + 1) == Some(&'*') {
            flush(&mut buf, &mut spans, bold, code);
            bold = !bold;
            i += 2;
        } else if chars[i] == '`' {
            flush(&mut buf, &mut spans, bold, code);
            code = !code;
            i += 1;
        } else {
            buf.push(chars[i]);
            i += 1;
        }
    }
    flush(&mut buf, &mut spans, bold, code);
    spans
}

/// Greedy word wrap to `width` columns.
pub fn wrap(s: &str, width: usize) -> Vec<String> {
    let width = width.max(8);
    let mut out = vec![];
    for line in s.lines() {
        let mut cur = String::new();
        for word in line.split(' ') {
            if width_of(&cur) + width_of(word) + usize::from(!cur.is_empty()) > width && !cur.is_empty() {
                out.push(std::mem::take(&mut cur));
            }
            if !cur.is_empty() {
                cur.push(' ');
            }
            cur.push_str(word);
            while width_of(&cur) > width {
                let head: String = cur.chars().take(width).collect();
                let rest: String = cur.chars().skip(width).collect();
                out.push(head);
                cur = rest;
            }
        }
        out.push(cur);
    }
    out
}

fn width_of(s: &str) -> usize {
    width(s)
}
