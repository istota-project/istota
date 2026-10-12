<script lang="ts">
  import type { AdminConfigField, ConfigOp } from '$lib/api';
  import { Badge, Button, Chip, Field, Input, Select, TextArea } from '$lib/components/ui';
  import ConfigSecretField from './SecretField.svelte';
  import TableEditor from './TableEditor.svelte';
  import { displayValue, formatValue, opFor, parseList, parseNumber } from './changeSet';

  interface Props {
    field: AdminConfigField;
    /** This field's pending op, if any. */
    op: ConfigOp | undefined;
    /** A credential field's pending value: `""` clears, undefined is unchanged. */
    secretPending?: string;
    /** False when nothing on the page can be changed (no applier); the banner
     *  says why, so the field shows its value and no reason of its own. */
    editable: boolean;
    /** A problem the last check reported against this key. */
    problem?: string;
    onOp: (op: ConfigOp | null, path: string[]) => void;
    onSecret: (name: string, value: string | null) => void;
  }

  let { field, op, secretPending, editable, problem, onOp, onSecret }: Props = $props();

  // What the admin typed into a number or list input, held while it does not
  // parse (a number) or while it is being typed (a list, whose blank lines
  // parsing drops). Cleared by Undo and reset, which change the value under it.
  let draftText = $state<string | null>(null);
  let inputError = $state('');

  let kind = $derived(editable ? field.editor : 'readonly');
  let hint = $derived(field.doc ?? undefined);
  let showMeta = $derived(editable && field.editor !== 'readonly' && field.editor !== 'secret');
  let shown = $derived(displayValue(field, op));
  let pendingSet = $derived(op?.op === 'set' || op?.op === 'set_toml');
  let pendingUnset = $derived(op?.op === 'unset');
  let numericList = $derived(
    Array.isArray(field.value)
      ? field.value.length > 0 && field.value.every((v) => typeof v === 'number')
      : false,
  );
  let badge = $derived(
    kind === 'readonly' && field.read_only_reason
      ? 'read-only'
      : field.risky
        ? 'needs confirmation'
        : undefined,
  );
  let selectOptions = $derived(
    (field.choices ?? []).map((choice) => ({
      value: choice,
      label: choice === '' ? '(none)' : choice,
    })),
  );

  function emit(value: unknown) {
    onOp(opFor(field, value), field.path);
  }

  function onNumber(text: string) {
    const parsed = parseNumber(text, field.editor === 'float' ? 'float' : 'int');
    if (parsed === null) {
      draftText = text;
      inputError = field.editor === 'float' ? 'Enter a number.' : 'Enter a whole number.';
      onOp(null, field.path);
      return;
    }
    draftText = null;
    inputError = '';
    emit(parsed);
  }

  function onList(text: string) {
    draftText = text;
    const parsed = parseList(text, numericList);
    if (parsed === null) {
      inputError = 'Every line has to be a number.';
      onOp(null, field.path);
      return;
    }
    inputError = '';
    emit(parsed);
  }

  function toggleChoice(choice: string) {
    const current = Array.isArray(shown) ? (shown as string[]) : [];
    const next = current.includes(choice)
      ? current.filter((c) => c !== choice)
      : (field.multi_choices ?? []).filter((c) => c === choice || current.includes(c));
    emit(next);
  }

  function undo() {
    draftText = null;
    inputError = '';
    onOp(null, field.path);
  }

  function reset() {
    draftText = null;
    inputError = '';
    onOp({ op: 'unset', path: field.path }, field.path);
  }
</script>

