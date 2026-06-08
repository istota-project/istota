import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';

// Standalone vitest config — the reducer under test is pure (no DOM, no Svelte
// runtime), so we don't load the SvelteKit plugin. The `$lib` alias is provided
// for any future test that imports across the lib boundary.
export default defineConfig({
	resolve: {
		alias: {
			$lib: resolve(__dirname, 'src/lib'),
		},
	},
	test: {
		include: ['src/**/*.test.ts'],
		environment: 'node',
	},
});
