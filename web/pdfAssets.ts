import type { Plugin } from 'vite';
import { readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';

/** PDF.js imports decoders by filename; Vite's hashed ?url assets cannot share
 * that directory contract. Emit only the fixed runtime resources, versioned
 * with the package, and serve the identical set during development. */
export function pdfAssets(): Plugin {
  const require = createRequire(import.meta.url);
  const root = dirname(require.resolve('pdfjs-dist/package.json'));
  const { version } = require('pdfjs-dist/package.json');
  const prefix = `pdfjs/${version}/`;
  const files = new Map<string, Buffer>();
  for (const directory of ['cmaps', 'standard_fonts', 'wasm']) {
    for (const name of readdirSync(join(root, directory))) {
      // No scripting/QuickJS assets. The two JS decoders work with useWasm:false.
      if (
        directory === 'wasm' &&
        !['jbig2_nowasm_fallback.js', 'openjpeg_nowasm_fallback.js'].includes(name) &&
        !name.startsWith('LICENSE')
      )
        continue;
      files.set(`${prefix}${directory}/${name}`, readFileSync(join(root, directory, name)));
    }
  }
  let base = '/';
  return {
    name: 'pdf-runtime-assets',
    configResolved(config) {
      base = config.base;
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = req.url?.split('?')[0];
        if (!path?.startsWith(base)) return next();
        const key = path.slice(base.length);
        const bytes = files.get(key);
        if (!bytes) return next();
        res.setHeader(
          'Content-Type',
          key.endsWith('.js') ? 'text/javascript' : 'application/octet-stream',
        );
        res.end(bytes);
      });
    },
    generateBundle(options) {
      // SvelteKit builds its server bundle too; these belong to the client.
      if (options.dir?.endsWith('/server')) return;
      for (const [fileName, source] of files) this.emitFile({ type: 'asset', fileName, source });
    },
  };
}
