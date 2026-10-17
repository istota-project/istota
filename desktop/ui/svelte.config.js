import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  compilerOptions: {
    runes: true,
  },
  kit: {
    // The app's own pages, served from the bundle by Tauri: one static page.
    adapter: adapter({ pages: 'build', assets: 'build', fallback: undefined }),
  },
};

export default config;
