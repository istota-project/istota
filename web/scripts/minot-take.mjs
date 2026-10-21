#!/usr/bin/env node
// `npm run minot:take -- <version>`: install a published @minot/ui at an exact
// version and print the commit subject to use. Run the web checks without
// MINOT_DEV before committing, and commit the bump with the change that needs it.

import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const VERSION = /^\d+\.\d+\.\d+$/;

/** The indent a JSON file was written with, so npm's rewrite can be put back. */
export function indentOf(source) {
  const line = source.split('\n').find((l) => /^\s+"/.test(l));
  return line ? line.match(/^\s+/)[0] : '  ';
}

/**
 * The sha512 integrity for a tarball whose lockfile entry carries `current`.
 * GitLab's registry publishes only a sha1, so npm writes that; the bytes are
 * checked against it first, so the stronger hash names what npm verified.
 */
export function strongIntegrity(bytes, current) {
  const [algo, digest] = current.split(/-(.*)/s);
  if (algo === 'sha512') return current;
  if (algo !== 'sha1') throw new Error(`unexpected integrity ${current}`);
  const actual = createHash('sha1').update(bytes).digest('base64');
  if (actual !== digest) throw new Error(`tarball does not match ${current}`);
  return `sha512-${createHash('sha512').update(bytes).digest('base64')}`;
}

/** The commit subject for taking `version`. */
export function subject(version) {
  return `web: take @minot/ui ${version} for <reason>`;
}

async function main(argv) {
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
  const entry = lock.packages['node_modules/@minot/ui'];
  const tarball = await fetch(entry.resolved);
  if (!tarball.ok) {
    console.error(`could not fetch ${entry.resolved}: ${tarball.status}`);
    return 1;
  }
  entry.integrity = strongIntegrity(Buffer.from(await tarball.arrayBuffer()), entry.integrity);
  writeFileSync(lockPath, JSON.stringify(lock, null, indent) + '\n');

  console.log(
    `\n@minot/ui ${version} installed. Run the web checks without MINOT_DEV, then commit:`,
  );
  console.log(`  ${subject(version)}`);
  return 0;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exit(await main(process.argv.slice(2)));
}
