use ratatui::style::{Color, Modifier, Style};

pub const BG: Color = Color::Rgb(13, 16, 23);
pub const PANEL: Color = Color::Rgb(22, 27, 38);
pub const FG: Color = Color::Rgb(222, 227, 236);
pub const DIM: Color = Color::Rgb(128, 138, 156);
pub const ACCENT: Color = Color::Rgb(90, 190, 255);
pub const GOOD: Color = Color::Rgb(90, 220, 140);
pub const WARN: Color = Color::Rgb(255, 196, 70);
pub const BAD: Color = Color::Rgb(255, 98, 98);
pub const SEL_BG: Color = Color::Rgb(44, 56, 84);

pub fn base() -> Style {
    Style::default().fg(FG).bg(BG)
}
pub fn dim() -> Style {
    Style::default().fg(DIM).bg(BG)
}
pub fn bold() -> Style {
    base().add_modifier(Modifier::BOLD)
}
pub fn sel() -> Style {
    Style::default().fg(Color::White).bg(SEL_BG).add_modifier(Modifier::BOLD)
}
pub fn fg(c: Color) -> Style {
    Style::default().fg(c).bg(BG)
}

/// Colour by the `type` label.
pub fn kind_color(kind: &str) -> Color {
    match kind {
        "dive" => Color::Rgb(100, 160, 255),
        "experiment" => Color::Rgb(200, 130, 255),
        "review" => Color::Rgb(255, 210, 90),
        "build" => Color::Rgb(100, 220, 140),
        "release" => Color::Rgb(80, 220, 220),
        "decision" => Color::Rgb(255, 150, 70),
        "watch" => Color::Rgb(150, 160, 175),
        _ => FG,
    }
}

pub fn tier_color(tier: &str) -> Color {
    match tier {
        "low" => Color::Rgb(140, 150, 165),
        "medium" => Color::Rgb(220, 225, 235),
        "high" => Color::Rgb(255, 205, 90),
        "max" => Color::Rgb(255, 110, 150),
        _ => FG,
    }
}

pub fn tier_tag(tier: &str) -> &'static str {
    match tier {
        "low" => "L",
        "medium" => "M",
        "high" => "H",
        "max" => "X",
        _ => "?",
    }
}

pub fn event_color(kind: &str) -> Color {
    match kind {
        "created" => Color::Rgb(100, 200, 255),
        "claimed" => Color::Rgb(120, 150, 255),
        "done" => GOOD,
        "dead" => BAD,
        "failed" | "failure" => Color::Rgb(255, 140, 80),
        "watch_fired" => Color::Rgb(220, 140, 255),
        "runner_reload" | "runner_start" | "runner_stop" => WARN,
        "cancelled" => Color::Rgb(200, 120, 120),
        _ => DIM,
    }
}
