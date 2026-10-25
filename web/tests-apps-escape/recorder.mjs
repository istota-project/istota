// The escape tier's recorder: a second loopback origin the fixtures point their
// attempted requests at, so "did anything leave the frame" has an answer that
// does not depend on how a browser reports a blocked request. Chromium emits
// Playwright `request` events for CSP-blocked XHR and images (the request never
// reaches the network); Firefox and WebKit do not. The recorder is what actually
// received bytes, so the verdict rests on it (`/log`), and the browser's own
// request events are attached to the run for the navigations and for context.
//
// Serves `/log` (the hits so far, JSON), `/landed` (the page the pre-load
// navigation fixture points at, which tries the bridge and reports back through
// `/mark`), and 200s for everything else. WebSocket upgrades are logged and
// dropped, since a CSP-less frame's socket attempt is a leak the tier must see.
import { createServer } from 'node:http';

const PORT = Number(process.env.APPS_ESCAPE_RECORDER_PORT || '5198');
const hits = [];

const LANDED = `<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><title>landed</title></head>
<body><p>landed</p>
<script>
(async () => {
  const mark = (query) => fetch('/mark?' + query, { cache: 'no-store' }).catch(() => {});
  const port = await new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), 5000);
    window.addEventListener('message', (event) => {
      const data = event.data || {};
      if (data.istota !== 1 || data.kind !== 'port' || !event.ports || !event.ports[0]) return;
      clearTimeout(timer);
      resolve(event.ports[0]);
    });
  });
  if (!port) {
    await mark('page=landed&port=0');
    return;
  }
  await mark('page=landed&port=1');
  // A page that navigated the frame before its first load is handed the port.
  // It never saw the fragment, so it cannot know the key: every call must be
  // refused. A reply here would mean the gate failed.
  port.onmessage = (event) => {
    const data = event.data || {};
    if (data.istota === 1 && typeof data.id === 'number') mark('page=landed&call-answered=1');
  };
  port.postMessage({ istota: 1, kind: 'hello', k: '' });
  port.postMessage({ istota: 1, id: 1, kind: 'call', capability: 'kv:own', op: 'list', args: {} });
})();
</script></body></html>`;

const server = createServer((req, res) => {
  const url = new URL(req.url ?? '/', `http://127.0.0.1:${PORT}`);
  if (url.pathname === '/log' && req.method === 'GET') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(hits));
    return;
  }
  if (url.pathname === '/log/clear' && req.method === 'POST') {
    hits.length = 0;
    res.writeHead(204);
    res.end();
    return;
  }
  if (url.pathname === '/landed') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(LANDED);
    return;
  }
  hits.push({ method: req.method, path: url.pathname, query: url.search, at: Date.now() });
  res.writeHead(200, { 'content-type': 'text/plain' });
  res.end('recorded');
});

server.on('upgrade', (req, socket) => {
  const url = new URL(req.url ?? '/', `http://127.0.0.1:${PORT}`);
  hits.push({ method: 'WS', path: url.pathname, query: url.search, at: Date.now() });
  socket.destroy();
});

server.listen(PORT, '127.0.0.1', () => {
  process.stdout.write(`apps-escape recorder on http://127.0.0.1:${PORT}\n`);
});
