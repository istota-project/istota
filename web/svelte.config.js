import adapter from '@sveltejs/adapter-static';
import { relative, sep } from 'node:path';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  compilerOptions: {
    // defaults to rune mode for the project, except for `node_modules`. Can be removed in svelte 6.
    runes: ({ filename }) => {
      const relativePath = relative(import.meta.dirname, filename);
      const pathSegments = relativePath.toLowerCase().split(sep);
      const isExternalLibrary = pathSegments.includes('node_modules');

      return isExternalLibrary ? undefined : true;
    },
  },
  kit: {
    // adapter-auto only supports some environments, see https://svelte.dev/docs/kit/adapter-auto for a list.
    // If your environment is not supported, or you settled on a specific environment, switch out the adapter.
    // See https://svelte.dev/docs/kit/adapters for more information about adapters.
    adapter: adapter(),
    paths: {
      base: '/istota',
    },
    version: {
      // Poll `_app/version.json` so a long-lived session learns a new build
      // shipped. SvelteKit only reloads on the *next navigation*, which a chat
      // tab left open for days never performs — the root layout turns `updated`
      // into a visible prompt (and auto-reloads when idle). Chiefly for the iOS
      // home-screen PWA, which caches the app shell aggressively enough to keep
      // running a deleted bundle against a current API.
      pollInterval: 300000,
    },
  },
};

export default config;
