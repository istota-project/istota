import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		proxy: {
			'/istota/api': {
				target: 'http://localhost:8766',
				changeOrigin: true
			}
		}
	}
});
