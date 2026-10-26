// The apps escape tier: `npm --prefix web run test:apps-escape`. Discretionary,
// never part of `npm run test`. Browsers: `npx --prefix web playwright install
// chromium firefox webkit`. Fixtures, the reporting convention and the negative
// control are described in README.md beside this file.
import { defineConfig, devices } from '@playwright/test';
import { PORT, RECORDER_PORT } from './fixtures';

export default defineConfig({
  testDir: import.meta.dirname,
  testMatch: 'escape.spec.ts',
  globalSetup: './global-setup.ts',
  outputDir: './test-results',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: { baseURL: `http://127.0.0.1:${PORT}` },
  webServer: [
    {
      command: `npx vite --config tests-apps-escape/vite.config.ts`,
      cwd: `${import.meta.dirname}/..`,
      url: `http://127.0.0.1:${PORT}/`,
      reuseExistingServer: false,
      env: { APPS_ESCAPE_NO_CSP: process.env.APPS_ESCAPE_NO_CSP ?? '' },
    },
    {
      command: `node tests-apps-escape/recorder.mjs`,
      cwd: `${import.meta.dirname}/..`,
      url: `http://127.0.0.1:${RECORDER_PORT}/log`,
      reuseExistingServer: false,
      env: { APPS_ESCAPE_RECORDER_PORT: String(RECORDER_PORT) },
    },
  ],
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
