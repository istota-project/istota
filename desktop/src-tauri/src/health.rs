//! Whether the local web UI answers, polled through the forwarded port.
//!
//! The probe is `GET /istota/api/me` with the `Host` the web app accepts
//! under `local_code`. The web app answers it itself (401 with no session),
//! so a 200 or a 401 means the stack is up; nginx's own "starting" page,
//! which it serves while `web` is down, carries `X-Istota-Upstream` and does
//! not count. Three failures in a row turn the menu-bar icon red; the app
//! never restarts anything on its own.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

pub const FAILURES_BEFORE_UNHEALTHY: u32 = 3;

pub fn interval(window_open: bool) -> Duration {
    if window_open {
        Duration::from_secs(10)
    } else {
        Duration::from_secs(60)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    Healthy,
    Unhealthy,
}

#[derive(Debug, Default)]
pub struct Tracker {
    failures: u32,
}

impl Tracker {
    /// Records one probe; returns the verdict once it changes.
    pub fn record(&mut self, ok: bool) -> Option<Verdict> {
        if ok {
            let was_unhealthy = self.failures >= FAILURES_BEFORE_UNHEALTHY;
            self.failures = 0;
            return was_unhealthy.then_some(Verdict::Healthy);
        }
        self.failures += 1;
        (self.failures == FAILURES_BEFORE_UNHEALTHY).then_some(Verdict::Unhealthy)
    }

    pub fn reset(&mut self) {
        self.failures = 0;
    }
}

/// Whether a response head is the web app answering: a 200 or a 401, and
/// not nginx's stand-in for it.
pub fn web_answered(head: &str) -> bool {
    let mut lines = head.lines();
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok());
    let stand_in = lines
        .take_while(|line| !line.is_empty())
        .any(|line| line.to_ascii_lowercase().starts_with("x-istota-upstream:"));
    matches!(status, Some(200) | Some(401)) && !stand_in
}

pub fn probe(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_secs(3)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let request = format!(
        "GET /istota/api/me HTTP/1.1\r\nHost: localhost:{port}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut head = Vec::new();
    let mut buf = [0u8; 1024];
    while head.len() < 16 * 1024 {
        match stream.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => head.extend_from_slice(&buf[..n]),
        }
        if head.windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
    }
    web_answered(&String::from_utf8_lossy(&head).replace("\r\n", "\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn three_failures_make_it_unhealthy_and_one_success_heals_it() {
        let mut tracker = Tracker::default();
        assert_eq!(tracker.record(false), None);
        assert_eq!(tracker.record(false), None);
        assert_eq!(tracker.record(false), Some(Verdict::Unhealthy));
        assert_eq!(tracker.record(false), None);
        assert_eq!(tracker.record(true), Some(Verdict::Healthy));
        assert_eq!(tracker.record(true), None);
    }

    #[test]
    fn a_success_between_failures_starts_the_count_again() {
        let mut tracker = Tracker::default();
        tracker.record(false);
        tracker.record(false);
        assert_eq!(tracker.record(true), None);
        assert_eq!(tracker.record(false), None);
        assert_eq!(tracker.record(false), None);
    }

    #[test]
    fn only_the_web_app_itself_counts_as_an_answer() {
        assert!(web_answered("HTTP/1.1 401 Unauthorized\ncontent-type: application/json\n\n"));
        assert!(web_answered("HTTP/1.1 200 OK\n\n"));
        assert!(!web_answered("HTTP/1.1 503 Service Unavailable\nX-Istota-Upstream: unavailable\n\n"));
        assert!(!web_answered("HTTP/1.1 200 OK\nx-istota-upstream: unavailable\n\n"));
        assert!(!web_answered("HTTP/1.1 502 Bad Gateway\n\n"));
        assert!(!web_answered(""));
    }

    #[test]
    fn a_closed_port_is_a_failed_probe() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        assert!(!probe(port));
    }

    #[test]
    fn the_probe_sends_the_host_the_web_app_accepts() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 1024];
            let n = stream.read(&mut buf).unwrap();
            let request = String::from_utf8_lossy(&buf[..n]).to_string();
            stream.write_all(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n").unwrap();
            request
        });
        assert!(probe(port));
        let request = server.join().unwrap();
        assert!(request.starts_with("GET /istota/api/me HTTP/1.1\r\n"), "{request}");
        assert!(request.contains(&format!("\r\nHost: localhost:{port}\r\n")), "{request}");
    }
}
