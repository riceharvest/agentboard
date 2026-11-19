//! Serde mirrors of `ab snapshot --json` (schema: agentboard/snapshot.schema.json) and `ab show --json`.
//! Everything is tolerant: missing keys default, unknown keys are ignored (the schema promises both).
use serde::Deserialize;
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Snapshot {
    pub schema: i64,
    pub generated_at: f64,
    pub board: Option<String>,
    pub actor: String,
    pub db: String,
    pub boards: Vec<Board>,
    pub tasks: Vec<Task>,
    pub tasks_open: i64,
    pub tasks_closed_shown: i64,
    pub runner: Runner,
    pub limits: Vec<Limit>,
    pub caps: BTreeMap<String, Value>,
    pub plan: Plan,
    pub usage: Vec<Bucket>,
    pub usage_hourly: BTreeMap<String, Vec<i64>>,
    pub usage_split: Vec<SplitRow>,
    pub watchers: Vec<Watcher>,
    pub schedules: Vec<Schedule>,
    pub active_windows: Vec<String>,
    pub tiers: Tiers,
    pub events: Vec<Event>,
    pub events_cursor: i64,
    pub events_truncated: bool,
    pub tail: Option<Tail>,
    pub took_ms: f64,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Board {
    pub name: String,
    pub description: String,
    pub paused: bool,
    pub max_slots: Option<i64>,
    pub default_tier: Option<String>,
    pub default_workdir: Option<String>,
    pub counts: BTreeMap<String, i64>,
    pub states: BTreeMap<String, i64>,
    pub running: i64,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Task {
    pub id: String,
    pub board: String,
    pub title: String,
    pub channel: String,
    pub status: String,
    pub state: String,
    pub runnable: bool,
    pub priority: i64,
    pub tier: String,
    pub harness: Option<String>,
    pub model: Option<String>,
    pub labels: BTreeMap<String, String>,
    pub deps: Vec<String>,
    pub deps_open: Vec<String>,
    pub workdir: Option<String>,
    pub attempts: i64,
    pub max_attempts: i64,
    pub timeout_s: Option<f64>,
    pub owner: Option<String>,
    pub lease_until: Option<f64>,
    pub not_before: Option<f64>,
    pub run_id: Option<i64>,
    pub created_by: Option<String>,
    pub created_at: Option<f64>,
    pub updated_at: Option<f64>,
    pub result_head: Option<String>,
    pub wait_reason: Option<String>,
    pub park: Option<Park>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Park {
    pub pid: Option<i64>,
    pub file: Option<String>,
    pub grace_s: Option<f64>,
    pub note: Option<String>,
    pub run: Option<i64>,
    pub at: Option<f64>,
    pub woken_at: Option<f64>,
    pub woke: Option<String>,
}

impl Park {
    pub fn active(&self) -> bool {
        self.woken_at.is_none()
    }

    pub fn what(&self) -> String {
        if let Some(pid) = self.pid {
            format!("pid {pid}")
        } else if let Some(f) = &self.file {
            format!("file {}", f.rsplit('/').next().unwrap_or(f))
        } else {
            "a job".into()
        }
    }
}

impl Task {
    pub fn kind(&self) -> &str {
        self.labels.get("type").map(String::as_str).unwrap_or("")
    }

    pub fn is_negative(&self) -> bool {
        self.status == "dead" && self.labels.get("outcome").map(String::as_str) == Some("negative")
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Child {
    pub run: i64,
    pub task_id: String,
    pub harness: String,
    pub endpoint: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub bucket: Option<String>,
    pub pid: Option<i64>,
    pub started_at: f64,
    pub elapsed_s: f64,
    pub timeout_s: Option<f64>,
    pub title: Option<String>,
    pub board: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Runner {
    pub running: bool,
    pub pid: Option<i64>,
    pub host: Option<String>,
    pub started_at: Option<f64>,
    pub state: Option<String>,
    pub active: i64,
    pub slots: Option<i64>,
    pub slots_setting: i64,
    pub slots_effective: i64,
    pub paused: bool,
    pub draining: bool,
    pub stop: String,
    pub heartbeat_age_s: Option<f64>,
    pub note: Option<String>,
    pub free_gib: f64,
    pub min_free_gib: f64,
    pub ready: i64,
    pub launch_per_tick: Option<i64>,
    pub pace_interval: Option<String>,
    pub pacer: Option<bool>,
    pub auto_worktree: Option<bool>,
    pub windowed: bool,
    pub children: Vec<Child>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Limit {
    pub scope: String,
    pub kind: Option<String>,
    pub until: f64,
    pub reason: Option<String>,
    pub remaining_s: f64,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct PlanPct {
    pub five_hour: f64,
    pub seven_day: f64,
    pub five_hour_resets: Option<String>,
    pub seven_day_resets: Option<String>,
    pub age_s: f64,
    pub stale: bool,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Guard {
    pub bucket: String,
    pub reason: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Plan {
    pub claude_plan_pct: Option<PlanPct>,
    pub weekly_pace_pct: Option<f64>,
    pub guards: Vec<Guard>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Window {
    pub calls: i64,
    pub output: i64,
    pub fresh_in: i64,
    pub cost_usd: f64,
    pub runs: i64,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct BucketLimit {
    pub kind: Option<String>,
    pub until: f64,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct PacerInfo {
    pub budget: i64,
    pub why: String,
    pub interval: String,
    pub interval_s: f64,
    pub used_in_interval: i64,
    pub remaining: i64,
    pub detail: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Bucket {
    pub bucket: String,
    #[serde(rename = "5h")]
    pub h5: Window,
    #[serde(rename = "24h")]
    pub h24: Window,
    #[serde(rename = "7d")]
    pub d7: Window,
    pub limit: Option<BucketLimit>,
    pub cap: Option<Value>,
    pub cap_hit: Option<String>,
    pub launch_budget: Option<i64>,
    pub budget_why: Option<String>,
    pub pacer: Option<PacerInfo>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct SplitRow {
    pub harness: Option<String>,
    pub model: Option<String>,
    pub bucket: Option<String>,
    pub calls: i64,
    pub output: i64,
    pub fresh_in: i64,
    pub cost_usd: Option<f64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Watcher {
    pub id: i64,
    pub board: String,
    pub name: String,
    pub enabled: bool,
    pub kinds: Vec<String>,
    pub all_boards: bool,
    pub fired: Option<i64>,
    pub last_fired: Option<f64>,
    pub cooldown: Option<String>,
    pub tier: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Schedule {
    pub name: String,
    pub kind: String,
    pub spec: Value,
    pub action: Value,
    pub enabled: bool,
    pub last_fired: Option<f64>,
    pub active_now: bool,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Tiers {
    pub order: Vec<String>,
    pub tiers: BTreeMap<String, Vec<String>>,
    pub fallback: String,
    pub default: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Event {
    pub id: i64,
    pub ts: f64,
    pub board: Option<String>,
    pub channel: Option<String>,
    pub task_id: Option<String>,
    pub actor: Option<String>,
    pub kind: String,
    pub payload: Value,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Tail {
    pub run: i64,
    pub task_id: Option<String>,
    pub harness: Option<String>,
    pub running: bool,
    pub bytes: i64,
    pub out: String,
    pub out_size: i64,
    pub out_truncated: bool,
    pub err: String,
    pub error: Option<String>,
}

// ---- `ab show ID --json`
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Note {
    pub id: i64,
    pub author: String,
    pub body: String,
    pub ts: f64,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct RunRow {
    pub id: i64,
    pub harness: Option<String>,
    pub model: Option<String>,
    pub bucket: Option<String>,
    pub started_at: Option<f64>,
    pub ended_at: Option<f64>,
    pub outcome: Option<String>,
    pub exit_code: Option<i64>,
    pub input: Option<i64>,
    pub output: Option<i64>,
    pub cache_read: Option<i64>,
    pub cost_usd: Option<f64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Detail {
    pub id: String,
    pub title: String,
    pub body: String,
    pub status: String,
    pub state: String,
    pub result: Option<String>,
    pub deps: Vec<Value>,
    pub children: Vec<Value>,
    pub notes: Vec<Note>,
    pub runs: Vec<RunRow>,
    pub park: Option<Park>,
}

/// An id out of a value that is either "t_x" or {"id": "t_x", ...}.
pub fn value_id(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Object(o) => o.get("id").and_then(Value::as_str).unwrap_or("?").to_string(),
        other => other.to_string(),
    }
}