<div class="config-field" class:pending={!!op || secretPending !== undefined} title={field.key}>
  <div class="field-row" class:inline-meta={showMeta && kind !== 'table'}>
    {#if kind === 'secret' && field.credential}
      <ConfigSecretField
        label={field.name}
        configured={field.set}
        {hint}
        inline
        pending={secretPending}
        onChange={(value) => onSecret(field.credential!.name, value)}
      />
    {:else if kind === 'bool'}
      <Field label={field.name} {hint} {badge} inline checkbox error={problem}>
        <input
          type="checkbox"
          checked={shown === true}
          onchange={(e) => emit((e.currentTarget as HTMLInputElement).checked)}
        />
      </Field>
    {:else if kind === 'int' || kind === 'float'}
      <Field label={field.name} {hint} {badge} inline error={inputError || problem}>
        <Input
          type="number"
          step={kind === 'float' ? 'any' : '1'}
          value={draftText ?? (shown === null || shown === undefined ? '' : String(shown))}
          invalid={!!inputError}
          oninput={(e: Event) => onNumber((e.currentTarget as HTMLInputElement).value)}
        />
      </Field>
    {:else if kind === 'str' || kind === 'path'}
      <Field label={field.name} {hint} {badge} inline error={problem}>
        <Input
          value={shown === null || shown === undefined ? '' : String(shown)}
          monospace={kind === 'path'}
          oninput={(e: Event) => emit((e.currentTarget as HTMLInputElement).value)}
        />
      </Field>
    {:else if kind === 'list'}
      <Field label={field.name} {hint} {badge} inline error={inputError || problem} wide>
        <TextArea
          monospace
          rows={3}
          value={draftText ?? (Array.isArray(shown) ? shown.map(String).join('\n') : '')}
          invalid={!!inputError}
          placeholder="One per line"
          oninput={(e: Event) => onList((e.currentTarget as HTMLTextAreaElement).value)}
        />
      </Field>
    {:else if kind === 'choice'}
      <Field label={field.name} {hint} {badge} inline error={problem} labelled={false}>
        <Select
          value={String(shown ?? '')}
          options={selectOptions}
          ariaLabel={field.name}
          fullWidth
          onValueChange={(value) => emit(value)}
        />
      </Field>
    {:else if kind === 'multi_choice'}
      <Field label={field.name} {hint} {badge} inline error={problem} labelled={false}>
        <div class="choices">
          {#each field.multi_choices ?? [] as choice (choice)}
            {@const on = Array.isArray(shown) && (shown as string[]).includes(choice)}
            <Chip checked={on} aria-pressed={on} onclick={() => toggleChoice(choice)}>{choice}</Chip
            >
          {/each}
        </div>
      </Field>
    {:else if kind === 'table' && !pendingUnset}
      <TableEditor {field} {op} {hint} {badge} {problem} {onOp} />
    {:else if kind === 'table'}
      <Field label={field.name} {hint} {badge} inline error={problem} labelled={false} wide>
        <span class="config-value dim">Removed from config.toml.</span>
      </Field>
    {:else}
      <Field label={field.name} {hint} {badge} inline error={problem} labelled={false}>
        {#if field.secret || field.editor === 'secret'}
          <span class="value-badge">
            <Badge variant={field.set ? 'success' : 'partial'}
              >{field.set ? 'set' : 'not set'}</Badge
            >
          </span>
        {:else}
          <span class="config-value" class:dim={formatValue(field.value) === '—'}
            >{formatValue(field.value)}</span
          >
        {/if}
      </Field>
    {/if}

    {#if showMeta}
      <div class="field-meta control-row">
        {#if pendingSet}
          <Badge size="sm" variant="info">changed</Badge>
        {/if}
        {#if pendingSet || pendingUnset}
          <Button variant="secondary" size="sm" onclick={undo}>Undo</Button>
        {/if}
        {#if field.in_file && !pendingUnset}
          <Button
            variant="secondary"
            size="sm"
            title="Remove from config.toml, so the default applies"
            onclick={reset}>Reset</Button
          >
        {/if}
      </div>
    {/if}
  </div>

  {#if showMeta && pendingUnset}
    <p class="caption field-note">Will reset to the default: {formatValue(field.default)}</p>
  {:else if showMeta && !pendingSet && !field.in_file && 'default' in field}
    <p class="caption field-note">Not in config.toml; the default applies.</p>
  {/if}
  {#if editable && field.read_only_reason}
    <p class="caption field-note">{field.read_only_reason}</p>
  {/if}
</div>

<style>
  .config-field {
    padding: var(--space-2) 0;
    border-bottom: 1px solid var(--border-subtle);
  }

  .config-field:last-child {
    border-bottom: none;
  }

  /* The meta row (default note, Undo, Reset) sits on the control's line and
     wraps under it when the row is too narrow. */
  .field-row.inline-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    /* The gap SecretField leaves before its Clear, so a button beside a field
       sits the same distance from it whichever component draws it. */
    column-gap: var(--space-2);
    row-gap: var(--space-1);
  }

  .inline-meta > :global(.field) {
    flex: 0 1 calc(var(--field-label-width) + var(--space-3) + 24rem);
  }

  .inline-meta > :global(.field-wide) {
    flex-basis: calc(var(--field-label-width) + var(--space-3) + 36rem);
  }

  .field-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .field-meta:empty {
    display: none;
  }

  .field-note {
    margin: var(--space-1) 0 0;
  }

  /* Every control-column item that is not itself a control takes the
     control's height as a floor, centred in it, so it lines up with the
     label and the meta buttons as an input would. */
  .value-badge,
  .choices,
  .config-value {
    display: flex;
    align-items: center;
    min-height: var(--control-height-lg);
  }

  .value-badge {
    justify-self: start;
    align-self: flex-start;
  }

  @media (min-width: 641px) {
    /* The control's line, and top-aligned with it, so the buttons sit level
       with the control however tall the label or an error makes the field. */
    .inline-meta > .field-meta {
      min-height: var(--control-height-lg);
      align-self: flex-start;
    }

    /* Under the control, in its column, not under the label. */
    .field-note {
      margin-left: calc(var(--field-label-width) + var(--space-3));
    }
  }

  .choices {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .config-value {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .config-value.dim {
    color: var(--text-dim);
  }
</style>
