// The apps build: the house kit and the starter apps, into `build-apps/`.
//
//   build-apps/kit/guard.js, istota-app.js, components.js   copied verbatim
//   build-apps/kit/istota.css       built, so its @imports of minot's tokens
//                                   and app-tokens.css are inlined
//   build-apps/starter/<id>/...     copied verbatim, tests left out
//   build-apps/manifest.json        {relative path: sha256} for every file
//
// The daemon reads it through `istota.apps.kit` when it builds an app
// document, and checks each file against the manifest. The output is not
// under `build/`, so the web service never serves it.
//
//   npm run build:apps     (also the last part of `npm run build`)

import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { defineConfig, type Plugin, type Rollup } from 'vite';
import {
  checkInlineable,
  KIT_SCRIPTS,
  KIT_STYLESHEET,
  manifestOf,
  starterFiles,
} from './src/apps/build';

const ROOT = import.meta.dirname;
const KIT = resolve(ROOT, 'src/apps/kit');
const STARTER = resolve(ROOT, 'src/apps/starter');
const OUT = resolve(ROOT, 'build-apps');
const ENVIRONMENT = 'apps_kit';

function kitFiles(): Plugin {
  return {
    name: 'istota-apps-kit',
    apply: 'build',
    generateBundle: {
      order: 'post',
      handler(_options, bundle) {
        if (this.environment.name !== ENVIRONMENT) return;
        const css = Object.values(bundle)
          .filter(
            (item): item is Rollup.OutputAsset =>
              item.type === 'asset' && item.fileName.endsWith('.css'),
          )
          .map((item) => String(item.source))
          .join('\n');
        if (!css.includes('--surface-base')) {
          this.error(`the kit stylesheet came out without minot's tokens`);
        }
        for (const key of Object.keys(bundle)) delete bundle[key];
        checkInlineable(KIT_STYLESHEET, css);
        this.emitFile({ type: 'asset', fileName: `kit/${KIT_STYLESHEET}`, source: css });
        for (const name of KIT_SCRIPTS) {
          const text = readFileSync(join(KIT, name), 'utf8');
          checkInlineable(name, text);
          this.emitFile({ type: 'asset', fileName: `kit/${name}`, source: text });
        }
      },
    },
  };
}

function copyStarterApps(): void {
  for (const path of starterFiles(STARTER)) {
    const target = join(OUT, 'starter', path);
    mkdirSync(dirname(target), { recursive: true });
    cpSync(join(STARTER, path), target);
  }
}

export default defineConfig({
  plugins: [kitFiles()],
  publicDir: false,
  environments: {
    [ENVIRONMENT]: {
      consumer: 'client' as const,
      build: {
        outDir: OUT,
        emptyOutDir: false,
        copyPublicDir: false,
        cssMinify: false,
        minify: false,
        modulePreload: false,
        // Nothing the stylesheet names may be fetched: a frame's CSP allows
        // only data: fonts and images.
        assetsInlineLimit: Number.MAX_SAFE_INTEGER,
        rollupOptions: { input: join(KIT, KIT_STYLESHEET) },
      },
    },
  },
  builder: {
    async buildApp(builder) {
      rmSync(OUT, { recursive: true, force: true });
      mkdirSync(OUT, { recursive: true });
      await builder.build(builder.environments[ENVIRONMENT]);
      copyStarterApps();
      writeFileSync(join(OUT, 'manifest.json'), `${JSON.stringify(manifestOf(OUT), null, 2)}\n`);
    },
  },
});
