/**
 * The config page's model pickers, as plain data.
 *
 * A model field is edited with a source (Aliases, or one provider), a model
 * from that source and, where the field takes one, an effort. The model
 * control is `@minot/ui`'s `AutocompleteInput`, which takes strings only, so
 * this reuses the room picker's label-to-value mapping (`chat/modelPicker.ts`)
 * rather than a second one. It has no option groups either: the source select
 * is the grouping, and order within a source is the server's (enabled first,
 * deprecated last).
 *
 * What a field stores is an alias name or a `provider/id` ref, plus `:effort`.
 * A stored value the catalogue does not hold is kept as it is and labelled,
 * so opening and saving the page never changes a value nobody touched.
 */
import type { ConfigCatalogueSource, ConfigModelCatalogue, ConfigModelField } from '$lib/api';
import { EFFORT_LEVELS, type PickerOption } from '$lib/components/chat/modelPicker';

/** The source select's value for the aliases. Not a provider id: ids are
 *  lowercase letters, digits and `_`, starting with a letter. */
export const ALIASES = '@aliases';

/** The last entry of each provider's list: a model id typed by hand. */
export const OTHER_LABEL = 'Other…';

export interface SourceChoice {
  value: string;
  label: string;
}

/** `value` split into its base and a trailing `:effort`, the way the server's
 *  `split_effort` splits it: only on a known level, so `deepseek/r1:free`
 *  keeps its colon. `:high` alone is an effort with no model, which only the
 *  alias editor writes (an effort-only override). */
export function splitEffort(value: string): { base: string; effort: string } {
  const at = value.lastIndexOf(':');
  if (at >= 0) {
    const level = value.slice(at + 1).toLowerCase();
    if ((EFFORT_LEVELS as readonly string[]).includes(level)) {
      return { base: value.slice(0, at), effort: level };
    }
  }
  return { base: value, effort: '' };
}

export function joinEffort(base: string, effort: string): string {
  const trimmed = base.trim();
  if (!trimmed) return '';
  return effort ? `${trimmed}:${effort}` : trimmed;
}

/** The sources a field may name, Aliases first when it takes them. */
export function sourceChoices(
  catalogue: ConfigModelCatalogue,
  field: ConfigModelField,
): SourceChoice[] {
  const out: SourceChoice[] = [];
  if (field.aliases) out.push({ value: ALIASES, label: 'Aliases' });
  for (const id of field.sources) {
    const source = catalogue.sources.find((s) => s.id === id);
    if (source) out.push({ value: id, label: source.label });
  }
  return out;
}

export interface StoredModel {
  /** The source select's value. */
  source: string;
  /** What the model control shows: the alias, the ref, or the stored text. */
  model: string;
  effort: string;
  /** False when nothing in the catalogue answers to the value. */
  known: boolean;
}

/**
 * Where a stored value sits in the pickers. An alias goes to Aliases; a
 * `provider/id` ref to that provider; a bare id that one allowed provider
 * lists is shown as its ref (the value is rewritten only if the field is
 * saved). Anything else stays as written, under the first source, unknown.
 */
export function parseStored(
  value: string,
  catalogue: ConfigModelCatalogue,
  field: ConfigModelField,
): StoredModel {
  const { base, effort } = splitEffort(value.trim());
  const choices = sourceChoices(catalogue, field);
  const first = choices.find((c) => c.value !== ALIASES)?.value ?? choices[0]?.value ?? ALIASES;
  if (!base) return { source: first, model: '', effort, known: true };
  const lower = base.toLowerCase();
  if (field.aliases && catalogue.aliases.some((a) => a.name === lower)) {
    return { source: ALIASES, model: lower, effort, known: true };
  }
  const allowed = catalogue.sources.filter((s) => field.sources.includes(s.id));
  const head = lower.split('/', 1)[0];
  const byProvider = allowed.find((s) => s.id === head);
  if (byProvider) {
    const known = byProvider.models.some((m) => m.ref.toLowerCase() === lower);
    return { source: byProvider.id, model: base, effort, known };
  }
  const matches = allowed.flatMap((s) =>
    s.models.filter((m) => m.ref.slice(s.id.length + 1).toLowerCase() === lower),
  );
  if (matches.length === 1) {
    const ref = matches[0].ref;
    return { source: ref.split('/', 1)[0], model: ref, effort, known: true };
  }
  return { source: first, model: base, effort, known: false };
}

