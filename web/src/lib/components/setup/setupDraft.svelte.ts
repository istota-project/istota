/**
 * The setup flow's step order and its client copy of the draft.
 *
 * The ids are the server's `SETUP_STEPS` keys (`webui/setup_app.py`), which
 * own the step-to-fields map; `setupDraft.test.ts` holds the two lists equal.
 * Titles are the page's.
 *
 * The draft is the server's: every answer is saved there, credentials come
 * back as `{set: true}` and never as values, and a reload reads it back. This
 * holds the last copy the server sent.
 */
import { getSetupDraft, type SetupDraft, type SetupDraftValue } from '$lib/api';

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

export interface SetupDraftStore {
  readonly values: SetupDraft;
  /** Whether the server holds a value for a credential field. */
  isSet(field: string): boolean;
  /** A plain answer as text, or `''` for a missing one or a credential. */
  text(field: string): string;
  /** Replace the copy with what the server just sent. */
  adopt(draft: SetupDraft): void;
  load(): Promise<void>;
}

export function createSetupDraft(): SetupDraftStore {
  let values = $state<SetupDraft>({});

  function isCredential(value: SetupDraftValue | undefined): value is { set: boolean } {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  }

  return {
    get values() {
      return values;
    },
    isSet(field) {
      const value = values[field];
      return isCredential(value) && value.set;
    },
    text(field) {
      const value = values[field];
      if (value === undefined || isCredential(value) || Array.isArray(value)) return '';
      return String(value);
    },
    adopt(draft) {
      values = draft;
    },
    async load() {
      const { draft } = await getSetupDraft();
      values = draft;
    },
  };
}
