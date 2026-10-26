// The escape tier's server: the shell page from shell/, and each built fixture
// at /frame/<id> with the headers the real frame route sends. Not SvelteKit:
// the shell page imports only bridge.ts and frameWatch.ts.
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import { OUT, PORT, WEB, cspRemoved } from './fixtures';

const APP_ID = /^[a-z0-9][a-z0-9-]{0,39}$/;

function frameRoute(): Plugin {
  return {
    name: 'apps-escape-frame',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const match = /^\/frame\/([^/?#]+)$/.exec(req.url ?? '');
        if (!match) return next();
        const id = match[1];
        const html = join(OUT, id, 'frame.html');
        if (!APP_ID.test(id) || !existsSync(html)) {
          res.statusCode = 404;
          res.end();
          return;
        }
        res.setHeader('Content-Type', 'text/html; charset=utf-8');
        res.setHeader('X-Content-Type-Options', 'nosniff');
        res.setHeader('Referrer-Policy', 'no-referrer');
        res.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
        res.setHeader('Cache-Control', 'private, no-store');
        if (!cspRemoved()) {
          const csp = readFileSync(join(OUT, id, 'csp.txt'), 'utf8').trim();
          res.setHeader('Content-Security-Policy', csp);
        }
        res.end(readFileSync(html));
      });
    },
  };
}

export default defineConfig({
  root: join(import.meta.dirname, 'shell'),
  plugins: [frameRoute()],
  server: {
    host: '127.0.0.1',
    port: PORT,
    strictPort: true,
    fs: { allow: [WEB] },
  },
  logLevel: 'warn',
});
