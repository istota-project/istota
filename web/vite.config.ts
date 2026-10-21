import { existsSync } from 'node:fs';
import path from 'node:path';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, searchForWorkspaceRoot } from 'vite';
import { gitVersion } from './gitVersion';
import { mockApi } from './vite-mock-api';
import { pdfAssets } from './pdfAssets';

const APP_VERSION = process.env.VITE_APP_VERSION || gitVersion();
const APP_BUILT_AT = new Date().toISOString();

const useMock = process.env.VITE_MOCK_API === '1';

// A no-auth listener accepts a POST from this origin by port, so the Python side
// names it too (istota.config.VITE_DEV_PORT); strictPort keeps the proxying dev
// server from moving. The mock server has no backend and may take another port.
const DEV_PORT = 5173;

// MINOT_DEV points the dev server at a minot checkout's source instead of the
// installed @minot/ui, so a change there shows here with hot reload (`npm run
// minot:dev`). Not `npm link`, which brings the checkout's own svelte and so a
// second runtime. Builds, `check` and tests always use the installed version.
const minotDev = process.env.MINOT_DEV;
const minotLib = minotDev ? path.resolve(minotDev, 'src/lib') : null;
if (minotLib && !existsSync(minotLib)) {
  throw new Error(`MINOT_DEV is set, but there is no src/lib at ${minotLib}`);
}

export default defineConfig(({ command }) => {
  const minot = command === 'serve' && minotDev && minotLib ? minotLib : null;
  const server = useMock
    ? {}
    : {
        port: DEV_PORT,
        strictPort: true,
        proxy: {
          '/istota/api': {
            target: 'http://localhost:8766',
            changeOrigin: true,
          },
        },
      };
  return {
    plugins: [pdfAssets(), sveltekit(), ...(useMock ? [mockApi()] : [])],
    define: {
      __APP_VERSION__: JSON.stringify(APP_VERSION),
      __APP_BUILT_AT__: JSON.stringify(APP_BUILT_AT),
    },
    resolve: minot
      ? {
          alias: [
            // The stylesheet subpaths name files the source tree spells
            // differently, so they come before the bare package name. The
            // other subpaths (stores/notices, clipboard, dateFormat, ui/*)
            // have the same layout in the source as in the exports.
            { find: '@minot/ui/styles.css', replacement: `${minot}/styles/index.css` },
            { find: '@minot/ui/tokens.css', replacement: `${minot}/styles/tokens.css` },
            { find: '@minot/ui', replacement: minot },
          ],
          // The checkout's source resolves a bare import from its own
          // node_modules first; these must be this app's, or the page runs two
          // Svelte runtimes.
          dedupe: ['svelte', '@sveltejs/kit', 'bits-ui', '@lucide/svelte'],
        }
      : {},
    // Naming fs.allow drops Vite's own default, so the workspace root goes
    // back in beside the checkout.
    server: minot
      ? {
          ...server,
          fs: { allow: [searchForWorkspaceRoot(process.cwd()), path.resolve(minotDev!)] },
        }
      : server,
  };
});
