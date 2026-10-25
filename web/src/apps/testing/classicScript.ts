// Loads a starter app's pure-logic script the way its document runs it: a
// classic script assigning to `globalThis`, here in a fresh context of its
// own, so a test sees only what the script itself defines. Test-only; no app
// document includes it.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { createContext, runInContext } from 'node:vm';

const STARTER = join(process.cwd(), 'src', 'apps', 'starter');

/** Run `starter/<app>/<file>` in a new context and return that context's globals. */
export function loadStarterScript(app: string, file: string): Record<string, unknown> {
  const path = join(STARTER, app, file);
  const context = createContext({});
  runInContext(readFileSync(path, 'utf8'), context, { filename: path });
  return context as Record<string, unknown>;
}
