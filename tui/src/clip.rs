//! Copy text to the system clipboard from inside the TUI (mouse capture keeps the terminal's own selection away).
//! Tries the desktop clipboard tools first, and always also sends OSC 52 so terminals that support it (and SSH
//! sessions) get the text too.
use std::io::Write;
use std::process::{Command, Stdio};

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn base64(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let n = (u32::from(c[0]) << 16) | (u32::from(*c.get(1).unwrap_or(&0)) << 8) | u32::from(*c.get(2).unwrap_or(&0));
        for i in 0..4 {
            if i <= c.len() {
                out.push(B64[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

fn pipe_to(cmd: &str, args: &[&str], text: &str) -> bool {
    let Ok(mut child) = Command::new(cmd).args(args).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null()).spawn() else {
        return false;
    };
    if let Some(mut stdin) = child.stdin.take() {
        if stdin.write_all(text.as_bytes()).is_err() {
            let _ = child.kill();
            return false;
        }
    }
    child.wait().map(|s| s.success()).unwrap_or(false)
}

/// Copy `text`; returns how it was delivered (for the toast).
pub fn copy(text: &str) -> &'static str {
    // OSC 52 first: harmless where unsupported, and the only route over SSH
    let mut out = std::io::stdout();
    let _ = write!(out, "\x1b]52;c;{}\x07", base64(text.as_bytes()));
    let _ = out.flush();
    let wayland = std::env::var_os("WAYLAND_DISPLAY").is_some();
    let x11 = std::env::var_os("DISPLAY").is_some();
    if wayland && pipe_to("wl-copy", &[], text) {
        return "wl-copy";
    }
    if x11 && pipe_to("xclip", &["-selection", "clipboard"], text) {
        return "xclip";
    }
    if x11 && pipe_to("xsel", &["--clipboard", "--input"], text) {
        return "xsel";
    }
    if cfg!(target_os = "macos") && pipe_to("pbcopy", &[], text) {
        return "pbcopy";
    }
    "OSC 52"
}

#[cfg(test)]
mod tests {
    use super::base64;

    #[test]
    fn base64_matches_rfc4648_vectors() {
        for (i, o) in [("", ""), ("f", "Zg=="), ("fo", "Zm8="), ("foo", "Zm9v"), ("foob", "Zm9vYg=="), ("fooba", "Zm9vYmE="), ("foobar", "Zm9vYmFy")] {
            assert_eq!(base64(i.as_bytes()), o);
        }
    }
}
