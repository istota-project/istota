import { readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// Every page is prerendered (`routes/+layout.ts`) and adapter-static has no
// fallback, so a `[param]` route directory fails `npm run build` and could not
// be served on a full load anyway. Selection goes in the query instead.

const ROUTES = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'routes');

function paramDirs(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (!statSync(full).isDirectory()) continue;
    if (entry.includes('[')) out.push(relative(ROUTES, full));
    paramDirs(full, out);
  }
  return out;
}

describe('prerendered routes', () => {
  it('has no route directory with a path parameter', () => {
    expect(paramDirs(ROUTES)).toEqual([]);
  });
});
