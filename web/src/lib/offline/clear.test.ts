/**
 * Settings → "Clear offline data" (ISSUE-202).
 *
 * The one property to hold is that all three steps run: the worker has to be
 * unregistered *and* its caches deleted *and* the database emptied, because
 * each of them can hold a stale app on its own — a registered worker serves
 * the old document, an undeleted cache is what it serves from, and the
 * database is the transcript. A button that did two of the three would look
 * like it worked and leave the state the user pressed it to escape.
 *
 * The second property is that no step can stop another. A phone with no Cache
 * Storage, a worker that was never registered, a database that will not open:
 * all of them are ordinary here, and a half-cleared state is the thing being
 * escaped.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const db = vi.hoisted(() => ({ clearOffline: vi.fn(async () => {}) }));
vi.mock('./db', () => db);

import { clearOfflineData } from './clear';

function stubWorkers(registrations: { unregister: () => Promise<boolean> }[]) {
  vi.stubGlobal('navigator', {
    serviceWorker: { getRegistrations: async () => registrations },
  });
}

function stubCaches(keys: string[], deleted: string[]) {
  vi.stubGlobal('caches', {
    keys: async () => keys,
    delete: async (key: string) => {
      deleted.push(key);
      return true;
    },
  });
}

beforeEach(() => {
  db.clearOffline.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('clearing the offline data', () => {
  it('unregisters every worker, deletes this app’s caches, and empties the database', async () => {
    const unregistered: boolean[] = [];
    const register = () => ({
      unregister: async () => {
        unregistered.push(true);
        return true;
      },
    });
    stubWorkers([register(), register()]);
    const deleted: string[] = [];
    stubCaches(['istota-v1', 'istota-v2', 'something-else'], deleted);

    const result = await clearOfflineData();

    expect(unregistered).toHaveLength(2);
    // This app's own generations only — deleting storage we did not write is
    // not ours to offer, and the worker's activate step follows the same rule.
    expect(deleted).toEqual(['istota-v1', 'istota-v2']);
    expect(db.clearOffline).toHaveBeenCalledOnce();
    expect(result).toEqual({ workers: 2, caches: 2, database: true });
  });

  it('clears the database even where there is no worker and no cache storage', async () => {
    vi.stubGlobal('navigator', {});
    vi.stubGlobal('caches', undefined);

    const result = await clearOfflineData();

    expect(db.clearOffline).toHaveBeenCalledOnce();
    expect(result).toEqual({ workers: 0, caches: 0, database: true });
  });

  it('carries on when a step throws', async () => {
    vi.stubGlobal('navigator', {
      serviceWorker: {
        getRegistrations: async () => {
          throw new Error('refused');
        },
      },
    });
    vi.stubGlobal('caches', {
      keys: async () => {
        throw new Error('refused');
      },
      delete: async () => true,
    });

    const result = await clearOfflineData();

    expect(db.clearOffline).toHaveBeenCalledOnce();
    expect(result).toEqual({ workers: 0, caches: 0, database: true });
  });

  it('counts an unregister that refused rather than reporting it as done', async () => {
    stubWorkers([
      { unregister: async () => true },
      {
        unregister: async () => {
          throw new Error('refused');
        },
      },
    ]);
    stubCaches([], []);

    const result = await clearOfflineData();

    expect(result.workers).toBe(1);
  });
});
