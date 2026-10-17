//! Signing the local install in with a one-use code (the Mac app spec,
//! "Logging in"): the app runs `istota web login-code` through the Mac CLI,
//! then loads `http://localhost:<port>/istota/auth/local#<code>`. The code is
//! in the fragment, so it never reaches nginx's log, and it lasts 60 seconds.

use crate::paths::{env_file_value, Paths};

/// The port the Mac CLI uses when its state file names none.
pub const DEFAULT_PORT: u16 = 8080;

pub const LOGIN_CODE: &[&str] = &["web", "login-code"];

/// The port the Mac CLI recorded for the local VM.
pub fn local_port(paths: &Paths) -> u16 {
    std::fs::read_to_string(paths.cli_state_file())
        .ok()
        .and_then(|text| env_file_value(&text, "PORT"))
        .and_then(|port| port.parse().ok())
        .unwrap_or(DEFAULT_PORT)
}

/// The workspace folder the Mac CLI recorded, if any.
pub fn local_workspace(paths: &Paths) -> Option<std::path::PathBuf> {
    std::fs::read_to_string(paths.cli_state_file())
        .ok()
        .and_then(|text| env_file_value(&text, "WORKSPACE"))
        .map(std::path::PathBuf::from)
}

/// The code `istota web login-code` printed: its last line, which must be a
/// URL-safe token (`secrets.token_urlsafe(32)` gives 43 characters).
pub fn parse_code(stdout: &str) -> Result<String, String> {
    let code = stdout.lines().rev().map(str::trim).find(|l| !l.is_empty()).unwrap_or("");
    let safe = code.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if safe && (32..=128).contains(&code.len()) {
        Ok(code.to_string())
    } else {
        Err("istota web login-code printed no code".to_string())
    }
}

pub fn login_url(port: u16, code: &str) -> String {
    format!("http://localhost:{port}/istota/auth/local#{code}")
}

/// The one web identity whose sessions "Sign out everywhere" ends, from
/// `istota auth list`'s lines (`<user> email=… … live`). The local install has
/// one; several, or none, is refused rather than guessed.
pub fn only_live_identity(auth_list: &str) -> Result<String, String> {
    let live: Vec<&str> = auth_list
        .lines()
        .filter(|line| line.split_whitespace().last() == Some("live"))
        .filter_map(|line| line.split_whitespace().next())
        .collect();
    match live.as_slice() {
        [one] => Ok(one.to_string()),
        [] => Err("no web identity to sign out".to_string()),
        _ => Err("more than one web identity; sign them out with `istota auth logout-all <user>`".to_string()),
    }
}

/// How many tasks `istota list --status running` printed (`[<id>] …` lines).
pub fn running_tasks(list: &str) -> usize {
    list.lines().filter(|line| line.starts_with('[')).count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_code_is_the_last_line_and_url_safe() {
        let code = "Ab3_-".repeat(9);
        assert_eq!(parse_code(&format!("{code}\n")).unwrap(), code);
        assert_eq!(parse_code(&format!("warning: something\n{code}\n\n")).unwrap(), code);
        assert!(parse_code("").is_err());
        assert!(parse_code("Error: [web] auth does not include \"local_code\"").is_err());
        assert!(parse_code(&format!("{code}#x")).is_err());
        assert!(parse_code("short").is_err());
    }

    #[test]
    fn the_code_goes_in_the_fragment() {
        assert_eq!(login_url(8181, "abc"), "http://localhost:8181/istota/auth/local#abc");
    }

    #[test]
    fn the_port_comes_from_the_clis_state_file() {
        let dir = std::env::temp_dir().join(format!("istota-login-{}", std::process::id()));
        let paths = Paths::new(dir.clone(), dir.join("Resources"));
        assert_eq!(local_port(&paths), DEFAULT_PORT);
        std::fs::create_dir_all(paths.cli_state_file().parent().unwrap()).unwrap();
        std::fs::write(paths.cli_state_file(), "WORKSPACE=/Users/a/Istota\nPORT=8181\n").unwrap();
        assert_eq!(local_port(&paths), 8181);
        assert_eq!(local_workspace(&paths), Some("/Users/a/Istota".into()));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn sign_out_everywhere_needs_exactly_one_live_identity() {
        let one = "alice email=a@example.org password_set=True disabled=False last_login=- live\nstate=unchanged\n";
        assert_eq!(only_live_identity(one).unwrap(), "alice");
        let orphan = "bob email=b@example.org password_set=False disabled=False last_login=- orphaned\n";
        assert!(only_live_identity(&format!("{one}{orphan}")).is_ok());
        let two = format!("{one}{}", one.replace("alice", "carol"));
        assert!(only_live_identity(&two).is_err());
        assert!(only_live_identity("state=unchanged\n").is_err());
    }

    #[test]
    fn running_tasks_are_counted_from_the_list() {
        assert_eq!(running_tasks("No tasks found\n"), 0);
        assert_eq!(running_tasks("[12] running              alice           hi\n[13] running x\n"), 2);
    }
}
