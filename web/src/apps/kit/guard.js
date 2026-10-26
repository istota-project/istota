// The first script in every installed app document, before any app code.
//
// ICE candidate gathering against a STUN server is not covered by
// `connect-src` in every browser, so the WebRTC constructors are taken off the
// window. Defence in depth behind the CSP; the escape tier checks it per
// browser. Each name is deleted where the binding allows it, and otherwise
// pinned to undefined, so app code cannot assign it back.
(() => {
  const NAMES = [
    'RTCPeerConnection',
    'webkitRTCPeerConnection',
    'mozRTCPeerConnection',
    'RTCDataChannel',
    'RTCIceCandidate',
  ];
  /** @type {Record<string, unknown>} */
  const scope = /** @type {any} */ (window);
  for (const name of NAMES) {
    try {
      delete scope[name];
    } catch {
      // A non-configurable binding throws in strict code; pinned below.
    }
    if (name in scope) {
      try {
        Object.defineProperty(scope, name, { value: undefined, writable: false });
      } catch {
        // Neither deletable nor redefinable: nothing more a script can do.
      }
    }
  }
})();
