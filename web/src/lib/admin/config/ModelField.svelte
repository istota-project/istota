<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { ConfigModelCatalogue, ConfigModelField } from '$lib/api';
  import { AutocompleteInput, Field, Input, Select } from '@minot/ui';
  import {
    effortOptions,
    filterOptions,
    valueOf,
    type PickerOption,
  } from '$lib/components/chat/modelPicker';
  import {
    ALIASES,
    OTHER_LABEL,
    joinEffort,
    modelOptions,
    otherValue,
    parseStored,
    sourceChoices,
    sourceNote,
  } from './modelField';

  interface Props {
    /** The field's label, also naming each control. */
    label: string;
    /** The value shown: the pending one, the default after a reset, or the stored one. */
    value: string;
    modelField: ConfigModelField;
    catalogue: ConfigModelCatalogue;
    hint?: string;
    badge?: string;
    labelMark?: Snippet;
    problem?: string;
    /** Send `:effort` for an effort with no model (an alias's effort-only
     *  override) rather than clearing the field. */
    effortOnly?: boolean;
    /** A new value; '' clears the field to its default. */
    onValue: (value: string) => void;
  }

  let {
    label,
    value,
    modelField,
    catalogue,
    hint,
    badge,
    labelMark,
    problem,
    effortOnly = false,
    onValue,
  }: Props = $props();

  let source = $state('');
  let text = $state('');
  let effort = $state('');
  let other = $state(false);
  let otherText = $state('');
  let unknown = $state('');
  /** The last value this control sent, so a value arriving from outside
   *  (Undo, Reset) re-seeds the controls and its own echo does not. */
  let sent: string | null = null;
  /** Set when the admin emptied the field: the default arrives as the value,
   *  and the controls stay empty rather than filling with it mid-edit. */
  let cleared = false;

  $effect(() => {
    if (value === sent) return;
    if (cleared) {
      cleared = false;
      sent = value;
      return;
    }
    const parsed = parseStored(value, catalogue, modelField);
    source = parsed.source;
    text = parsed.model;
    effort = parsed.effort;
    other = false;
    otherText = '';
    unknown = parsed.known ? '' : parsed.model;
    sent = value;
  });

  let sources = $derived(sourceChoices(catalogue, modelField));
  let options = $derived<PickerOption[]>(modelOptions(catalogue, source));
  let labels = $derived(options.map((o) => o.label));
  let picked = $derived(other ? otherValue(source, otherText) : valueOf(options, text));
  let efforts = $derived(options.find((o) => o.value && o.value === picked)?.efforts ?? null);
  let effortChoices = $derived(effortOptions(efforts, effort));
  let note = $derived(sourceNote(catalogue, source));
  let warning = $derived(
    other && picked
      ? `The catalogue does not list ${picked}; check the id before saving.`
      : unknown && text === unknown
        ? `Not in the catalogue: ${unknown}`
        : undefined,
  );

  function send() {
    const level = modelField.effort ? effort : '';
    const next = picked || !effortOnly || !level ? joinEffort(picked, level) : `:${level}`;
    cleared = next === '' && value !== '';
    sent = next;
    onValue(next);
  }

  function onSource(next: string) {
    source = next;
    text = '';
    other = false;
    otherText = '';
  }

  function onText(next: string) {
    if (next === OTHER_LABEL) return;
    text = next;
    send();
  }

  function onSelect(chosen: string) {
    if (chosen === OTHER_LABEL) {
      other = true;
      text = '';
      return;
    }
    text = valueOf(options, chosen);
    send();
  }
</script>

<Field {label} {hint} {badge} {labelMark} layout="inline" wide {warning} error={problem}>
  <div class="model-picker vstack" style:--vstack-gap="var(--space-1)">
    <div class="model-controls">
      <span class="model-source">
        <Select
          value={source}
          options={sources}
          ariaLabel={`${label} source`}
          fullWidth
          onValueChange={onSource}
        />
      </span>
      <span class="model-name">
        {#if other}
          <Input
            value={otherText}
            monospace
            autocomplete="off"
            spellcheck="false"
            placeholder={source === ALIASES ? 'alias' : 'model id'}
            aria-label={`${label} model id`}
            oninput={(e: Event) => {
              otherText = (e.currentTarget as HTMLInputElement).value;
              send();
            }}
          />
        {:else}
          <AutocompleteInput
            value={text}
            options={labels}
            filter={filterOptions(options, text)}
            limit={500}
            monospace
            ariaLabel={`${label} model`}
            placeholder={source === ALIASES ? 'An alias' : 'A model'}
            onValueChange={onText}
            {onSelect}
          />
        {/if}
      </span>
      {#if modelField.effort}
        <span class="model-effort">
          <Select
            value={effort}
            options={effortChoices}
            ariaLabel={`${label} effort`}
            fullWidth
            onValueChange={(next) => {
              effort = next;
              send();
            }}
          />
        </span>
      {/if}
    </div>
    {#if note}<p class="caption">{note}</p>{/if}
  </div>
</Field>

<style>
  .model-controls {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .model-source {
    flex: 0 0 9rem;
  }

  .model-name {
    flex: 1 1 14rem;
    min-width: 0;
  }

  .model-effort {
    flex: 0 0 8rem;
  }

  .model-picker .caption {
    margin: 0;
  }
</style>
