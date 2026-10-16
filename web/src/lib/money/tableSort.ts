import { loadSetting, saveSetting } from '$lib/stores/persisted';

/** Which column a money table is sorted by, and which way. */
export interface TableSort<K extends string> {
  key: K;
  descending: boolean;
}

/**
 * The sort after clicking `key`: the same column flips, a new one starts
 * descending, since newest first is what these tables are read for.
 */
export function nextSort<K extends string>(current: TableSort<K>, key: K): TableSort<K> {
  return key === current.key ? { key, descending: !current.descending } : { key, descending: true };
}

// Numeric collation, so INV-000010 sorts after INV-000009 and a client's own
// prefixed numbering sorts by its digits rather than character by character.
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

/**
 * Sort `rows` by the columns `valuesOf` returns, compared in order with the
 * sort's direction. An empty first value (an entry with no invoice) sorts last
 * either way, so flipping the direction never buries the rows that have one.
 */
export function sortRows<T>(
  rows: readonly T[],
  descending: boolean,
  valuesOf: (row: T) => string[],
): T[] {
  const keyed = rows.map((row) => ({ row, values: valuesOf(row) }));
  keyed.sort((a, b) => {
    const aEmpty = !a.values[0];
    const bEmpty = !b.values[0];
    if (aEmpty !== bEmpty) return aEmpty ? 1 : -1;
    for (let i = 0; i < a.values.length; i++) {
      const order = collator.compare(a.values[i] ?? '', b.values[i] ?? '');
      if (order) return descending ? -order : order;
    }
    return 0;
  });
  return keyed.map((k) => k.row);
}

/** A remembered sort for one table, falling back to `fallback` on anything unrecognised. */
export function loadSort<K extends string>(
  storageKey: string,
  keys: readonly K[],
  fallback: TableSort<K>,
): TableSort<K> {
  const saved = loadSetting<Partial<TableSort<string>> | null>(storageKey, null);
  if (!saved || !keys.includes(saved.key as K)) return fallback;
  return { key: saved.key as K, descending: saved.descending !== false };
}

export function saveSort<K extends string>(storageKey: string, sort: TableSort<K>): void {
  saveSetting(storageKey, sort);
}
