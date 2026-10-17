import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [sveltekit()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    // The web UI's design tokens are read from web/, not copied.
    fs: { allow: ['../../web/src/lib/styles'] },
  },
});
