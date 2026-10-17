// The render bundles: each `src/render/<name>/main.ts` built into one
// self-contained `build-render/<name>.html`, which the daemon sends to the
// browser service's `POST /render-document` as a document (see
// `src/render/host.ts`). `build-render/manifest.json` maps each name to the
// sha256 of its file, which the daemon checks before it sends one.
//
// One Vite environment per entry, because inlining a bundle's dynamic imports
// (MapLibre is one) works only on a build with a single input. The output is
// not under `build/`, so the web service never serves it.
//
//   npm run build:render     (also the second half of `npm run build`)

import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig, type Plugin, type Rollup } from 'vite';
import { renderDocument, renderEntries } from './src/render/build';

const ROOT = import.meta.dirname;
const ENTRIES = resolve(ROOT, 'src/render');
const OUT = resolve(ROOT, 'build-render');

// Vite environment names allow no hyphen; a renderer name is `[a-z0-9-]`.
function environmentName(name: string): string {
  return `render_${name.replace(/-/g, '_')}`;
}

const names = renderEntries(ENTRIES);
const nameOfEnvironment = new Map(names.map((name) => [environmentName(name), name]));

function singleFile(): Plugin {
  return {
    name: 'istota-render-single-file',
    apply: 'build',
    generateBundle(_options, bundle) {
      const name = nameOfEnvironment.get(this.environment.name);
      if (!name) return;
      const chunks = Object.values(bundle).filter(
        (item): item is Rollup.OutputChunk => item.type === 'chunk',
      );
      if (chunks.length !== 1) {
        this.error(`render bundle ${name} built ${chunks.length} chunks; it must be one`);
      }
      const css = Object.values(bundle)
        .filter(
          (item): item is Rollup.OutputAsset =>
            item.type === 'asset' && item.fileName.endsWith('.css'),
        )
        .map((item) => String(item.source))
        .join('\n');
      for (const key of Object.keys(bundle)) delete bundle[key];
      this.emitFile({
        type: 'asset',
        fileName: `${name}.html`,
        source: renderDocument(name, chunks[0].code, css),
      });
    },
  };
}

function writeManifest(names: string[]): void {
  const manifest: Record<string, string> = {};
  for (const name of names) {
    const body = readFileSync(join(OUT, `${name}.html`));
    manifest[name] = createHash('sha256').update(body).digest('hex');
  }
  writeFileSync(join(OUT, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
}

export default defineConfig({
  plugins: [svelte({ hot: false }), singleFile()],
  publicDir: false,
  define: {
    __APP_VERSION__: JSON.stringify('render'),
    __APP_BUILT_AT__: JSON.stringify(''),
  },
  resolve: {
    alias: {
      $lib: resolve(ROOT, 'src/lib'),
      '$app/paths': resolve(ENTRIES, 'app-paths.ts'),
    },
  },
  environments: Object.fromEntries(
    names.map((name) => [
      environmentName(name),
      {
        consumer: 'client' as const,
        build: {
          outDir: OUT,
          emptyOutDir: false,
          copyPublicDir: false,
          cssCodeSplit: false,
          modulePreload: false,
          // Everything a bundle imports becomes a data: URI: the document
          // is served alone and fetches nothing from its own origin.
          assetsInlineLimit: Number.MAX_SAFE_INTEGER,
          rollupOptions: {
            input: join(ENTRIES, name, 'main.ts'),
            output: { format: 'es' as const, inlineDynamicImports: true },
          },
        },
      },
    ]),
  ),
  builder: {
    async buildApp(builder) {
      rmSync(OUT, { recursive: true, force: true });
      mkdirSync(OUT, { recursive: true });
      for (const name of names) {
        await builder.build(builder.environments[environmentName(name)]);
      }
      writeManifest(names);
    },
  },
});
