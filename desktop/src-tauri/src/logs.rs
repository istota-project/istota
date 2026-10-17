//! The app's own log and the "Show logs" bundle.
//!
//! `logs/app.log` keeps every Mac CLI command the app ran, its exit, and the
//! output of the long calls (create, start, stop). A one-use address the CLI
//! prints (the setup page's code, an enrolment link) is cut at its `?` or `#`
//! before it is written, and a call whose output is a value, such as a login
//! code, is logged by its command alone (`vm.rs`).
//!
//! "Show logs" writes `logs/<UTC timestamp>/` with `istota-stack status` and a
//! copy of the app's log, then reveals it in Finder.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

/// The app log is cut to its newest half past this size.
const MAX_LOG_BYTES: u64 = 4 * 1024 * 1024;

/// `line` with the query and fragment of every web address in it removed.
pub fn redact_line(line: &str) -> String {
    let mut out = Vec::new();
    for word in line.split(' ') {
        let is_url = word.contains("http://") || word.contains("https://");
        match word.find(['?', '#']) {
            Some(cut) if is_url => out.push(format!("{}…", &word[..cut])),
            _ => out.push(word.to_string()),
        }
    }
    out.join(" ")
}

pub fn append(log: &Path, line: &str) {
    if let Ok(meta) = std::fs::metadata(log) {
        if meta.len() > MAX_LOG_BYTES {
            if let Ok(text) = std::fs::read_to_string(log) {
                let keep = &text[text.len() / 2..];
                let start = keep.find('\n').map(|i| i + 1).unwrap_or(0);
                let _ = std::fs::write(log, &keep[start..]);
            }
        }
    }
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(log) {
        let _ = writeln!(file, "{} {}", timestamp(SystemTime::now(), true), redact_line(line));
    }
}

/// UTC, as `2026-10-16T09:05:03Z`, or `20261016-090503` for a directory name.
pub fn timestamp(at: SystemTime, readable: bool) -> String {
    let secs = at.duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let (year, month, day) = civil_from_days((secs / 86_400) as i64);
    let (h, m, s) = (secs % 86_400 / 3600, secs % 3600 / 60, secs % 60);
    if readable {
        format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}Z")
    } else {
        format!("{year:04}{month:02}{day:02}-{h:02}{m:02}{s:02}")
    }
}

/// Days since 1970-01-01 to a proleptic Gregorian date (Howard Hinnant's algorithm).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// Writes a logs bundle and returns its directory.
pub fn write_bundle(logs_dir: &Path, status: &str, app_log: &Path) -> std::io::Result<PathBuf> {
    let dir = logs_dir.join(timestamp(SystemTime::now(), false));
    std::fs::create_dir_all(&dir)?;
    let status: Vec<String> = status.lines().map(redact_line).collect();
    std::fs::write(dir.join("status.txt"), status.join("\n") + "\n")?;
    if app_log.exists() {
        std::fs::copy(app_log, dir.join("app.log"))?;
    }
    Ok(dir)
}

pub fn reveal(dir: &Path) {
    let _ = std::process::Command::new("/usr/bin/open").arg("-R").arg(dir).status();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn one_use_addresses_lose_their_query_and_fragment() {
        assert_eq!(
            redact_line("ISTOTA_SETUP_URL=http://localhost:8080/istota/setup#code=ABCDE-FGHIJ"),
            "ISTOTA_SETUP_URL=http://localhost:8080/istota/setup…"
        );
        assert_eq!(
            redact_line("[istota-stack] set a's password here (the link works once): https://h/istota/auth/set-password?token=x"),
            "[istota-stack] set a's password here (the link works once): https://h/istota/auth/set-password…"
        );
        assert_eq!(redact_line("exit Some(0) #1"), "exit Some(0) #1");
    }

    #[test]
    fn timestamps_are_utc_dates() {
        let at = UNIX_EPOCH + Duration::from_secs(1_792_141_503);
        assert_eq!(timestamp(at, true), "2026-10-16T09:05:03Z");
        assert_eq!(timestamp(at, false), "20261016-090503");
        assert_eq!(timestamp(UNIX_EPOCH + Duration::from_secs(951_782_400), true), "2000-02-29T00:00:00Z");
    }

    #[test]
    fn the_bundle_holds_the_status_and_the_app_log() {
        let dir = std::env::temp_dir().join(format!("istota-logs-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("app.log");
        append(&log, "$ istota-stack start");
        let bundle = write_bundle(&dir, "VM: Running\nopen http://localhost:8080/istota/setup#code=X\n", &log).unwrap();
        let status = std::fs::read_to_string(bundle.join("status.txt")).unwrap();
        assert_eq!(status, "VM: Running\nopen http://localhost:8080/istota/setup…\n");
        assert!(std::fs::read_to_string(bundle.join("app.log")).unwrap().ends_with(" $ istota-stack start\n"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
