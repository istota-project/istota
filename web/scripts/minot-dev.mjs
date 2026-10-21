#!/usr/bin/env node
// `npm run minot:dev`: the dev server against a minot checkout's source, so a
// change there shows here with hot reload. MINOT_DEV names the checkout and
// defaults to ../../minot from web/ (the checkout beside this repository's).
// See vite.config.ts for the alias it turns on.

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const web = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const checkout = resolve(web, process.env.MINOT_DEV || '../../minot');

if (!existsSync(resolve(checkout, 'src/lib'))) {
  console.error(`minot:dev: no minot checkout at ${checkout}`);
  console.error('Set MINOT_DEV to the path of one, e.g. MINOT_DEV=~/Repos/minot npm run minot:dev');
  process.exit(1);
}

const vite = spawn('npx', ['vite', 'dev', ...process.argv.slice(2)], {
  cwd: web,
  env: { ...process.env, MINOT_DEV: checkout },
  stdio: 'inherit',
});
vite.on('exit', (code, signal) => process.exit(signal ? 1 : (code ?? 1)));
