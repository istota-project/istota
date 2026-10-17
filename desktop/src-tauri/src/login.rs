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

/// The web UI with no code, for an install whose config takes none.
pub fn plain_url(port: u16) -> String {
    format!("http://localhost:{port}/istota/")
}

/// Whether `istota web login-code` refused because the install's `[web]
/// auth` has no `local_code`: one moved in from the command line, or
/// imported, keeps the sign-in it had.
pub fn codes_off(tail: &[String]) -> bool {
    tail.iter().any(|line| line.contains("does not include \"local_code\""))
}

pub const CODES_OFF_NOTICE: &str =
    "This install signs in with its own password; login codes are off in its settings.";

/// Where Open Istota goes, and why it is not signed in when it is not.
#[derive(Debug, PartialEq, Eq)]
pub struct Opening {
    pub url: String,
    pub notice: Option<&'static str>,
    /// For the app's log: why no code was used.
    pub reason: Option<String>,
}

/// The login page stands in for any code `istota web login-code` did not
/// give: codes off, several users, no web identity, or a failed call. Only
/// codes off is worth a notice; the page itself says the rest.
pub fn opening(port: u16, minted: Result<&str, (&str, &[String])>) -> Opening {
    let plain = |notice, reason: String| Opening { url: plain_url(port), notice, reason: Some(reason) };
    match minted {
        Ok(stdout) => match parse_code(stdout) {
            Ok(code) => Opening { url: login_url(port, &code), notice: None, reason: None },
            Err(err) => plain(None, err),
        },
        Err((_, tail)) if codes_off(tail) => plain(Some(CODES_OFF_NOTICE), "login codes are off".to_string()),
        Err((message, tail)) => plain(None, format!("{message} {}", tail.join(" ")).trim().to_string()),
    }
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
        assert_eq!(plain_url(8181), "http://localhost:8181/istota/");
    }

    #[test]
    fn an_install_without_login_codes_is_told_apart_from_a_failure() {
        let refused = vec!["Error: [web] auth does not include \"local_code\"; this install takes no login codes".to_string()];
        assert!(codes_off(&refused));
        assert!(!codes_off(&["Error: database is locked".to_string()]));
    }

    #[test]
    fn any_refused_code_opens_the_login_page_with_the_reason_kept() {
        let code = "Ab3_-".repeat(9);
        let signed_in = opening(8181, Ok(&format!("{code}\n")));
        assert_eq!(signed_in.url, login_url(8181, &code));
        assert_eq!((signed_in.notice, signed_in.reason), (None, None));

        let off = vec!["Error: [web] auth does not include \"local_code\"; this install takes no login codes".to_string()];
        let opened = opening(8181, Err(("istota exited 1", &off)));
        assert_eq!(opened.url, plain_url(8181));
        assert_eq!(opened.notice, Some(CODES_OFF_NOTICE));

        for tail in [
            "Error: name the user with --user (users with a profile: alice, bob)",
            "Error: alice has no web identity to sign in as; add one with `istota auth add`",
            "Error: Auth database operation failed",
        ] {
            let lines = vec![tail.to_string()];
            let opened = opening(8181, Err(("istota exited 1", &lines)));
            assert_eq!(opened.url, plain_url(8181), "{tail}");
            assert_eq!(opened.notice, None);
            assert!(opened.reason.as_deref().is_some_and(|r| r.contains(tail)), "{tail}");
        }

        let garbled = opening(8181, Ok("not a code\n"));
        assert_eq!(garbled.url, plain_url(8181));
        assert!(garbled.reason.is_some());
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
