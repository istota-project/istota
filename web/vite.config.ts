import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { gitVersion } from './gitVersion';
import { mockApi } from './vite-mock-api';

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
            changeOrigin: true,
          },
        },
      },
});
