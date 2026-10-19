<script lang="ts">
  import { CircleDashed, Lock } from '@lucide/svelte';
  import type { AdminConfigField, ConfigOp, ConfigSecretSource } from '$lib/api';
  import {
    Badge,
    Button,
    ToggleGroup,
    Field,
    Input,
    Select,
    TextArea,
    Checkbox,
  } from '$lib/components/ui';
  import ConfigSecretField from './SecretField.svelte';
  import FieldRow from './FieldRow.svelte';
  import TableEditor from './TableEditor.svelte';
  import { displayValue, formatValue, opFor } from './changeSet';
  import { parseList, parseNumber } from '$lib/ui/parse';
  import { credentialStatus } from './credentialStatus';

  interface Props {
    field: AdminConfigField;
    /** This field's pending op, if any. */
    op: ConfigOp | undefined;
    /** A credential field's pending value: `""` clears, undefined is unchanged. */
    secretPending?: string;
    /** Where a credential's value is held, from the view's `secrets` entry. */
    secretSource?: ConfigSecretSource;
    /** False when nothing on the page can be changed (no applier); the banner
     *  says why, so the field shows its value and no reason of its own. */
    editable: boolean;
    /** A problem the last check reported against this key. */
    problem?: string;
    onOp: (op: ConfigOp | null, path: string[]) => void;
    onSecret: (name: string, value: string | null) => void;
  }

  let { field, op, secretPending, secretSource, editable, problem, onOp, onSecret }: Props =
    $props();

  // What the admin typed into a number or list input, held while it does not
  // parse (a number) or while it is being typed (a list, whose blank lines
  // parsing drops). Cleared by Undo and reset, which change the value under it.
  let draftText = $state<string | null>(null);
  let inputError = $state('');

  let kind = $derived(editable ? field.editor : 'readonly');
  let hint = $derived(field.doc ?? undefined);
  let showMeta = $derived(editable && field.editor !== 'readonly' && field.editor !== 'secret');
  let shown = $derived(displayValue(field, op));
  let status = $derived(credentialStatus(field.set, secretSource));
  /** A credential shown but not editable here: no file to write, or read-only. */
  let shownSecret = $derived(
    !(kind === 'secret' && field.credential) && (field.secret || field.editor === 'secret'),
  );
  let pendingSet = $derived(op?.op === 'set' || op?.op === 'set_toml');
  let pendingUnset = $derived(op?.op === 'unset');
  let numericList = $derived(
    Array.isArray(field.value)
      ? field.value.length > 0 && field.value.every((v) => typeof v === 'number')
      : false,
  );
  let badge = $derived(kind === 'readonly' && field.read_only_reason ? 'read-only' : undefined);
  let labelMark = $derived(field.risky && !badge ? lockMark : undefined);
  let selectOptions = $derived(
    (field.choices ?? []).map((choice) => ({
      value: choice,
      label: choice === '' ? '(none)' : choice,
    })),
  );

  const TEXT_KINDS = new Set(['str', 'path', 'int', 'float', 'list']);
  let hasDefault = $derived('default' in field);
  let textKind = $derived(TEXT_KINDS.has(kind));
  /** The key is absent from config.toml, or will be once a pending reset lands. */
  let notInFile = $derived(showMeta && hasDefault && (!field.in_file || pendingUnset));
  /** A text control left empty so its placeholder shows the default. */
  let blank = $derived(textKind && notInFile && !pendingSet);
  let defaultText = $derived(
    Array.isArray(field.default)
      ? field.default.map(String).join(kind === 'list' ? '\n' : ', ')
      : field.default === null || field.default === undefined
        ? ''
        : String(field.default),
  );
  let placeholder = $derived(textKind && hasDefault && showMeta ? defaultText : undefined);
  let markLabel = $derived(
    pendingSet
      ? 'Not in config.toml yet; this change adds it'
      : pendingUnset
        ? 'Will be removed from config.toml; the default applies'
        : 'Not in config.toml; the default applies',
  );

  /** An emptied text control returns the key to its default. */
  function clearToDefault() {
    draftText = null;
    inputError = '';
    onOp(field.in_file ? { op: 'unset', path: field.path } : null, field.path);
  }

  function emit(value: unknown) {
    onOp(opFor(field, value), field.path);
  }

  function onNumber(text: string) {
    if (!text.trim() && hasDefault) {
      clearToDefault();
      return;
    }
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

  function onText(text: string) {
    if (!text && hasDefault) clearToDefault();
    else emit(text);
  }

  function onList(text: string) {
    if (!text.trim() && hasDefault) {
      clearToDefault();
      return;
    }
    draftText = text;
    const parsed = parseList(text, { separator: '\n', numeric: numericList });
    if (parsed === null) {
      inputError = 'Every line has to be a number.';
      onOp(null, field.path);
      return;
    }
    inputError = '';
    emit(parsed);
  }

  // In vocabulary order, whatever order the chips were pressed in.
  function emitChoices(picked: string[]) {
    emit((field.multi_choices ?? []).filter((c) => picked.includes(c)));
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

{#snippet statusMark()}
  <Badge size="sm" variant={status.variant}>{status.label}</Badge>
{/snippet}

{#snippet lockMark()}
  <span class="lock-mark" role="img" aria-label="Needs confirmation" title="Needs confirmation"
    ><Lock size={13} aria-hidden="true" /></span
  >
{/snippet}

{#snippet defaultMark()}
  {#if notInFile}
    <span class="default-mark" role="img" aria-label={markLabel} title={markLabel}
      ><CircleDashed size={14} /></span
    >
  {/if}
{/snippet}

<div class="config-field" class:pending={!!op || secretPending !== undefined} title={field.key}>
  <FieldRow>
    {#if kind === 'secret' && field.credential}
      <ConfigSecretField
        label={field.name}
        configured={field.set}
        {hint}
        {status}
        inline
        pending={secretPending}
        onChange={(value) => onSecret(field.credential!.name, value)}
      />
    {:else if kind === 'bool'}
      <Field label={field.name} {hint} {badge} {labelMark} layout="inline" checkbox error={problem}>
        <span class="adorned beside">
          <Checkbox checked={shown === true} onchange={(on) => emit(on)} />
          {@render defaultMark()}
        </span>
      </Field>
    {:else if kind === 'int' || kind === 'float'}
      <Field
        label={field.name}
        {hint}
        {badge}
        {labelMark}
        layout="inline"
        error={inputError || problem}
      >
        <Input
          type="number"
          step={kind === 'float' ? 'any' : '1'}
          value={draftText ?? (blank || shown === null || shown === undefined ? '' : String(shown))}
          {placeholder}
          invalid={!!inputError}
          suffix={notInFile ? defaultMark : undefined}
          oninput={(e: Event) => onNumber((e.currentTarget as HTMLInputElement).value)}
        />
      </Field>
    {:else if kind === 'str' || kind === 'path'}
      <Field label={field.name} {hint} {badge} {labelMark} layout="inline" error={problem}>
        <Input
          value={blank || shown === null || shown === undefined ? '' : String(shown)}
          {placeholder}
          monospace={kind === 'path'}
          suffix={notInFile ? defaultMark : undefined}
          oninput={(e: Event) => onText((e.currentTarget as HTMLInputElement).value)}
        />
      </Field>
    {:else if kind === 'list'}
      <Field
        label={field.name}
        {hint}
        {badge}
        {labelMark}
        layout="inline"
        error={inputError || problem}
      >
        <span class="adorned area" class:marked={notInFile}>
          <TextArea
            monospace
            rows={3}
            value={draftText ??
              (blank ? '' : Array.isArray(shown) ? shown.map(String).join('\n') : '')}
            invalid={!!inputError}
            placeholder={placeholder || 'One per line'}
            oninput={(e: Event) => onList((e.currentTarget as HTMLTextAreaElement).value)}
          />
          {@render defaultMark()}
        </span>
      </Field>
    {:else if kind === 'choice'}
      <Field label={field.name} {hint} {badge} {labelMark} layout="inline" error={problem}>
        <span class="adorned select">
          <Select
            value={String(shown ?? '')}
            options={selectOptions}
            ariaLabel={field.name}
            fullWidth
            onValueChange={(value) => emit(value)}
          />
          {@render defaultMark()}
        </span>
      </Field>
    {:else if kind === 'multi_choice'}
      <Field label={field.name} {hint} {badge} {labelMark} layout="inline" error={problem}>
        <span class="adorned beside">
          <span class="choices">
            <ToggleGroup
              type="multiple"
              label={field.name}
              value={Array.isArray(shown) ? (shown as string[]) : []}
              options={(field.multi_choices ?? []).map((choice) => ({
                value: choice,
                label: choice,
              }))}
              onValueChange={(next) => emitChoices(next as string[])}
            />
          </span>
          {@render defaultMark()}
        </span>
      </Field>
    {:else if kind === 'table' && !pendingUnset}
      <TableEditor {field} {op} {hint} {badge} {labelMark} {problem} {onOp} />
    {:else if kind === 'table'}
      <Field label={field.name} {hint} {badge} {labelMark} layout="inline" error={problem}>
        <span class="config-value dim">Removed from config.toml.</span>
      </Field>
    {:else if shownSecret}
      <!-- Its status beside the label, as an editable credential's is; the
           control column says why it cannot be edited here. -->
      <Field
        label={field.name}
        {hint}
        {badge}
        labelMark={statusMark}
        layout="inline"
        error={problem}
      >
        <span class="config-value dim">{field.read_only_reason || '—'}</span>
      </Field>
    {:else}
      <Field label={field.name} {hint} {badge} {labelMark} layout="inline" error={problem}>
        <span class="config-value" class:dim={formatValue(field.value) === '—'}
          >{formatValue(field.value)}</span
        >
      </Field>
    {/if}

    {#if showMeta}
      <!-- Reset, then Undo, then the badge: each appears after the ones that
           are already there, so nothing shifts under the pointer. -->
      <div class="field-meta control-row">
        {#if field.in_file && !pendingUnset}
          <Button
            variant="secondary"
            size="sm"
            title="Remove from config.toml, so the default applies"
            onclick={reset}>Reset</Button
          >
        {/if}
        {#if pendingSet || pendingUnset}
          <Button variant="secondary" size="sm" onclick={undo}>Undo</Button>
        {/if}
        {#if pendingSet}
          <span class="changed-badge"><Badge size="sm" variant="info">changed</Badge></span>
        {/if}
      </div>
    {/if}
  </FieldRow>

  <!-- A text control shows the default as its placeholder; the others need
       it said. -->
  {#if showMeta && pendingUnset && !textKind}
    <p class="caption field-note">Will reset to the default: {formatValue(field.default)}</p>
  {/if}
  {#if editable && field.read_only_reason && !shownSecret}
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

  /* Fills the rest of the row so the badge can sit at its far end. */
  .field-meta {
    flex: 1 1 auto;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .changed-badge {
    margin-inline-start: auto;
  }

  .field-meta:empty {
    display: none;
  }

  .field-note {
    margin: var(--space-1) 0 0;
    max-width: 60ch;
  }

  /* Every control-column item that is not itself a control takes the
     control's height as a floor, centred in it, so it lines up with the
     label and the meta buttons as an input would. */
  .choices,
  .config-value {
    display: flex;
    align-items: center;
    min-height: var(--control-height-lg);
  }

  @media (min-width: 641px) {
    /* The control's line, and top-aligned with it, so the buttons sit level
       with the control however tall the label or an error makes the field. */
    .field-meta {
      min-height: var(--control-height-lg);
      align-self: flex-start;
    }

    /* Under the control, in its column, not under the label. */
    .field-note {
      margin-left: calc(var(--field-label-width) + var(--space-3));
    }
  }

  /* The control and its "not in config.toml" mark, for the controls that are
     not an Input (a text or number Input takes the mark as its suffix). Inside
     the box for a textarea or a select, after it for a checkbox or chips.
     Carries the control's own cap so the mark sits at the box's right edge. */
  .adorned {
    position: relative;
    display: block;
    width: 100%;
    max-width: 24rem;
  }

  .lock-mark {
    display: inline-flex;
    color: var(--text-dim);
  }

  .default-mark {
    display: flex;
    color: var(--text-dim);
  }

  .adorned .default-mark {
    position: absolute;
    top: 50%;
    right: var(--space-2);
    transform: translateY(-50%);
  }

  .adorned.area .default-mark {
    top: var(--space-2);
    transform: none;
  }

  /* A select keeps its chevron at the edge; the mark sits left of it. */
  .adorned.select .default-mark {
    right: calc(var(--space-2) + var(--space-4) + var(--space-1));
  }

  /* Text stops short of the mark. */
  .config-field .adorned.marked :global(textarea) {
    padding-right: calc(var(--space-2) + var(--space-4) + var(--space-1));
  }

  .config-field .adorned.beside {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--control-height-lg);
    /* Below 641px the checkbox field is a flex row; full width here pushed
       the label to the far end. In the inline grid it still stretches. */
    width: auto;
    max-width: none;
  }

  /* Centred by the row above rather than by Field's own offset. */
  .config-field .adorned.beside :global(input[type='checkbox']) {
    margin: 0;
  }

  .adorned.beside .default-mark {
    position: static;
    transform: none;
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
