#!/usr/bin/env node
// `npm run minot:take -- <version>`: install a published @minot/ui at an exact
// version and print the commit subject to use. Run the web checks without
// MINOT_DEV before committing, and commit the bump with the change that needs it.

import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const VERSION = /^\d+\.\d+\.\d+$/;

/** The indent a JSON file was written with, so npm's rewrite can be put back. */
export function indentOf(source) {
  const line = source.split('\n').find((l) => /^\s+"/.test(l));
  return line ? line.match(/^\s+/)[0] : '  ';
}

/** The commit subject for taking `version`. */
export function subject(version) {
  return `web: take @minot/ui ${version} for <reason>`;
}

function main(argv) {
  const version = argv[0];
  if (!version || !VERSION.test(version)) {
    console.error('usage: npm run minot:take -- <version>   (an exact x.y.z, no range)');
    return 2;
  }
  const web = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const lockPath = resolve(web, 'package-lock.json');
  // npm rewrites the lockfile with two spaces; this one is kept with tabs, and
  // a whole-file reindent in every bump would bury the change it carries.
  const indent = indentOf(readFileSync(lockPath, 'utf8'));

  const npm = spawnSync('npm', ['install', `@minot/ui@${version}`, '--save-exact'], {
    cwd: web,
    stdio: 'inherit',
  });
  if (npm.status !== 0) return npm.status ?? 1;

  const lock = JSON.parse(readFileSync(lockPath, 'utf8'));
  writeFileSync(lockPath, JSON.stringify(lock, null, indent) + '\n');

  console.log(
    `\n@minot/ui ${version} installed. Run the web checks without MINOT_DEV, then commit:`,
  );
  console.log(`  ${subject(version)}`);
  return 0;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exit(main(process.argv.slice(2)));
}
