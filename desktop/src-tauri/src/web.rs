//! The web UI's windows, and what confines them (the Mac app spec,
//! "Webview confinement", row M9).
//!
//! The web UI renders model output, feed content and email, so an XSS in it
//! must not reach the Mac. A Tauri webview carries Tauri's IPC in every page
//! it loads: `window.__TAURI_INTERNALS__` is defined before any page script
//! runs, non-configurable, whatever the capabilities say. So the web UI is not
//! in a Tauri webview at all. Its window is a bare Tauri window holding a
//! plain wry `WKWebView`, built with no IPC handler, no initialization script
//! and no custom protocol, so no bridge into the app exists for a page to
//! find. Tauri's IPC stays in the app's own window (`app`), which loads only
//! the bundled pages.
//!
//! Navigation: the main frame stays on its profile's origin; a link elsewhere,
//! and every `window.open` or `target=_blank` to another origin, opens in the
//! default browser. Subframes may load other origins (the feed reader embeds
//! video players), but never a local file or a custom scheme. Downloads go to
//! `~/Downloads` through wry's handler.

#![cfg_attr(feature = "m9-negative-control", allow(dead_code))]

use tauri::Url;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Nav {
    Allow,
    /// Refused here, opened in the default browser.
    External,
    Deny,
}

fn same_origin(a: &Url, b: &Url) -> bool {
    a.origin() == b.origin()
}

/// What a navigation in a web UI window may do. `main_frame` is false for an
/// iframe's own navigation.
pub fn decide(origin: &Url, target: &str, main_frame: bool) -> Nav {
    let Ok(url) = Url::parse(target) else {
        return Nav::Deny;
    };
    match url.scheme() {
        "http" | "https" if same_origin(origin, &url) => Nav::Allow,
        "http" | "https" if main_frame => Nav::External,
        "http" | "https" => Nav::Allow,
        "about" if matches!(url.path(), "blank" | "srcdoc") => Nav::Allow,
        // A blob: URL's origin is the page that made it.
        "blob" => match Url::parse(url.path()) {
            Ok(inner) if same_origin(origin, &inner) => Nav::Allow,
            _ => Nav::Deny,
        },
        "data" if !main_frame => Nav::Allow,
        "mailto" | "tel" if main_frame => Nav::External,
        _ => Nav::Deny,
    }
}

/// What a `window.open` or a `target=_blank` link does: the same origin
/// loads in the window itself, another web address opens in the default
/// browser, anything else nowhere.
pub fn decide_new_window(origin: &Url, target: &str) -> Nav {
    match decide(origin, target, true) {
        Nav::Allow if target.starts_with("http") => Nav::Allow,
        Nav::External => Nav::External,
        _ => Nav::Deny,
    }
}

/// The default browser, through `open`. `ISTOTA_DESKTOP_OPENER` names another
/// program to hand the URL to, so the desktop tier records the call rather
/// than opening a tab on the Mac it runs on.
pub fn open_externally(url: &str) {
    let opener = std::env::var_os("ISTOTA_DESKTOP_OPENER").unwrap_or_else(|| "/usr/bin/open".into());
    let _ = std::process::Command::new(opener).arg(url).status();
}

#[cfg(not(feature = "m9-negative-control"))]
pub use confined::{close_all, open};

#[cfg(feature = "m9-negative-control")]
pub use control::{close_all, open};

#[cfg(not(feature = "m9-negative-control"))]
mod confined {
    use std::cell::RefCell;
    use std::collections::HashMap;
    use std::sync::Arc;

    use objc2::rc::Retained;
    use objc2::runtime::{NSObject, ProtocolObject};
    use objc2::{define_class, msg_send, DefinedClass, MainThreadOnly};
    use objc2_foundation::{MainThreadMarker, NSObjectProtocol};
    use objc2_web_kit::{
        WKDownload, WKNavigation, WKNavigationAction, WKNavigationActionPolicy, WKNavigationDelegate,
        WKNavigationResponse, WKNavigationResponsePolicy, WKWebView,
    };
    use tauri::{AppHandle, Manager, Url};
    use wry::{NewWindowResponse, WebViewBuilder, WebViewExtMacOS};

    use super::{decide, decide_new_window, open_externally, Nav};

    type Log = Arc<dyn Fn(&str) + Send + Sync>;

    struct Open {
        webview: wry::WebView,
        _delegate: Retained<ConfinedNavigation>,
    }

    thread_local! {
        // Main thread only, as wry's webviews are.
        static WINDOWS: RefCell<HashMap<String, Open>> = RefCell::new(HashMap::new());
    }

    pub struct Ivars {
        /// wry's own delegate: page loads and downloads go on to it.
        original: Retained<ProtocolObject<dyn WKNavigationDelegate>>,
        origin: Url,
        log: Log,
    }

