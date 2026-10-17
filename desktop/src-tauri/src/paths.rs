//! Where the app keeps its state, and where the bundled tools are.
//!
//! The state directory is `~/Library/Application Support/Istota/`
//! (`ISTOTA_DESKTOP_STATE_DIR` overrides it, for the `desktop` tier or a second instance):
//! `state.json`, `lima/` (the app's own `LIMA_HOME`), `config/` (the Mac CLI's
//! `XDG_CONFIG_HOME`, so `~/.config/istota-stack/` stays the command-line
//! install's) and `logs/`.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

/// The Lima instance the app's VM is, as the Mac CLI names it, unless
/// `ISTOTA_DESKTOP_INSTANCE` names another. The Mac CLI keys the Keychain's
/// master key by it (`secret-key-<instance>`), so a second instance never
/// reads or writes the first one's.
pub const DEFAULT_INSTANCE: &str = "istota";

/// The command-line install's instance in `~/.lima`, which the app offers to
/// move in and stops; always this name, whatever the app's own is.
pub const CLI_INSTALL_INSTANCE: &str = "istota";

/// The app's instance name from `ISTOTA_DESKTOP_INSTANCE`'s value. A name
/// that is not lowercase letters, digits and inner dashes is refused rather
/// than falling back, since the fallback is the default instance's Keychain item.
pub fn instance_from(value: Option<&OsStr>) -> Result<String, String> {
    let Some(value) = value else {
        return Ok(DEFAULT_INSTANCE.to_string());
    };
    let name = value.to_str().unwrap_or("");
    let allowed = |c: char| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-';
    let valid = !name.is_empty()
        && name.len() <= 40
        && name.chars().all(allowed)
        && !name.starts_with('-')
        && !name.ends_with('-');
    if valid {
        Ok(name.to_string())
    } else {
        Err(format!(
            "ISTOTA_DESKTOP_INSTANCE={:?} is not an instance name (lowercase letters, digits and dashes).",
            value
        ))
    }
}

#[derive(Clone, Debug)]
pub struct Paths {
    pub state_dir: PathBuf,
    /// `Contents/Resources` of the app bundle.
    pub resources: PathBuf,
    /// The app's Lima instance, as the Mac CLI is told it.
    pub instance: String,
}

impl Paths {
    pub fn new(state_dir: PathBuf, resources: PathBuf) -> Paths {
        Paths { state_dir, resources, instance: DEFAULT_INSTANCE.to_string() }
    }

    pub fn with_instance(self, instance: String) -> Paths {
        Paths { instance, ..self }
    }

    pub fn default_instance() -> Result<String, String> {
        instance_from(std::env::var_os("ISTOTA_DESKTOP_INSTANCE").as_deref())
    }

    pub fn default_state_dir() -> PathBuf {
        if let Some(dir) = std::env::var_os("ISTOTA_DESKTOP_STATE_DIR") {
            return PathBuf::from(dir);
        }
        home().join("Library/Application Support/Istota")
    }

    pub fn state_file(&self) -> PathBuf {
        self.state_dir.join("state.json")
    }

    pub fn lima_home(&self) -> PathBuf {
        self.state_dir.join("lima")
    }

    pub fn cli_config_home(&self) -> PathBuf {
        self.state_dir.join("config")
    }

    /// The Mac CLI's state file: what `create` was given, the port and the folder.
    pub fn cli_state_file(&self) -> PathBuf {
        self.cli_config_home().join("istota-stack").join(format!("{}.env", self.instance))
    }

    /// `release/<tag>/`: the releases the VM mounts read-only, copied out of
    /// the app so the VM does not depend on where the app is.
    pub fn release_root(&self) -> PathBuf {
        self.state_dir.join("release")
    }

    pub fn logs_dir(&self) -> PathBuf {
        self.state_dir.join("logs")
    }

    pub fn app_log(&self) -> PathBuf {
        self.logs_dir().join("app.log")
    }

