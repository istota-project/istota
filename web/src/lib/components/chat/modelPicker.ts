// The room-settings model picker's logic, as plain data so it can be tested
// without the modal. The control is `@minot/ui`'s `AutocompleteInput`, which
// takes a flat list of strings: it has no option groups and no value apart from
// the label. So the grouping is the order (aliases, then models by provider,
// enabled first in each), the enabled mark is part of the label, and a picked
// label is mapped back to the value it stands for.

import type { ChatModel, ChatModelAlias, ChatProvider, ChatRoomModel } from '$lib/api';

export interface ModelChoices {
  aliases: ChatModelAlias[];
  models: ChatModel[];
  /** What an unpinned room runs, or null when the backend picks. */
  defaultModel: string | null;
  /** The room's stored model as this viewer sees it, or null when none. */
  roomModel: ChatRoomModel | null;
  /** The providers a native room may be moved to; empty for anyone who may
   *  not choose one (D22), and for a room that is not native. */
  providers?: ChatProvider[];
  /** The room's stored provider; null is native's default. */
  roomProvider?: string | null;
}

export interface PickerOption {
  /** What the menu shows, unique across the list. */
  label: string;
  /** What a pick stores: an alias name or a `provider/id` ref. */
  value: string;
  /** The levels its model takes; null when not known. */
  efforts: string[] | null;
}

export const EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'] as const;

const ENABLED_MARK = ' · enabled';

/** The picker's options: aliases first, then the models in the server's order.
 *  The enabled mark appears only on a list that mixes enabled and other models,
 *  which is an admin's; on anyone else's every model is enabled and the mark
 *  would say nothing. */
export function pickerOptions(
  choices: ModelChoices,
  provider: string | null = null,
): PickerOption[] {
  const out: PickerOption[] = [];
  for (const a of choices.aliases) {
    out.push({
      label: `${a.alias} → ${a.target ?? "the brain's own model"}`,
      value: a.alias,
      efforts: a.efforts ?? null,
    });
  }
  // With a provider chosen, only its models: a room's model is an id within
  // its provider (D22), and the admin's provider select fills the list.
  const models = provider ? choices.models.filter((m) => m.provider === provider) : choices.models;
  const mixed = models.some((m) => m.enabled) && models.some((m) => !m.enabled);
  for (const m of models) {
    const id = m.ref.slice(m.provider.length + 1);
    const name = m.name && m.name !== id ? ` (${m.name})` : '';
    out.push({
      label: `${m.ref}${name}${mixed && m.enabled ? ENABLED_MARK : ''}`,
      value: m.ref,
      efforts: m.efforts,
    });
  }
  return out;
}

/** The provider select's options: native's default first, as the empty value
 *  a room with no provider of its own holds, then the other listed ones. */
export function providerOptions(choices: ModelChoices): { value: string; label: string }[] {
  const out: { value: string; label: string }[] = [];
  for (const p of choices.providers ?? []) {
    if (p.default) out.unshift({ value: '', label: `${p.id} (default)` });
    else out.push({ value: p.id, label: p.id });
  }
  return out;
}

/** The provider a selection runs on: the chosen one, else native's default. */
export function effectiveProvider(choices: ModelChoices, selected: string): string | null {
  if (selected) return selected;
  return (choices.providers ?? []).find((p) => p.default)?.id ?? null;
}

/** What a room on the selected provider runs with no model pinned: the
 *  server's answer for the room's own provider, the provider's own default
 *  model for another one. */
export function defaultModelFor(
  choices: ModelChoices,
  selected: string,
  stored: string,
): string | null {
  if (selected === stored) return choices.defaultModel;
  const provider = effectiveProvider(choices, selected);
  return (choices.providers ?? []).find((p) => p.id === provider)?.default_model ?? null;
}

/** Whether a value can stay when the room moves to ``provider``: an alias
 *  stays (the server says whether it has a target there), a ref only when it
 *  is that provider's, and a stored id only while the provider is unchanged. */
