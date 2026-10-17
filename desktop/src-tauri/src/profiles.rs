//! The app's profiles, in `state.json`: the local install, and remote
//! installs by URL. No secrets are kept here.
//!
//! A remote profile is `https://`, or `http://` to a loopback host only. The
//! window for a profile navigates within its origin alone (`web.rs`).

use std::path::Path;

use serde::{Deserialize, Serialize};
use tauri::Url;

pub const LOCAL_ID: &str = "local";

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Kind {
    Local,
    Remote { url: String },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Profile {
    pub id: String,
    #[serde(flatten)]
    pub kind: Kind,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AppState {
    pub profiles: Vec<Profile>,
    /// The profile the window last showed.
    #[serde(default)]
    pub last_profile: Option<String>,
    /// The local VM's first run finished: a VM found without it is one a
    /// cancelled or crashed first run left, which is resumed, not started.
    #[serde(default)]
    pub local_ready: bool,
    /// The job that makes the local VM while it has not finished, so Resume
    /// after a relaunch runs the same one (a move resumes as a restore).
    #[serde(default)]
    pub pending_job: Option<crate::firstrun::Job>,
}

impl Default for AppState {
    fn default() -> AppState {
        AppState {
            profiles: vec![Profile { id: LOCAL_ID.into(), kind: Kind::Local }],
            last_profile: Some(LOCAL_ID.into()),
            local_ready: false,
            pending_job: None,
        }
    }
}

impl AppState {
    /// The state file, or the default (the local install alone) when there is
    /// none. A file that does not parse, or names a remote URL the app would
    /// refuse, is an error rather than a silent default.
    pub fn load(path: &Path) -> Result<AppState, String> {
        let text = match std::fs::read_to_string(path) {
            Ok(text) => text,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(AppState::default()),
            Err(err) => return Err(format!("could not read {}: {err}", path.display())),
        };
        let state: AppState =
            serde_json::from_str(&text).map_err(|err| format!("{} is not valid: {err}", path.display()))?;
        for profile in &state.profiles {
            if let Kind::Remote { url } = &profile.kind {
                remote_url(url).map_err(|err| format!("profile {}: {err}", profile.id))?;
            }
        }
        Ok(state)
    }

    /// Writes the state file through a temporary file and a rename, so a
    /// crash leaves the old file or the new one.
    pub fn save(&self, path: &Path) -> Result<(), String> {
        let text = serde_json::to_string_pretty(self).map_err(|err| err.to_string())?;
        let partial = path.with_extension("json.partial");
        std::fs::write(&partial, text + "\n")
            .and_then(|()| std::fs::rename(&partial, path))
            .map_err(|err| format!("could not write {}: {err}", path.display()))
    }

    pub fn current(&self) -> Option<&Profile> {
        let id = self.last_profile.as_deref()?;
        self.profiles.iter().find(|p| p.id == id)
    }

    /// Adds a remote install by name and URL; the name is its id.
    pub fn add_remote(&mut self, id: &str, url: &str) -> Result<(), String> {
        valid_id(id)?;
        if self.profiles.iter().any(|p| p.id == id) {
            return Err(format!("There is already a profile called {id}."));
        }
        let url = remote_url(url.trim())?;
        self.profiles.push(Profile { id: id.into(), kind: Kind::Remote { url: url.to_string() } });
        Ok(())
    }

    /// Removes a remote profile. The local install is removed with "Remove
    /// Istota", not here. The window falls back to the local install, or
    /// the first profile left.
    pub fn remove_remote(&mut self, id: &str) -> Result<(), String> {
        match self.profiles.iter().position(|p| p.id == id) {
            Some(index) if matches!(self.profiles[index].kind, Kind::Remote { .. }) => {
                self.profiles.remove(index);
            }
            Some(_) => return Err("The install on this Mac is removed in Settings, with Remove Istota.".into()),
            None => return Err(format!("There is no profile called {id}.")),
        }
        if self.last_profile.as_deref() == Some(id) {
            self.last_profile = self
                .profiles
                .iter()
                .find(|p| p.kind == Kind::Local)
                .or(self.profiles.first())
                .map(|p| p.id.clone());
        }
        Ok(())
    }

    pub fn select(&mut self, id: &str) -> Result<(), String> {
        if !self.profiles.iter().any(|p| p.id == id) {
            return Err(format!("There is no profile called {id}."));
        }
        self.last_profile = Some(id.into());
        Ok(())
    }

    /// The install on this Mac, put back in the list (a state file that has
    /// only remote profiles, when the user sets one up after all).
    pub fn ensure_local(&mut self) {
        if !self.profiles.iter().any(|p| p.kind == Kind::Local) {
            self.profiles.insert(0, Profile { id: LOCAL_ID.into(), kind: Kind::Local });
        }
    }
}

/// A profile's name: lowercase letters, digits and inner dashes, which is
/// also what a window label may hold. `local` is the install on this Mac.
pub fn valid_id(id: &str) -> Result<(), String> {
    let allowed = |c: char| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-';
    let ok = !id.is_empty()
        && id.len() <= 32
        && id.chars().all(allowed)
        && !id.starts_with('-')
        && !id.ends_with('-')
        && id != LOCAL_ID;
    if ok {
        Ok(())
    } else {
        Err(format!(
            "{id:?} cannot be a profile's name: use lowercase letters, digits and dashes, and not \"local\"."
        ))
    }
}

/// The website data store a remote profile's window uses: its own, so the
/// cookies and storage of one install never reach another's window
/// (`WKWebsiteDataStore(forIdentifier:)`, macOS 14). Derived from the app's
/// instance and the profile's name, so a profile keeps its store across
/// launches and a second instance of the app never shares one: FNV-1a 64 over
/// the name, twice with different offsets, as a version 8 UUID.
pub fn data_store_id(instance: &str, profile: &str) -> [u8; 16] {
    let name = format!("istota-desktop:{instance}:{profile}");
    let fnv = |offset: u64| {
        name.bytes().fold(offset, |hash, byte| (hash ^ byte as u64).wrapping_mul(0x0000_0100_0000_01b3))
    };
    let mut id = [0u8; 16];
    id[..8].copy_from_slice(&fnv(0xcbf2_9ce4_8422_2325).to_be_bytes());
    id[8..].copy_from_slice(&fnv(0x6c62_272e_07bb_0142).to_be_bytes());
    id[6] = (id[6] & 0x0f) | 0x80;
    id[8] = (id[8] & 0x3f) | 0x80;
    id
}

fn loopback(host: &str) -> bool {
    matches!(host, "localhost" | "127.0.0.1" | "[::1]")
}

/// A remote profile's URL, if the app will open it.
pub fn remote_url(text: &str) -> Result<Url, String> {
    let url = Url::parse(text).map_err(|err| format!("{text} is not a URL: {err}"))?;
    let host = url.host_str().unwrap_or("");
    match url.scheme() {
        "https" if !host.is_empty() => {}
        "http" if loopback(host) => {}
        "http" => return Err(format!("{text} is http to another machine; use https")),
        _ => return Err(format!("{text} is not an https address")),
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(format!("{text} carries credentials"));
    }
    Ok(url)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remote_urls_are_https_or_loopback_http() {
        assert!(remote_url("https://istota.example.org/istota/").is_ok());
        assert!(remote_url("http://127.0.0.1:8181/").is_ok());
        assert!(remote_url("http://localhost:8080/istota/").is_ok());
        assert!(remote_url("http://istota.example.org/").is_err());
        assert!(remote_url("http://192.168.1.2:8080/").is_err());
        assert!(remote_url("file:///etc/passwd").is_err());
        assert!(remote_url("https://user:pw@istota.example.org/").is_err());
        assert!(remote_url("not a url").is_err());
    }

    #[test]
    fn remote_profiles_are_added_selected_and_removed() {
        let mut state = AppState::default();
        state.add_remote("work", "https://istota.example.org/istota/").unwrap();
        assert!(state.add_remote("work", "https://other.example.org/").unwrap_err().contains("already"));
        assert!(state.add_remote("home", "http://istota.example.org/").is_err());
        assert!(state.add_remote("Home", "https://istota.example.org/").is_err());
        assert!(state.add_remote("local", "https://istota.example.org/").is_err());
        state.select("work").unwrap();
        assert_eq!(state.current().map(|p| p.id.as_str()), Some("work"));
        assert!(state.select("nowhere").is_err());
        assert!(state.remove_remote("local").is_err());
        state.remove_remote("work").unwrap();
        assert_eq!(state.current().map(|p| &p.kind), Some(&Kind::Local));
    }

    #[test]
    fn with_only_remote_profiles_the_local_one_comes_back_when_wanted() {
        let mut state = AppState { profiles: vec![], last_profile: None, local_ready: false, pending_job: None };
        state.add_remote("work", "https://istota.example.org/").unwrap();
        state.select("work").unwrap();
        state.ensure_local();
        state.ensure_local();
        assert_eq!(state.profiles.iter().filter(|p| p.kind == Kind::Local).count(), 1);
        assert_eq!(state.current().map(|p| p.id.as_str()), Some("work"));
    }

    #[test]
    fn each_profile_has_a_data_store_of_its_own() {
        let work = data_store_id("istota", "work");
        assert_eq!(work, data_store_id("istota", "work"), "stable across launches");
        assert_ne!(work, data_store_id("istota", "home"));
        assert_ne!(work, data_store_id("istota-dt", "work"));
        assert_eq!(work[6] >> 4, 8, "a version 8 UUID");
        assert_eq!(work[8] >> 6, 0b10, "RFC 4122 variant");
        // Pinned, since tests/desktop computes the same to clean up after itself.
        let hex: String = data_store_id("istota-dt", "dt-a").iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(hex, PINNED_DT_A);
    }

    const PINNED_DT_A: &str = "13f0730808848d93bf90807f8ac58ca6";

    #[test]
    fn no_state_file_is_the_local_install_alone() {
        let state = AppState::load(Path::new("/nonexistent/state.json")).unwrap();
        assert_eq!(state.current().map(|p| &p.kind), Some(&Kind::Local));
    }

    #[test]
    fn the_state_file_reads_back_and_refuses_a_bad_remote() {
        let dir = std::env::temp_dir().join(format!("istota-profiles-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("state.json");
        let state = AppState {
            profiles: vec![Profile {
                id: "work".into(),
                kind: Kind::Remote { url: "https://istota.example.org/istota/".into() },
            }],
            last_profile: Some("work".into()),
            local_ready: true,
            pending_job: Some(crate::firstrun::Job::Move),
        };
        state.save(&path).unwrap();
        assert_eq!(AppState::load(&path).unwrap(), state);
        // A Stage 8 state file has no local_ready: its VM's first run is not known to have finished.
        std::fs::write(&path, r#"{"profiles":[{"id":"local","kind":"local"}],"last_profile":"local"}"#).unwrap();
        assert!(!AppState::load(&path).unwrap().local_ready);

        std::fs::write(&path, r#"{"profiles":[{"id":"x","kind":"remote","url":"http://evil.example/"}]}"#)
            .unwrap();
        assert!(AppState::load(&path).unwrap_err().contains("profile x"));
        std::fs::write(&path, "{").unwrap();
        assert!(AppState::load(&path).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