    /// Where the Mac CLI would refresh its scripts: a directory of the app's
    /// own that holds no install, so it never replaces `~/.local/bin`'s.
    pub fn cli_bin_dir(&self) -> PathBuf {
        self.state_dir.join("bin")
    }

    pub fn bin_dir(&self) -> PathBuf {
        self.resources.join("bin")
    }

    pub fn mac_cli(&self) -> PathBuf {
        self.bin_dir().join("istota-stack")
    }

    pub fn limactl(&self) -> PathBuf {
        self.bin_dir().join("limactl")
    }

    /// `PATH` for every Mac CLI call: the bundled tools, then the system's.
    pub fn search_path(&self) -> String {
        format!("{}:/usr/bin:/bin:/usr/sbin:/sbin", self.bin_dir().display())
    }

    /// Makes the state directories, and keeps the VM's disk out of Time Machine.
    pub fn ensure(&self) -> std::io::Result<()> {
        std::fs::create_dir_all(self.logs_dir())?;
        std::fs::create_dir_all(self.cli_config_home())?;
        let lima = self.lima_home();
        if !lima.exists() {
            std::fs::create_dir_all(&lima)?;
            exclude_from_backups(&lima);
        }
        Ok(())
    }
}

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

/// `tmutil addexclusion` is a sticky exclusion on the directory itself, which
/// needs no administrator. A failure only costs backup space, so it is logged.
fn exclude_from_backups(dir: &Path) {
    let status = std::process::Command::new("/usr/bin/tmutil")
        .arg("addexclusion")
        .arg(dir)
        .status();
    if !matches!(status, Ok(s) if s.success()) {
        eprintln!("istota: could not exclude {} from Time Machine", dir.display());
    }
}

/// One value of the Mac CLI's state file, read as its `state_get` reads it:
/// the last line for the key, unquoted, and nothing when that is empty.
pub fn env_file_value(text: &str, key: &str) -> Option<String> {
    let prefix = format!("{key}=");
    text.lines()
        .rev()
        .find_map(|line| line.strip_prefix(&prefix))
        .map(str::to_string)
        .filter(|value| !value.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_cli_runs_with_the_bundled_tools_first() {
        let paths = Paths::new("/s".into(), "/A/Contents/Resources".into());
        assert_eq!(
            paths.search_path(),
            "/A/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        );
        assert_eq!(paths.cli_state_file(), PathBuf::from("/s/config/istota-stack/istota.env"));
        assert_eq!(paths.lima_home(), PathBuf::from("/s/lima"));
    }

    #[test]
    fn an_env_file_value_is_the_last_one_set() {
        let text = "PORT=8080\nWORKSPACE=/Users/a/Istota\nPORT=8181\nTAG=\n";
        assert_eq!(env_file_value(text, "PORT").as_deref(), Some("8181"));
        assert_eq!(env_file_value(text, "WORKSPACE").as_deref(), Some("/Users/a/Istota"));
        assert_eq!(env_file_value(text, "TAG"), None);
        assert_eq!(env_file_value(text, "MISSING"), None);
    }

    #[test]
    fn the_instance_is_istota_unless_the_environment_names_another() {
        assert_eq!(instance_from(None).as_deref(), Ok("istota"));
        assert_eq!(instance_from(Some("istota-d9".as_ref())).as_deref(), Ok("istota-d9"));
        for bad in ["", "../x", "a b", "-x", "Istota", "x/y"] {
            assert!(instance_from(Some(bad.as_ref())).is_err(), "{bad:?}");
        }
        let paths = Paths::new("/s".into(), "/R".into()).with_instance("istota-d9".into());
        assert_eq!(paths.cli_state_file(), PathBuf::from("/s/config/istota-stack/istota-d9.env"));
        assert_eq!(Paths::new("/s".into(), "/R".into()).instance, "istota");
    }
}
