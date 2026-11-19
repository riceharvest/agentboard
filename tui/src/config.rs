//! `tui.toml` in the board home (`$AB_HOME`, or --config FILE). Nothing in here is required.
//!
//! ```toml
//! poll_ms = 1000          # snapshot interval
//! fx = true               # tachyonfx effects (also --no-fx)
//! bell = true             # terminal bell on decision and dead cards
//! owner = "me"            # whose decision cards to show (default: the actor `ab` runs as)
//! ram_command = "..."     # first lines of its output replace the built-in /proc/meminfo reading
//! gpu_command = "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader"
//! gpu_refresh = 5
//! [[panel]]
//! title = "Digest"
//! command = "cat ~/digest.txt"
//! refresh = 30
//! ```
use serde::Deserialize;
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Panel {
    pub title: String,
    pub command: String,
    pub refresh: u64,
}

impl Default for Panel {
    fn default() -> Self {
        Panel { title: "panel".into(), command: String::new(), refresh: 10 }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Config {
    pub poll_ms: u64,
    pub fx: bool,
    pub bell: bool,
    pub owner: Option<String>,
    pub closed: u32,
    pub tail_bytes: u32,
    pub ram_command: Option<String>,
    pub gpu_command: Option<String>,
    pub gpu_refresh: u64,
    pub notify_command: Option<String>,
    pub panel: Vec<Panel>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            poll_ms: 1000,
            fx: true,
            bell: true,
            owner: None,
            closed: 40,
            tail_bytes: 6000,
            ram_command: None,
            gpu_command: None,
            gpu_refresh: 5,
            notify_command: None,
            panel: vec![],
        }
    }
}

impl Config {
    pub fn load(path: &Path) -> Result<Config, String> {
        match std::fs::read_to_string(path) {
            Ok(text) => toml::from_str(&text).map_err(|e| format!("{}: {e}", path.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Config::default()),
            Err(e) => Err(format!("{}: {e}", path.display())),
        }
    }
}
