import { loadSetting, saveSetting } from '$lib/stores/persisted';

export type SortDirection = 'ascending' | 'descending';

export interface TableSortOptions<K extends string> {
  /** The sort before anything is clicked or remembered. */
  initial: { key: K; direction: SortDirection };
  /**
   * Which way a column starts when it becomes the sort. Money starts every
   * column descending, since newest first is what its tables are read for;
   * the file table starts `name` ascending and the others descending.
   */
  firstDirection: SortDirection | ((key: K) => SortDirection);
}

export interface TableSort<K extends string> {
  readonly key: K;
  readonly direction: SortDirection;
  readonly descending: boolean;
  /** The column's direction when it is the sort, `null` when it is not. */
  directionOf(key: K): SortDirection | null;
  /** Click a column: the active one flips, a new one starts at its first direction. */
  toggle(key: K): void;
}

/**
 * A table's sort, remembered per browser under `storageKey`.
 *
 * Stored as `{ key, descending }`, the shape both earlier copies (money's
 * tables and the file table) wrote, so a sort saved before this existed is
 * still read. A saved key not in `keys`, or a value that is not an object,
 * falls back to `initial`.
 */
export function createTableSort<K extends string>(
  storageKey: string,
  keys: readonly K[],
  options: TableSortOptions<K>,
): TableSort<K> {
  const saved = loadSetting<{ key?: unknown; descending?: unknown } | null>(storageKey, null);
  const restored =
    saved && typeof saved === 'object' && keys.includes(saved.key as K)
      ? {
          key: saved.key as K,
          direction:
            typeof saved.descending === 'boolean'
              ? saved.descending
                ? ('descending' as const)
                : ('ascending' as const)
              : options.initial.direction,
        }
      : options.initial;

  let key = $state<K>(restored.key);
  let direction = $state<SortDirection>(restored.direction);

  const firstDirection = (k: K): SortDirection =>
    typeof options.firstDirection === 'function'
      ? options.firstDirection(k)
      : options.firstDirection;

  return {
    get key() {
      return key;
    },
    get direction() {
      return direction;
    },
    get descending() {
      return direction === 'descending';
    },
    directionOf(k: K) {
      return k === key ? direction : null;
    },
    toggle(k: K) {
      if (k === key) {
        direction = direction === 'descending' ? 'ascending' : 'descending';
      } else {
        key = k;
        direction = firstDirection(k);
      }
      saveSetting(storageKey, { key, descending: direction === 'descending' });
    },
  };
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