export function keepsOnProvider(
  options: PickerOption[],
  value: string,
  provider: string | null,
): boolean {
  if (!value) return true;
  const option = options.find((o) => o.value === value);
  if (option && !value.includes('/')) return true;
  return !!provider && value.startsWith(`${provider}/`);
}

/** The value the field's text stands for: an option's value when the text is
 *  its label, else the text itself, trimmed. Empty is the room's default. */
export function valueOf(options: PickerOption[], text: string): string {
  const trimmed = text.trim();
  const picked = options.find((o) => o.label === trimmed);
  return picked ? picked.value : trimmed;
}

/** The menu's filter. A field holding a picked value, or the room's stored
 *  one, shows the whole list on focus, so choosing again does not start from a
 *  one-item or empty menu; anything else filters by substring of the label. */
export function filterOptions(options: PickerOption[], stored: string | null = null) {
  return (labels: string[], query: string): string[] => {
    const q = query.trim().toLowerCase();
    if (!q || (stored && stored.toLowerCase() === q)) return labels;
    if (options.some((o) => o.value.toLowerCase() === q)) return labels;
    return labels.filter((l) => l.toLowerCase().includes(q));
  };
}

/** The effort levels a value's model takes; null when not known, which offers
 *  every level and leaves the clamp to the server (D7). */
export function effortsFor(
  options: PickerOption[],
  roomModel: ChatRoomModel | null,
  value: string,
): string[] | null {
  if (!value) return null;
  const option = options.find((o) => o.value === value);
  if (option) return option.efforts;
  if (roomModel && roomModel.value === value) return roomModel.efforts;
  return null;
}

/** The effort select's options for a model's levels. A stored level the model
 *  does not take stays listed, marked, so the select never shows a value it
 *  does not offer; the server clamps it at each task. */
export function effortOptions(
  efforts: string[] | null,
  current: string,
): { value: string; label: string }[] {
  const levels = efforts ?? [...EFFORT_LEVELS];
  const out = [{ value: '', label: 'Default effort' }];
  for (const level of levels) out.push({ value: level, label: level });
  if (current && !levels.includes(current)) {
    out.push({ value: current, label: `${current} (this model takes ${clampedNote(levels)})` });
  }
  return out;
}

function clampedNote(levels: string[]): string {
  return levels.length ? `up to ${levels[levels.length - 1]}` : 'no effort';
}

/** A refused pick, as the room PATCH answers it (422). */
export interface ModelRefusal {
  error: string;
  code: string;
  candidates: string[];
}

/** One sentence per refusal code, for the field; the server's own wording
 *  when the code is one this client does not know. */
export function refusalMessage(refusal: ModelRefusal): string {
  switch (refusal.code) {
    case 'ambiguous':
      return 'More than one model matches that name.';
    case 'model_not_enabled':
      return 'That model is not one you can pick.';
    case 'model_not_runnable':
      return "This room's brain cannot run that model.";
    case 'no_tools':
      return 'That model cannot use tools, which a room needs.';
    case 'provider_admin_only':
      return 'That model runs on another provider, and only an admin can change the provider.';
    case 'no_default_model':
      return 'That provider has no default model. Pick a model for the room too.';
    case 'unknown':
      return refusal.candidates.length
        ? 'No model has that name.'
        : refusal.error || 'No model has that name.';
    default:
      return refusal.error || 'That model was refused.';
  }
}

/** The PATCH's refusal body, when a caught error carries one. */
export function modelRefusalOf(e: unknown): ModelRefusal | null {
  if (!e || typeof e !== 'object') return null;
  const { status, body } = e as { status?: unknown; body?: unknown };
  if (status !== 422 || !body || typeof body !== 'object') return null;
  const { error, code, candidates } = body as Record<string, unknown>;
  if (typeof code !== 'string') return null;
  return {
    error: typeof error === 'string' ? error : '',
    code,
    candidates: Array.isArray(candidates)
      ? candidates.filter((c): c is string => typeof c === 'string')
      : [],
  };
}