    define_class!(
        // wry's delegate sees every frame's navigations with no frame to tell
        // them apart; this one decides by frame, then hands over to it.
        #[unsafe(super(NSObject))]
        #[thread_kind = MainThreadOnly]
        #[ivars = Ivars]
        struct ConfinedNavigation;

        unsafe impl NSObjectProtocol for ConfinedNavigation {}

        unsafe impl WKNavigationDelegate for ConfinedNavigation {
            #[unsafe(method(webView:decidePolicyForNavigationAction:decisionHandler:))]
            fn decide_action(
                &self,
                webview: &WKWebView,
                action: &WKNavigationAction,
                handler: &block2::DynBlock<dyn Fn(WKNavigationActionPolicy)>,
            ) {
                let ivars = self.ivars();
                // SAFETY: reads of the action WebKit handed this delegate.
                let (url, main_frame) = unsafe {
                    let url = action
                        .request()
                        .URL()
                        .and_then(|u| u.absoluteString())
                        .map(|s| s.to_string())
                        .unwrap_or_default();
                    let main_frame = action.targetFrame().map(|f| f.isMainFrame()).unwrap_or(true);
                    (url, main_frame)
                };
                match decide(&ivars.origin, &url, main_frame) {
                    // SAFETY: wry's delegate implements the method it is handed.
                    Nav::Allow => unsafe {
                        ivars
                            .original
                            .webView_decidePolicyForNavigationAction_decisionHandler(webview, action, handler)
                    },
                    Nav::External => {
                        handler.call((WKNavigationActionPolicy::Cancel,));
                        (ivars.log)(&format!("opened in the default browser: {url}"));
                        open_externally(&url);
                    }
                    Nav::Deny => {
                        handler.call((WKNavigationActionPolicy::Cancel,));
                        (ivars.log)(&format!("refused a navigation to {url}"));
                    }
                }
            }

            #[unsafe(method(webView:decidePolicyForNavigationResponse:decisionHandler:))]
            fn decide_response(
                &self,
                webview: &WKWebView,
                response: &WKNavigationResponse,
                handler: &block2::DynBlock<dyn Fn(WKNavigationResponsePolicy)>,
            ) {
                // SAFETY: forwarded unchanged to wry's delegate.
                unsafe {
                    self.ivars()
                        .original
                        .webView_decidePolicyForNavigationResponse_decisionHandler(webview, response, handler)
                }
            }

            #[unsafe(method(webView:didCommitNavigation:))]
            fn did_commit(&self, webview: &WKWebView, navigation: Option<&WKNavigation>) {
                // SAFETY: as above.
                unsafe { self.ivars().original.webView_didCommitNavigation(webview, navigation) }
            }

            #[unsafe(method(webView:didFinishNavigation:))]
            fn did_finish(&self, webview: &WKWebView, navigation: Option<&WKNavigation>) {
                // SAFETY: as above.
                unsafe { self.ivars().original.webView_didFinishNavigation(webview, navigation) }
            }

            #[unsafe(method(webView:navigationAction:didBecomeDownload:))]
            fn action_download(&self, webview: &WKWebView, action: &WKNavigationAction, download: &WKDownload) {
                // SAFETY: as above.
                unsafe {
                    self.ivars()
                        .original
                        .webView_navigationAction_didBecomeDownload(webview, action, download)
                }
            }

            #[unsafe(method(webView:navigationResponse:didBecomeDownload:))]
            fn response_download(&self, webview: &WKWebView, response: &WKNavigationResponse, download: &WKDownload) {
                // SAFETY: as above.
                unsafe {
                    self.ivars()
                        .original
                        .webView_navigationResponse_didBecomeDownload(webview, response, download)
                }
            }

            #[unsafe(method(webViewWebContentProcessDidTerminate:))]
            fn content_process_terminated(&self, webview: &WKWebView) {
                // SAFETY: as above.
                unsafe { self.ivars().original.webViewWebContentProcessDidTerminate(webview) }
            }
        }
    );

    impl ConfinedNavigation {
        fn new(mtm: MainThreadMarker, ivars: Ivars) -> Retained<Self> {
            let this = Self::alloc(mtm).set_ivars(ivars);
            // SAFETY: NSObject's init on a freshly allocated object.
            unsafe { msg_send![super(this), init] }
        }
    }

