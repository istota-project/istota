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
}

impl Default for AppState {
    fn default() -> AppState {
        AppState {
            profiles: vec![Profile { id: LOCAL_ID.into(), kind: Kind::Local }],
            last_profile: Some(LOCAL_ID.into()),
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

    pub fn current(&self) -> Option<&Profile> {
        let id = self.last_profile.as_deref()?;
        self.profiles.iter().find(|p| p.id == id)
    }
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
        };
        std::fs::write(&path, serde_json::to_string(&state).unwrap()).unwrap();
        assert_eq!(AppState::load(&path).unwrap(), state);

        std::fs::write(&path, r#"{"profiles":[{"id":"x","kind":"remote","url":"http://evil.example/"}]}"#)
            .unwrap();
        assert!(AppState::load(&path).unwrap_err().contains("profile x"));
        std::fs::write(&path, "{").unwrap();
        assert!(AppState::load(&path).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
