//
//  ViewController.swift
//  Istota
//
//  Subclasses CAPBridgeViewController purely so app-target plugins can be
//  registered explicitly. Capacitor's own plugins arrive through the SPM
//  package list; a plugin compiled into the app has no package to be discovered
//  from, so `capacitorDidLoad()` is the documented registration hook.
//

import Capacitor
import UIKit

class ViewController: CAPBridgeViewController {
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(IstotaLocationPlugin())

        #if DEBUG
            startStage0Probe()
        #endif
    }

    #if DEBUG
        // ---------------------------------------------------------------
        // STAGE 0 PROBE — delete once Stage 0 is signed off.
        //
        // Everything runs in the *loaded page's* JS context. Under `server.url`
        // that page is the live site, so this is the only form of these tests
        // that exercises the configuration actually being shipped:
        //
        //   bridge   JS -> Objective-C -> JS round trip through the plugin.
        //   session  GET /api/me with same-origin credentials. On a cold launch
        //            this is the cookie-persistence test: a 200 means WKWebView
        //            carried the session across process death.
        //   sse      EventSource on the room stream. Proves SSE survives the
        //            WebView, which the spec calls out as a Stage 0 risk.
        // ---------------------------------------------------------------
        private func startStage0Probe(attempt: Int = 0) {
            guard attempt < 40 else {
                NSLog("[Stage0] gave up waiting for probe to settle")
                return
            }

            let js = """
            (function () {
              var s = window.__stage0 = window.__stage0 || {
                bridge: 'pending', session: 'pending', sse: 'pending', origin: null
              };
              s.origin = window.location.origin;
              var base = window.location.pathname.indexOf('/istota') === 0 ? '/istota' : '';

              // --- bridge ---
              var cap = window.Capacitor;
              if (s.bridge === 'pending' && cap && cap.Plugins && cap.Plugins.IstotaLocation) {
                s.bridge = 'dispatched';
                cap.Plugins.IstotaLocation.ping({ value: 'from-' + s.origin }).then(function (r) {
                  s.bridge = 'ok:' + r.language + ':' + r.echo;
                }).catch(function (e) { s.bridge = 'err:' + e; });
              }

              // --- session ---
              if (s.session === 'pending') {
                s.session = 'dispatched';
                fetch(base + '/api/me', { credentials: 'same-origin' }).then(function (res) {
                  if (!res.ok) { s.session = 'unauth:' + res.status; return null; }
                  return res.json();
                }).then(function (j) {
                  if (j) { s.session = 'ok:' + (j.user_id || j.username || 'authenticated'); }
                }).catch(function (e) { s.session = 'err:' + e; });
              }

              // --- sse (only once the session is known good) ---
              if (s.sse === 'pending' && String(s.session).indexOf('ok:') === 0) {
                s.sse = 'connecting';
                try {
                  var es = new EventSource(base + '/api/chat/stream?since_id=0', { withCredentials: true });
                  es.onopen = function () { s.sse = 'open'; };
                  es.onmessage = function (ev) {
                    s.sse = 'message:' + String(ev.data).slice(0, 40);
                    es.close();
                  };
                  es.onerror = function () {
                    if (s.sse === 'connecting') { s.sse = 'err:connect-failed'; }
                    es.close();
                  };
                } catch (e) { s.sse = 'err:' + e; }
              }

              return JSON.stringify(s);
            })();
            """

            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                guard let self else { return }
                self.bridge?.webView?.evaluateJavaScript(js) { result, error in
                    if let error {
                        NSLog("[Stage0] eval error: \(error.localizedDescription)")
                    } else {
                        let text = (result as? String) ?? "<non-string>"
                        NSLog("[Stage0] \(text)")
                    }
                    self.startStage0Probe(attempt: attempt + 1)
                }
            }
        }
    #endif
}
