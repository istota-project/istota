import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { gitVersion } from './gitVersion';
import { mockApi } from './vite-mock-api';
import { pdfAssets } from './pdfAssets';

const APP_VERSION = process.env.VITE_APP_VERSION || gitVersion();
const APP_BUILT_AT = new Date().toISOString();

const useMock = process.env.VITE_MOCK_API === '1';

// A no-auth listener accepts a POST from this origin by port, so the Python side
// names it too (istota.config.VITE_DEV_PORT); strictPort keeps the proxying dev
// server from moving. The mock server has no backend and may take another port.
const DEV_PORT = 5173;

export default defineConfig({
  plugins: [pdfAssets(), sveltekit(), ...(useMock ? [mockApi()] : [])],
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
    __APP_BUILT_AT__: JSON.stringify(APP_BUILT_AT),
  },
  server: useMock
    ? {}
    : {
        port: DEV_PORT,
        strictPort: true,
        proxy: {
          '/istota/api': {
            target: 'http://localhost:8766',
            changeOrigin: true,
          },
        },
      },
});
