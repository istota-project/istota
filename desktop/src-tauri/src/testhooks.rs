//! What only a `desktop` tier build does: the `desktop-test-hooks` feature,
//! which no release build enables (`tests/test_desktop_contract.py`).
//!
//! `ISTOTA_DESKTOP_FIRST_RUN_FOLDER` starts the first run at launch with that
//! folder, through the same path the first-run screen's Set up takes. It
//! also hands the web setup's address to the opener (`ISTOTA_DESKTOP_OPENER`)
//! rather than the window, so the test answers the setup over its API (the
//! page would spend the one-use code first), and leaves out the
//! launch-at-login offer, whose default button would register the test build
//! as a login item.

use std::ffi::OsStr;
use std::path::PathBuf;

/// The folder the tier's first run uses: an absolute path, and only in a
/// build with the feature.
pub fn first_run_folder_from(value: Option<&OsStr>) -> Option<PathBuf> {
    if !cfg!(feature = "desktop-test-hooks") {
        return None;
    }
    value.map(PathBuf::from).filter(|path| path.is_absolute())
}

pub fn first_run_folder() -> Option<PathBuf> {
    first_run_folder_from(std::env::var_os("ISTOTA_DESKTOP_FIRST_RUN_FOLDER").as_deref())
}

/// Row M6's negative control, built only by `scripts/test-desktop.sh
/// --control m6`: the Keychain write the Mac CLI makes through `security
/// -i`'s stdin, made again with the master key on `security`'s argv. The
/// witness's `ps` sampler must find it. Repeated for a few seconds because
/// the sampler reads every 200 ms and one `security` call is shorter.
#[cfg(feature = "m6-negative-control")]
pub fn put_the_master_key_on_an_argv(instance: &str, log: &dyn Fn(&str)) {
    use std::process::Command;
    let account = format!("secret-key-{instance}");
    let read = Command::new("/usr/bin/security")
        .args(["find-generic-password", "-s", "istota.local", "-a", &account, "-w"])
        .output();
    let key = match read {
        Ok(out) if out.status.success() => String::from_utf8_lossy(&out.stdout).trim().to_string(),
        _ => String::new(),
    };
    if key.is_empty() {
        log("m6 control: no master key in the Keychain to put on an argv");
        return;
    }
    for _ in 0..25 {
        let _ = Command::new("/usr/bin/security")
            .args(["add-generic-password", "-U", "-s", "istota.local", "-a", &account, "-w", &key])
            .status();
        std::thread::sleep(std::time::Duration::from_millis(200));
    }
    log("m6 control: wrote the master key back with it on security's argv");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[cfg(not(feature = "desktop-test-hooks"))]
    fn a_build_without_the_feature_ignores_the_folder() {
        assert_eq!(first_run_folder_from(Some("/tmp/istota-first-run".as_ref())), None);
    }

    #[test]
    #[cfg(feature = "desktop-test-hooks")]
    fn a_tier_build_takes_an_absolute_folder_only() {
        assert_eq!(
            first_run_folder_from(Some("/tmp/istota-first-run".as_ref())),
            Some(PathBuf::from("/tmp/istota-first-run"))
        );
        assert_eq!(first_run_folder_from(Some("relative".as_ref())), None);
        assert_eq!(first_run_folder_from(None), None);
    }
}
