//! tachyonfx effects. They are post-render cell shaders: `process` runs after the widgets drew, on the same buffer,
//! driven by real elapsed time. Unique keys make a new effect replace the old one with the same key, so nothing piles up.
use crate::theme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;
use std::time::Duration;
use tachyonfx::{fx, Effect, EffectManager, EffectTimer, Interpolation, Motion};

pub struct Fx {
    mgr: EffectManager<String>,
    pub enabled: bool,
    pulse_area: Option<Rect>,
}

fn ms(n: u32, i: Interpolation) -> EffectTimer {
    EffectTimer::from_ms(n, i)
}

impl Fx {
    pub fn new(enabled: bool) -> Fx {
        Fx { mgr: EffectManager::default(), enabled, pulse_area: None }
    }

    pub fn running(&self) -> bool {
        self.enabled && self.mgr.is_running()
    }

    fn add(&mut self, key: &str, effect: Effect) {
        if self.enabled {
            self.mgr.add_unique_effect(key.to_string(), effect);
        }
    }

    pub fn disable(&mut self) {
        self.enabled = false;
        self.pulse_area = None;
        self.mgr = EffectManager::default();
    }

    pub fn startup(&mut self, area: Rect) {
        self.add("startup", fx::sweep_in(Motion::LeftToRight, 28, 10, theme::BG, ms(750, Interpolation::QuadOut)).with_area(area));
    }

    /// Smooth switch between screens: the new content slides in from the left.
    pub fn transition(&mut self, area: Rect) {
        self.add("transition", fx::slide_in(Motion::LeftToRight, 12, 0, theme::BG, ms(220, Interpolation::QuadOut)).with_area(area));
    }

    /// New feed lines fade up out of the background.
    pub fn feed_in(&mut self, area: Rect) {
        self.add("feed", fx::fade_from(theme::BG, theme::BG, ms(500, Interpolation::QuadOut)).with_area(area));
    }

    /// A card finished: short green glow on its row.
    pub fn flash_done(&mut self, id: &str, area: Rect) {
        let e = fx::fade_from(Color::Rgb(60, 235, 150), Color::Rgb(14, 70, 44), ms(900, Interpolation::QuadOut)).with_area(area);
        self.add(&format!("done:{id}"), e);
    }

    /// A card died: red flash while the text coalesces back out of noise.
    pub fn glitch_dead(&mut self, id: &str, area: Rect) {
        let e = fx::parallel(&[
            fx::fade_from(Color::Rgb(255, 70, 70), Color::Rgb(100, 12, 12), ms(1100, Interpolation::QuadOut)),
            fx::coalesce(ms(700, Interpolation::QuadOut)),
        ])
        .with_area(area);
        self.add(&format!("dead:{id}"), e);
    }

    /// Something waits for the human: the Decisions tab breathes until it is answered.
    pub fn pulse(&mut self, area: Rect) {
        if self.pulse_area == Some(area) {
            return;
        }
        self.pulse_area = Some(area);
        let e = fx::repeating(fx::ping_pong(fx::fade_to(Color::Rgb(255, 214, 90), Color::Rgb(92, 62, 0), ms(850, Interpolation::SineInOut)))).with_area(area);
        self.add("pulse", e);
    }

    pub fn stop_pulse(&mut self) {
        if self.pulse_area.take().is_some() {
            self.mgr.cancel_unique_effect("pulse".to_string());
        }
    }

    pub fn process(&mut self, dt: Duration, buf: &mut Buffer, area: Rect) {
        if self.enabled {
            self.mgr.process_effects(dt.into(), buf, area);
        }
    }
}
