import { execSync } from 'node:child_process';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { mockApi } from './vite-mock-api';

function gitVersion(): string {
	try {
		const sha = execSync('git rev-parse --short HEAD', { encoding: 'utf8' }).trim();
		// Scope the dirty check to web/ — runtime config files under config/ and
		// config/users/ are expected to drift on deployed hosts.
		const dirty = execSync('git status --porcelain -- .', { encoding: 'utf8' }).trim().length > 0;
		return dirty ? `${sha}-dirty` : sha;
	} catch {
		return 'unknown';
	}
}

const APP_VERSION = process.env.VITE_APP_VERSION || gitVersion();
const APP_BUILT_AT = new Date().toISOString();

const useMock = process.env.VITE_MOCK_API === '1';

export default defineConfig({
	plugins: [sveltekit(), ...(useMock ? [mockApi()] : [])],
	define: {
		__APP_VERSION__: JSON.stringify(APP_VERSION),
		__APP_BUILT_AT__: JSON.stringify(APP_BUILT_AT),
	},
	server: useMock
		? {}
		: {
			proxy: {
				'/istota/api': {
					target: 'http://localhost:8766',
					changeOrigin: true
				}
			}
		}
});
