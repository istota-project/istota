import { changedFields } from './changedFields';

/**
 * Whether a form holds unsaved edits, by comparing what it holds now with a
 * JSON snapshot taken when it was loaded or last saved. Nine forms wrote this
 * out by hand, each with its own name for the snapshot and its own guard for
 * "not loaded yet".
 */
export interface ChangeTracker<T> {
  /** The value differs from the saved snapshot. */
  readonly dirty: boolean;
  /** The value as of the last save, read back from the snapshot. */
  readonly saved: T;
  /** Take what the form holds now as saved. */
  mark(): void;
  /**
   * The fields of `of` (the tracked value when omitted) that differ from the
   * saved snapshot, by `changedFields`' rule: a structural compare,
   * field by field. Empty for a value that is not an object.
   */
  changed(of?: Partial<NonNullable<T>>): Partial<NonNullable<T>>;
}

export interface Draft<T> extends ChangeTracker<T> {
  /** The edited record, deeply reactive. Bind its fields or assign it whole. */
  value: T;
  /** Load `next` as the clean value, or with no argument take the current value as saved. */
  reset(next?: T): void;
}

/**
 * Tracks a value read from state the caller already holds, for a form whose
 * saved shape is computed from several fields (a payload, or the visible half
 * of a two-mode input) rather than held as one record. The snapshot is taken
 * when the tracker is created, so the form is clean until it changes.
 */
export function trackChanges<T>(read: () => T): ChangeTracker<T> {
  let snapshot = $state(JSON.stringify(read()));
  const dirty = $derived(JSON.stringify(read()) !== snapshot);
  return {
    get dirty() {
      return dirty;
    },
    get saved() {
      return JSON.parse(snapshot) as T;
    },
    mark() {
      snapshot = JSON.stringify(read());
    },
    changed(of) {
      const current = of ?? read();
      if (current === null || typeof current !== 'object') return {};
      return changedFields(current, snapshot) as Partial<NonNullable<T>>;
    },
  };
}

/** A form's editable copy of a record, and whether it differs from the saved one. */
export function createDraft<T>(initial: T): Draft<T> {
  // A copy, so a caller still holding the record it passed cannot edit the
  // draft behind its back.
  let value = $state($state.snapshot(initial) as T);
  const tracker = trackChanges(() => value);
  return {
    get value() {
      return value;
    },
    set value(next: T) {
      value = next;
    },
    get dirty() {
      return tracker.dirty;
    },
    get saved() {
      return tracker.saved;
    },
    mark: tracker.mark,
    changed: tracker.changed,
    reset(next?: T) {
      if (next !== undefined) value = $state.snapshot(next) as T;
      tracker.mark();
    },
  };
}
