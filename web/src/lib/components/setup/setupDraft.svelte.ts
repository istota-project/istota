/**
 * The setup flow's step order and its client copy of the draft.
 *
 * The ids are the server's `SETUP_STEPS` keys (`webui/setup_app.py`), which
 * own the step-to-fields map; `setupDraft.test.ts` holds the two lists equal.
 * Titles are the page's.
 *
 * The draft is the server's: every answer is saved there, credentials come
 * back as `{set: true}` and never as values, and a reload reads it back. This
 * holds the last copy the server sent, plus the answers typed since, which
 * outlive the screen they were typed on: Back keeps them, and the next Next
 * saves them.
 */
import {
  checkSetupConnection,
  getSetupDraft,
  putSetupDraft,
  SetupApiError,
  type SetupCheckKind,
  type SetupCheckResult,
  type SetupDraft,
  type SetupDraftValue,
} from '$lib/api';

export const SETUP_STEPS = [
  { id: 'welcome', title: 'Welcome' },
  { id: 'account', title: 'Your account' },
  { id: 'model', title: 'AI service' },
  { id: 'address', title: 'Web address' },
  { id: 'nextcloud', title: 'Nextcloud' },
  { id: 'connections', title: 'Email and calendar' },
  { id: 'features', title: 'Features' },
  { id: 'review', title: 'Review' },
] as const;

export type SetupStepId = (typeof SETUP_STEPS)[number]['id'];
export type SetupStepInfo = (typeof SETUP_STEPS)[number];

export function isSetupStep(value: string): value is SetupStepId {
  return SETUP_STEPS.some((s) => s.id === value);
}

/** The step's number as the indicator shows it, from 1. */
export function stepPosition(id: SetupStepId): number {
  return SETUP_STEPS.findIndex((s) => s.id === id) + 1;
}

export function stepInfo(id: SetupStepId): SetupStepInfo {
  return SETUP_STEPS[stepPosition(id) - 1];
}

/** The step before or after `id`, or null at either end. */
export function neighbourStep(id: SetupStepId, delta: -1 | 1): SetupStepId | null {
  return SETUP_STEPS[stepPosition(id) - 1 + delta]?.id ?? null;
}

export const DEFAULT_BOT_NAME = 'Istota';

/**
 * The Features screen's rows, by module name as `/state`'s
 * `modules_available` names them; only the modules this image can run are
 * offered. The browser is a compose profile rather than a module.
 */
export const MODULE_FEATURES: Record<string, { title: string; description: string }> = {
  feeds: {
    title: 'Feeds',
    description: 'Follow news sites and blogs, and read them in one place.',
  },
  money: { title: 'Money', description: 'Keep accounts, track spending and send invoices.' },
  health: {
    title: 'Health',
    description: 'Keep body measurements, lab results and medical documents.',
  },
  briefings: {
    title: 'Briefings',
    description: 'Get a summary of your day, your news and your plans on a schedule.',
  },
};
export const BROWSER_PROFILE = 'browser';
/** Under this much memory the browser row warns; it stays on. */
export const BROWSER_MEMORY_WARN_MIB = 4096;

/**
 * Everything on, the browser included (decided 2026-10-12), for any feature
 * nobody has answered yet. The Features screen applies it when it opens, and
 * Review too, so a Finish reached without passing Features keeps the default.
 */
export function applyFeatureDefaults(
  draft: SetupDraftStore,
  modules: { field: string; module: string }[],
): void {
  for (const { field } of modules) draft.setDefault(field, true);
  draft.setDefault('profiles', [BROWSER_PROFILE]);
}

/** A module's row title, falling back to its name for one this page does not know. */
export function moduleTitle(module: string): string {
  return MODULE_FEATURES[module]?.title ?? module.charAt(0).toUpperCase() + module.slice(1);
}

/**
 * The username the Welcome screen suggests from an email address: its local
 * part, lowercased, with what the login's user-id pattern refuses dropped and
 * cut to 32 characters; `admin` when nothing usable is left. The pattern is
 * `webui/auth.valid_new_user_id`'s, and the server checks it again: this is
 * only the suggestion.
 */
export function deriveUsername(email: string): string {
  const local = email.trim().split('@')[0] ?? '';
  const kept = local
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, '')
    .slice(0, 32);
  return /^[a-z0-9]/.test(kept) ? kept : 'admin';
}

