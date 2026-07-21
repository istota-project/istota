import { resolve } from 'node:path';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

// Component tests (`*.svelte.test.ts`) mount real Svelte components under jsdom
// to verify rendering + reactivity; the pure reducer tests (`segments.test.ts`)
// need no DOM but run fine here too. The Svelte plugin compiles components.
export default defineConfig({
  plugins: [svelte({ hot: false })],
  resolve: {
    alias: {
      $lib: resolve(__dirname, 'src/lib'),
    },
    // Use the browser entry for Svelte so runtime imports resolve under vitest.
    conditions: ['browser'],
  },
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'jsdom',
    setupFiles: ['./vitest-setup.ts'],
  },
});
