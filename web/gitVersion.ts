import { execSync } from 'node:child_process';
import type { ExecSyncOptionsWithStringEncoding } from 'node:child_process';

/** The `execSync` shape this module uses, so a test can drive it without git. */
export type Exec = (command: string, options: ExecSyncOptionsWithStringEncoding) => string;

// stderr is dropped rather than inherited: the catch below already owns the
// outcome, and a build with no repository in it — `node:20-slim` carries no
// git, and the web build stage's context excludes `.git` — otherwise prints
// the shell's own `git: not found` twice per build, once per vite environment,
// which reads as a build failure in the log.
const SILENT: ExecSyncOptionsWithStringEncoding = {
  encoding: 'utf8',
  stdio: ['ignore', 'pipe', 'ignore'],
};

/** The short HEAD sha, `-dirty` if web/ has uncommitted changes, else `unknown`. */
export function gitVersion(exec: Exec = execSync): string {
  try {
    const sha = exec('git rev-parse --short HEAD', SILENT).trim();
    // Scope the dirty check to web/ — runtime config files under config/ and
    // config/users/ are expected to drift on deployed hosts.
    const dirty = exec('git status --porcelain -- .', SILENT).trim().length > 0;
    return dirty ? `${sha}-dirty` : sha;
  } catch {
    return 'unknown';
  }
}