    /// Opens (or brings forward and navigates) the web UI window `label` at
    /// `url`, confined to `url`'s origin. Must run on the main thread.
    pub fn open(app: &AppHandle, label: &str, title: &str, url: &Url, log: Log) -> Result<(), String> {
        let mtm = MainThreadMarker::new().ok_or("the web UI window is made on the main thread")?;
        let navigated = WINDOWS.with(|windows| {
            windows.borrow().get(label).map(|open| open.webview.load_url(url.as_str()))
        });
        if let Some(result) = navigated {
            result.map_err(|err| err.to_string())?;
            if let Some(window) = app.get_window(label) {
                let _ = window.set_title(title);
                let _ = window.show();
                let _ = window.set_focus();
            }
            return Ok(());
        }

        let window = tauri::window::WindowBuilder::new(app, label)
            .title(title)
            .inner_size(1200.0, 820.0)
            .min_inner_size(480.0, 360.0)
            .build()
            .map_err(|err| err.to_string())?;

        let origin = url.clone();
        let new_window_log = log.clone();
        let own_label = label.to_string();
        let webview = WebViewBuilder::new()
            .with_url(url.as_str())
            .with_new_window_req_handler(move |target, _features| {
                match decide_new_window(&origin, &target) {
                    Nav::Allow => {
                        // Same origin: the window itself goes there.
                        WINDOWS.with(|windows| {
                            if let Some(open) = windows.borrow().get(&own_label) {
                                let _ = open.webview.load_url(&target);
                            }
                        });
                        NewWindowResponse::Deny
                    }
                    Nav::External => {
                        new_window_log(&format!("opened in the default browser: {target}"));
                        open_externally(&target);
                        NewWindowResponse::Deny
                    }
                    Nav::Deny => {
                        new_window_log(&format!("refused a new window for {target}"));
                        NewWindowResponse::Deny
                    }
                }
            })
            .build(&window)
            .map_err(|err| err.to_string())?;

        // SAFETY: the WKWebView wry made; its delegate is replaced on the main thread.
        let delegate = unsafe {
            let wk = webview.webview();
            let original = wk.navigationDelegate().ok_or("the webview has no navigation delegate")?;
            let delegate = ConfinedNavigation::new(mtm, Ivars { original, origin: url.clone(), log });
            wk.setNavigationDelegate(Some(ProtocolObject::from_ref(&*delegate)));
            delegate
        };

        let closing = label.to_string();
        window.on_window_event(move |event| {
            if let tauri::WindowEvent::Destroyed = event {
                WINDOWS.with(|windows| windows.borrow_mut().remove(&closing));
            }
        });
        WINDOWS.with(|windows| {
            windows
                .borrow_mut()
                .insert(label.to_string(), Open { webview, _delegate: delegate })
        });
        Ok(())
    }

    pub fn close_all(app: &AppHandle) {
        let labels: Vec<String> = WINDOWS.with(|windows| windows.borrow().keys().cloned().collect());
        for label in labels {
            if let Some(window) = app.get_window(&label) {
                let _ = window.destroy();
            }
        }
    }
}

#[cfg(feature = "m9-negative-control")]
mod control {
    //! Row M9's negative control: the web UI in a stock Tauri webview.
    use std::sync::Arc;

    use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindowBuilder};

    pub fn open(app: &AppHandle, label: &str, title: &str, url: &Url, _log: Arc<dyn Fn(&str) + Send + Sync>) -> Result<(), String> {
        if let Some(window) = app.get_webview_window(label) {
            return window.navigate(url.clone()).map_err(|err| err.to_string());
        }
        WebviewWindowBuilder::new(app, label, WebviewUrl::External(url.clone()))
            .title(title)
            .inner_size(1200.0, 820.0)
            .build()
            .map(|_| ())
            .map_err(|err| err.to_string())
    }

    pub fn close_all(_app: &AppHandle) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    fn origin() -> Url {
        Url::parse("http://localhost:8080/istota/").unwrap()
    }

    #[test]
    fn the_main_frame_stays_on_its_origin() {
        let o = origin();
        assert_eq!(decide(&o, "http://localhost:8080/istota/chat/", true), Nav::Allow);
        assert_eq!(decide(&o, "http://localhost:8081/istota/", true), Nav::External);
        assert_eq!(decide(&o, "http://127.0.0.1:8080/istota/", true), Nav::External);
        assert_eq!(decide(&o, "https://example.org/article", true), Nav::External);
        assert_eq!(decide(&o, "mailto:someone@example.org", true), Nav::External);
    }

    #[test]
    fn nothing_reaches_a_local_file_or_a_custom_scheme() {
        let o = origin();
        for target in ["file:///etc/passwd", "tauri://localhost/index.html", "ipc://localhost/x", "javascript:alert(1)", "not a url"] {
            assert_eq!(decide(&o, target, true), Nav::Deny, "{target}");
            assert_eq!(decide(&o, target, false), Nav::Deny, "{target}");
        }
        assert_eq!(decide(&o, "data:text/html,hi", true), Nav::Deny);
    }

    #[test]
    fn subframes_may_embed_other_origins() {
        let o = origin();
        assert_eq!(decide(&o, "https://www.youtube-nocookie.com/embed/x", false), Nav::Allow);
        assert_eq!(decide(&o, "about:blank", false), Nav::Allow);
        assert_eq!(decide(&o, "about:srcdoc", false), Nav::Allow);
        assert_eq!(decide(&o, "blob:http://localhost:8080/0f1e", false), Nav::Allow);
        assert_eq!(decide(&o, "blob:https://evil.example/0f1e", false), Nav::Deny);
    }

    #[test]
    fn new_windows_open_in_the_default_browser_unless_same_origin() {
        let o = origin();
        assert_eq!(decide_new_window(&o, "https://example.org/"), Nav::External);
        assert_eq!(decide_new_window(&o, "http://localhost:8080/istota/files/x.pdf"), Nav::Allow);
        assert_eq!(decide_new_window(&o, "about:blank"), Nav::Deny);
        assert_eq!(decide_new_window(&o, "file:///Users/a/x"), Nav::Deny);
    }
}