/**
 * A screen's Test button: save what the screen holds, then have the server
 * test the draft. A test the server refused to run (fields missing, answers
 * it cannot use) comes back as a failed result plus the fields it named; a
 * lost session is thrown, for the page to send the user back to the code.
 */
export async function runSetupCheck(
  draft: SetupDraftStore,
  kind: SetupCheckKind,
): Promise<{ result: SetupCheckResult; fields: Record<string, string> }> {
  try {
    await draft.save();
    return { result: await checkSetupConnection(kind), fields: {} };
  } catch (e) {
    if (e instanceof SetupApiError && e.status === 401) throw e;
    const fields: Record<string, string> = {};
    if (e instanceof SetupApiError) {
      for (const f of e.fields) if (f.field && !(f.field in fields)) fields[f.field] = f.message;
    }
    const message = e instanceof Error ? e.message : String(e);
    return { result: { ok: false, warning: false, message }, fields };
  }
}

type Answer = string | number | boolean | string[];

export interface SetupDraftStore {
  readonly values: SetupDraft;
  /** Whether the server holds a value for a credential field. */
  isSet(field: string): boolean;
  /** An answer as text: the edit not yet saved, else the saved one; `''`
   *  for a missing answer and for a saved credential, whose value never
   *  comes back. */
  text(field: string): string;
  flag(field: string, fallback: boolean): boolean;
  /** A list answer (the compose profiles); `[]` when there is none. */
  list(field: string): string[];
  /** Whether the field has an answer, saved or not. */
  has(field: string): boolean;
  /** Change an answer; it goes to the server with the next `save`. */
  set(field: string, value: Answer): void;
  /** Answer only a question nobody has answered yet. */
  setDefault(field: string, value: Answer): void;
  /** A typed credential. Empty means "keep what is stored", not "erase it". */
  setSecret(field: string, value: string): void;
  /** Erase a stored credential with the next `save`. */
  clearSecret(field: string): void;
  /** Values the page needs that are no answer (a password's confirmation):
   *  kept while the page is open, never sent. */
  readonly scratch: Record<string, string>;
  /** Replace the copy with what the server just sent. */
  adopt(draft: SetupDraft): void;
  load(): Promise<void>;
  /** Send every unsaved answer. Resolves at once when there is none. */
  save(): Promise<void>;
}

export function createSetupDraft(): SetupDraftStore {
  let values = $state<SetupDraft>({});
  let edits = $state<Record<string, Answer>>({});
  const scratch = $state<Record<string, string>>({});

  function isCredential(value: SetupDraftValue | undefined): value is { set: boolean } {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  }

  function current(field: string): SetupDraftValue | undefined {
    return field in edits ? edits[field] : values[field];
  }

  const store: SetupDraftStore = {
    get values() {
      return values;
    },
    get scratch() {
      return scratch;
    },
    isSet(field) {
      const value = values[field];
      return isCredential(value) && value.set;
    },
    text(field) {
      const value = current(field);
      if (value === undefined || isCredential(value) || Array.isArray(value)) return '';
      return String(value);
    },
    flag(field, fallback) {
      const value = current(field);
      return typeof value === 'boolean' ? value : fallback;
    },
    list(field) {
      const value = current(field);
      return Array.isArray(value) ? [...value] : [];
    },
    has(field) {
      return field in edits || field in values;
    },
    set(field, value) {
      edits[field] = value;
    },
    setDefault(field, value) {
      if (!store.has(field)) edits[field] = value;
    },
    setSecret(field, value) {
      if (value) edits[field] = value;
      else delete edits[field];
    },
    clearSecret(field) {
      edits[field] = '';
    },
    adopt(draft) {
      values = draft;
    },
    async load() {
      const { draft } = await getSetupDraft();
      values = draft;
    },
    async save() {
      const sent = { ...edits };
      if (Object.keys(sent).length === 0) return;
      const { draft } = await putSetupDraft(sent);
      values = draft;
      // An answer changed while the request was out is still unsaved.
      for (const [field, value] of Object.entries(sent)) {
        if (edits[field] === value) delete edits[field];
      }
    },
  };
  return store;
}
