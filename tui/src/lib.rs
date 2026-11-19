//! ab-tui: a live dashboard and control panel for agentboard.
//! Reads `ab snapshot --json`, acts through `ab` verbs; holds no board logic of its own.
pub mod ab;
pub mod app;
pub mod config;
pub mod fx;
pub mod model;
pub mod theme;
pub mod ui;
pub mod util;
pub mod workers;
