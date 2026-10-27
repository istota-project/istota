/**
 * The Configuration page's `[models.aliases]` editor, as plain data, on the
 * pattern of `servarrApps.ts`.
 *
 * One row per alias (the tiers always, then the operator's own) and in it one
 * target per namespace: `anthropic` for the CLI brains, `openai_compat` for
 * native. A target is a selection (a ref, a family shortcut or another alias)
 * plus an effort; a target with only an effort keeps the alias's default
 * model. The whole table goes back as one `set_toml` op on `models.aliases`,
 * the op the table editor sends, so the edit pipeline is unchanged.
 */
import type { ConfigOp } from '$lib/api';
import { emptyDraft, withOp, type ConfigDraft } from './changeSet';
import { splitEffort } from './modelField';

export const ALIAS_NAMESPACES = ['anthropic', 'openai_compat'] as const;
export type AliasNamespace = (typeof ALIAS_NAMESPACES)[number];

export const NAMESPACE_LABELS: Record<AliasNamespace, string> = {
  anthropic: 'Claude Code',
  openai_compat: 'Native',
};

/** The tiers every deployment has, shown even when the file overrides none. */
export const TIERS = ['fast', 'general', 'smart'] as const;

export interface AliasTarget {
  /** A selection: a ref, a family shortcut or another alias; '' for none. */
  model: string;
  effort: string;
}

export interface AliasRow {
  name: string;
  targets: Record<AliasNamespace, AliasTarget>;
  /** `portable = true`, kept as it was: a cross-brain intent. */
  portable: boolean;
  /** A flat string alias, applying to every namespace. Kept flat until a
   *  namespace is edited apart from the other. */
  flat: boolean;
}

export type AliasesForm = AliasRow[];

const PATH = ['models', 'aliases'];

function target(raw: unknown): AliasTarget {
  if (typeof raw === 'string') {
    const { base, effort } = splitEffort(raw);
    return { model: base, effort };
  }
  if (raw && typeof raw === 'object') {
    const table = raw as Record<string, unknown>;
    const model = typeof table.model === 'string' ? table.model : '';
    const split = splitEffort(model);
    const effort = typeof table.effort === 'string' ? table.effort : split.effort;
    return { model: split.base, effort };
  }
  return { model: '', effort: '' };
}

/** The form for the table the file holds (`field.value`, raw). */
export function aliasesForm(value: unknown): AliasesForm {
  const table = value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
  const names = [
    ...TIERS,
    ...Object.keys(table)
      .filter((n) => !(TIERS as readonly string[]).includes(n))
      .sort(),
  ];
  return names.map((name) => {
    const raw = table[name];
    if (typeof raw === 'string') {
      const t = target(raw);
      return {
        name,
        targets: { anthropic: t, openai_compat: { ...t } },
        portable: false,
        flat: true,
      };
    }
    const entry = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
    return {
      name,
      targets: { anthropic: target(entry.anthropic), openai_compat: target(entry.openai_compat) },
      portable: entry.portable === true,
      flat: false,
    };
  });
}

/** A target as the one string the model picker edits: `model:effort`, or
 *  `:effort` for an effort-only override. */
export function targetValue(t: AliasTarget): string {
  if (t.model.trim()) return t.effort ? `${t.model.trim()}:${t.effort}` : t.model.trim();
  return t.effort ? `:${t.effort}` : '';
}

export function targetOf(value: string): AliasTarget {
  const { base, effort } = splitEffort(value.trim());
  return { model: base, effort };
}

/** `form` with an alias added, or unchanged when the name is taken or not
 *  an alias name (lowercase letters, digits, `-` and `_`). */
export function withAlias(form: AliasesForm, name: string): AliasesForm {
  const clean = name.trim().toLowerCase();
  if (!/^[a-z][a-z0-9_-]*$/.test(clean) || form.some((row) => row.name === clean)) return form;
  return [
    ...form,
    {
      name: clean,
      targets: { anthropic: { model: '', effort: '' }, openai_compat: { model: '', effort: '' } },
      portable: false,
      flat: false,
    },
  ];
}

/** `form` with one namespace's target of one alias replaced. */
export function withTarget(
  form: AliasesForm,
  name: string,
  namespace: AliasNamespace,
  next: AliasTarget,
): AliasesForm {
  return form.map((row) =>
    row.name === name
      ? { ...row, flat: false, targets: { ...row.targets, [namespace]: next } }
      : row,
  );
}

function quote(text: string): string {
  return `"${text
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/[\u0000-\u001f]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)}"`;
}

function key(name: string): string {
  return /^[A-Za-z0-9_-]+$/.test(name) ? name : quote(name);
}

function inlineTarget(t: AliasTarget): string | null {
  const model = t.model.trim();
  if (!model && !t.effort) return null;
  if (!t.effort) return quote(model);
  const parts = [];
  if (model) parts.push(`model = ${quote(model)}`);
  parts.push(`effort = ${quote(t.effort)}`);
  return `{ ${parts.join(', ')} }`;
}

function rowToml(row: AliasRow): string | null {
  if (row.flat) {
    const t = row.targets.anthropic;
    const model = t.model.trim();
    return model ? quote(t.effort ? `${model}:${t.effort}` : model) : null;
  }
  const parts: string[] = [];
  for (const ns of ALIAS_NAMESPACES) {
    const value = inlineTarget(row.targets[ns]);
    if (value) parts.push(`${ns} = ${value}`);
  }
  if (!parts.length) return null;
  if (row.portable) parts.push('portable = true');
  return `{ ${parts.join(', ')} }`;
}

/** The table as the TOML snippet a `set_toml` op carries: its one top-level
 *  key is the field's name. An alias with no target is left out, which
 *  returns it to its default. */
export function aliasesToml(form: AliasesForm): string {
  const lines = ['[aliases]'];
  for (const row of form) {
    const value = rowToml(row);
    if (value) lines.push(`${key(row.name)} = ${value}`);
  }
  return lines.join('\n') + '\n';
}

function same(a: AliasesForm, b: AliasesForm): boolean {
  return aliasesToml(a) === aliasesToml(b);
}

/** The change `form` makes to what `initial` was built from. */
export function aliasesDraft(form: AliasesForm, initial: AliasesForm): ConfigDraft {
  if (same(form, initial)) return emptyDraft();
  const op: ConfigOp = { op: 'set_toml', path: PATH, toml: aliasesToml(form) };
  return withOp(emptyDraft(), op);
}