/** The model control's options for one source, `Other…` last for a provider. */
export function modelOptions(catalogue: ConfigModelCatalogue, source: string): PickerOption[] {
  if (source === ALIASES) {
    return catalogue.aliases.map((a) => ({
      label: `${a.name} → ${a.target ?? "the brain's own model"}`,
      value: a.name,
      efforts: a.efforts,
    }));
  }
  const found = catalogue.sources.find((s) => s.id === source);
  if (!found) return [];
  return [...providerOptions(found), { label: OTHER_LABEL, value: '', efforts: null }];
}

function providerOptions(source: ConfigCatalogueSource): PickerOption[] {
  const mixed = source.models.some((m) => m.enabled) && source.models.some((m) => !m.enabled);
  return source.models.map((m) => {
    const id = m.ref.slice(source.id.length + 1);
    const name = m.name && m.name !== id ? ` (${m.name})` : '';
    const marks = [mixed && m.enabled ? 'enabled' : '', m.deprecated ? 'deprecated' : '']
      .filter(Boolean)
      .map((mark) => ` · ${mark}`)
      .join('');
    return { label: `${m.ref}${name}${marks}`, value: m.ref, efforts: m.efforts };
  });
}

/** The value typed under `Other…`: always a ref under the chosen provider. */
export function otherValue(source: string, typed: string): string {
  const id = typed.trim().replace(new RegExp(`^${source}/`, 'i'), '');
  return id ? `${source}/${id}` : '';
}

/** A line under the model control saying how fresh a provider's list is. */
export function sourceNote(catalogue: ConfigModelCatalogue, source: string): string | null {
  if (source === ALIASES) return null;
  const found = catalogue.sources.find((s) => s.id === source);
  if (!found) return null;
  if (found.last_error) return `The last fetch of this list failed: ${found.last_error}.`;
  if (!found.models.length) return 'No models listed yet. Use Other… to type an id.';
  return null;
}

// --- [models] enabled --------------------------------------------------------

function globToRegExp(glob: string): RegExp {
  let out = '';
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === '*') out += '.*';
    else if (c === '?') out += '.';
    else if (c === '[') {
      const end = glob.indexOf(']', i + 2);
      if (end < 0) out += '\\[';
      else {
        let body = glob.slice(i + 1, end);
        if (body.startsWith('!')) body = '^' + body.slice(1);
        out += `[${body.replace(/\\/g, '\\\\')}]`;
        i = end;
      }
    } else out += c.replace(/[.+^${}()|\\/]/g, '\\$&');
  }
  return new RegExp(`^${out}$`, 's');
}

/**
 * How many catalogue models an `[models] enabled` entry matches, for an entry
 * typed on this page. The server's `brain._resolve.enabled_refs` is the rule
 * (case-insensitive `fnmatch` over refs, `:effort` ignored, a deprecated model
 * matched only by its exact ref) and its counts for stored entries arrive as
 * `pattern_counts`; this is the same rule for text not yet saved.
 */
export function patternCount(catalogue: ConfigModelCatalogue, pattern: string): number {
  const glob = splitEffort(pattern.trim()).base.toLowerCase();
  if (!glob) return 0;
  const re = globToRegExp(glob);
  let count = 0;
  for (const source of catalogue.sources) {
    for (const model of source.models) {
      const ref = model.ref.toLowerCase();
      if (model.deprecated ? ref === glob : re.test(ref)) count += 1;
    }
  }
  return count;
}
