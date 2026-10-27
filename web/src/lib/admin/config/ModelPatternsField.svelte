<script lang="ts">
  import type { Snippet } from 'svelte';
  import { X } from '@lucide/svelte';
  import type { ConfigModelCatalogue } from '$lib/api';
  import { AutocompleteInput, Button, Field, IconButton, Input, Select } from '@minot/ui';
  import { filterOptions, valueOf } from '$lib/components/chat/modelPicker';
  import { modelOptions, patternCount } from './modelField';

  interface Props {
    label: string;
    /** The entries shown, or null when the setting is not written. */
    value: string[] | null;
    catalogue: ConfigModelCatalogue;
    hint?: string;
    badge?: string;
    labelMark?: Snippet;
    problem?: string;
    onValue: (value: string[]) => void;
  }

  let { label, value, catalogue, hint, badge, labelMark, problem, onValue }: Props = $props();

  /** The rows on screen, blank ones included while they are typed; what is
   *  sent leaves the blank ones out, since the loader refuses an empty entry. */
  let rows = $state<string[]>([]);
  let sent: string | null = null;

  $effect(() => {
    const incoming = JSON.stringify(value ?? null);
    if (incoming === sent) return;
    rows = [...(value ?? [])];
    sent = incoming;
  });

  function emit(next: string[]) {
    rows = next;
    const kept = next.map((row) => row.trim()).filter(Boolean);
    sent = JSON.stringify(kept);
    onValue(kept);
  }

  let chosenSource = $state('');
  let addSource = $derived(chosenSource || catalogue.sources[0]?.id || '');
  let addText = $state('');
  let sources = $derived(catalogue.sources.map((s) => ({ value: s.id, label: s.label })));
  let options = $derived(modelOptions(catalogue, addSource).filter((o) => o.value));

  /** The server's count for a stored entry, else this page's by the same rule. */
  function countOf(pattern: string): number {
    return catalogue.pattern_counts[pattern] ?? patternCount(catalogue, pattern);
  }

  function countText(pattern: string): string {
    if (!pattern.trim()) return '';
    const n = countOf(pattern);
    return n === 1 ? '1 model' : `${n} models`;
  }

  function setRow(index: number, next: string) {
    emit(rows.map((row, i) => (i === index ? next : row)));
  }

  function removeRow(index: number) {
    emit(rows.filter((_, i) => i !== index));
  }

  function addRef(chosen: string) {
    const ref = valueOf(options, chosen);
    addText = '';
    if (ref && !rows.includes(ref)) emit([...rows, ref]);
  }
</script>

<Field {label} {hint} {badge} {labelMark} layout="inline" wide error={problem}>
  <div class="patterns vstack" style:--vstack-gap="var(--space-2)">
    {#if value === null}
      <p class="caption">
        Not set, so a non-admin may pick every Anthropic model and native's configured model.
      </p>
    {:else if !rows.length}
      <p class="caption">Empty: a non-admin may pick aliases only.</p>
    {/if}
    {#each rows as pattern, index (index)}
      <div class="pattern-row">
        <span class="pattern-input">
          <Input
            value={pattern}
            monospace
            autocomplete="off"
            spellcheck="false"
            aria-label={`${label} entry ${index + 1}`}
            oninput={(e: Event) => setRow(index, (e.currentTarget as HTMLInputElement).value)}
          />
        </span>
        <span class="caption num pattern-count">{countText(pattern)}</span>
        <IconButton
          icon={X}
          size="sm"
          danger
          label={`Remove ${pattern || 'entry'}`}
          onclick={() => removeRow(index)}
        />
      </div>
    {/each}
    <div class="pattern-add">
      <span class="pattern-source">
        <Select
          value={addSource}
          options={sources}
          ariaLabel={`${label} source to add from`}
          fullWidth
          onValueChange={(next) => {
            chosenSource = next;
            addText = '';
          }}
        />
      </span>
      <span class="pattern-input">
        <AutocompleteInput
          bind:value={addText}
          options={options.map((o) => o.label)}
          filter={filterOptions(options)}
          limit={500}
          monospace
          ariaLabel={`${label} model to add`}
          placeholder="Add a model"
          onSelect={addRef}
        />
      </span>
      <Button variant="secondary" size="sm" onclick={() => (rows = [...rows, ''])}
        >Add pattern</Button
      >
    </div>
  </div>
</Field>

<style>
  .patterns .caption {
    margin: 0;
  }

  .pattern-row,
  .pattern-add {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .pattern-input {
    flex: 1 1 14rem;
    min-width: 0;
  }

  .pattern-source {
    flex: 0 0 9rem;
  }

  .pattern-count {
    min-width: 5rem;
  }
</style>
