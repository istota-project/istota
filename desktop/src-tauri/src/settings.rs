//! The app's Settings (the Mac app spec, "Settings"): the background
//! installs and the local model servers. Restore and Remove are VM calls
//! (`vm.rs`); brain and credentials are the web UI's own settings pages.

use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// One line of `istota-stack local-layers --status`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Layer {
    pub layer: String,
    /// `absent`, `building`, `ready` or `failed`.
    pub state: String,
    #[serde(default)]
    pub detail: String,
}

pub fn parse_layers(stdout: &str) -> Vec<Layer> {
    stdout.lines().filter_map(|line| serde_json::from_str::<Layer>(line.trim()).ok()).collect()
}

/// Words a build's last lines carry when it could not connect out.
const CONNECT_TIMEOUTS: &[&str] = &["timed out", "timeout", "etimedout", "could not resolve", "temporary failure in name resolution"];

/// The hint for an outbound filter: both layers failed, each on a connect
/// timeout. A Mac app firewall (Little Snitch, Radio Silence) holds a newly
/// signed `limactl`'s connections until the user allows them.
pub fn filter_hint(layers: &[Layer]) -> Option<&'static str> {
    let timed_out = |layer: &Layer| {
        let detail = layer.detail.to_ascii_lowercase();
        layer.state == "failed" && CONNECT_TIMEOUTS.iter().any(|word| detail.contains(word))
    };
    (layers.len() >= 2 && layers.iter().all(timed_out)).then_some(
        "Both installs timed out connecting. If this Mac runs an outbound firewall such as Little Snitch or Radio Silence, allow Istota's limactl, then Retry.",
    )
}

/// The local model servers the app knows, by their default ports.
pub const KNOWN_SERVERS: &[(&str, u16)] = &[("Ollama", 11434), ("LM Studio", 1234)];

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct LocalModel {
    pub name: &'static str,
    pub port: u16,
    /// Something accepts connections on the Mac's loopback at the port.
    pub listening: bool,
    /// Containers may reach the port (`HOST_LOOPBACK_ALLOW`).
    pub allowed: bool,
}

/// The ports `HOST_LOOPBACK_ALLOW` lists; the Mac CLI writes them
/// space-separated, and an older value may use commas.
pub fn allowed_ports(value: &str) -> Vec<u16> {
    value
        .split([',', ' ', '\n', '\t'])
        .filter_map(|word| word.trim().parse().ok())
        .collect()
}

pub fn listening(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok()
}

pub fn local_models(allowed: &[u16], probe: impl Fn(u16) -> bool) -> Vec<LocalModel> {
    KNOWN_SERVERS
        .iter()
        .map(|(name, port)| LocalModel { name, port: *port, listening: probe(*port), allowed: allowed.contains(port) })
        .collect()
}

/// Whether a port is one the app may toggle: only the known servers'.
pub fn known_port(port: u16) -> bool {
    KNOWN_SERVERS.iter().any(|(_, known)| *known == port)
}

/// `ISTOTA_LOCAL_LAYERS` with Claude Code left out, or back in. The VM reads
/// a key that is there but empty as no layers at all, so "back in" names both.
pub fn local_layers_value(claude_code: bool) -> &'static str {
    if claude_code {
        "claude-code chrome"
    } else {
        "chrome"
    }
}

/// Whether Claude Code is wanted: the status says "not wanted" when
/// `ISTOTA_LOCAL_LAYERS` leaves it out.
pub fn claude_code_wanted(layers: &[Layer]) -> bool {
    !layers
        .iter()
        .any(|l| l.layer == "claude-code" && l.state == "absent" && l.detail.starts_with("not wanted"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_status_lines_are_read_and_anything_else_skipped() {
        let out = concat!(
            r#"{"layer": "claude-code", "state": "ready", "detail": ""}"#, "\n",
            "[istota-stack] something else\n",
            r#"{"layer": "chrome", "state": "failed", "detail": "E: Failed to fetch\nConnection timed out"}"#, "\n",
        );
        let layers = parse_layers(out);
        assert_eq!(layers.len(), 2);
        assert_eq!(layers[0].state, "ready");
        assert_eq!(layers[1].layer, "chrome");
    }

    #[test]
    fn the_filter_is_named_only_when_both_layers_timed_out() {
        let failed = |layer: &str, detail: &str| Layer { layer: layer.into(), state: "failed".into(), detail: detail.into() };
        let both = [
            failed("claude-code", "npm error code ETIMEDOUT"),
            failed("chrome", "curl: (28) Connection timed out after 30001 milliseconds"),
        ];
        assert!(filter_hint(&both).unwrap().contains("Little Snitch"));
        let one = [both[0].clone(), failed("chrome", "dpkg: dependency problems")];
        assert_eq!(filter_hint(&one), None);
        let ready = [both[0].clone(), Layer { layer: "chrome".into(), state: "ready".into(), detail: String::new() }];
        assert_eq!(filter_hint(&ready), None);
        assert_eq!(filter_hint(&both[..1]), None);
    }

    #[test]
    fn local_models_are_the_known_servers_with_their_state() {
        assert_eq!(allowed_ports("11434 8000"), vec![11434, 8000]);
        assert_eq!(allowed_ports("1234,11434\n"), vec![1234, 11434]);
        assert!(allowed_ports("").is_empty());
        let models = local_models(&[11434], |port| port == 1234);
        assert_eq!(
            models,
            vec![
                LocalModel { name: "Ollama", port: 11434, listening: false, allowed: true },
                LocalModel { name: "LM Studio", port: 1234, listening: true, allowed: false },
            ]
        );
        assert!(known_port(1234) && !known_port(22));
    }

    #[test]
    fn the_probe_finds_a_listener_and_not_a_closed_port() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(listening(port));
        // Not the dropped listener's port: a parallel test may bind it again.
        assert!(!listening(1));
    }

    #[test]
    fn opting_out_of_claude_code_leaves_chrome() {
        assert_eq!(local_layers_value(false), "chrome");
        assert_eq!(local_layers_value(true), "claude-code chrome");
        let layer = |state: &str, detail: &str| Layer { layer: "claude-code".into(), state: state.into(), detail: detail.into() };
        assert!(!claude_code_wanted(&[layer("absent", "not wanted: ISTOTA_LOCAL_LAYERS in host.env leaves it out")]));
        assert!(claude_code_wanted(&[layer("absent", "")]));
        assert!(claude_code_wanted(&[]));
    }
}
