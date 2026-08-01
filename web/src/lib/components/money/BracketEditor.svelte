<script lang="ts">
  import { Button, IconButton, Input } from '$lib/components/ui';
  import { Plus, X } from 'lucide-svelte';

  // A repeating threshold/rate row list. A flat state is one row, so it needs
  // no separate control — which is also why the data models a flat rate as a
  // one-bracket table: states move between flat and graduated (Ohio did in
  // 2026), and a separate shape would need a migration each time one converts.
  //
  // Rates are edited as PERCENTAGES and stored as fractions. Nobody types
  // 0.0495 for Illinois, and a field that silently takes 4.95 as a 495% rate is
  // a worse failure than the conversion is a complication.

  interface Props {
    /** Stored form: [threshold, rate-as-fraction] pairs. */
    value: number[][];
    onchange: (next: number[][]) => void;
    disabled?: boolean;
  }

  let { value, onchange, disabled = false }: Props = $props();

  // Rows are the edit buffer, not a projection of `value`: mid-edit a field can
  // legitimately be empty or a lone "-", which has no number to project from
  // and would be destroyed on every keystroke by a `$derived`.
  interface Row {
    threshold: string;
    ratePercent: string;
  }

  let rows: Row[] = $state([]);
  let lastSerialized = $state('');

  $effect(() => {
    const serialized = JSON.stringify(value);
    if (serialized === lastSerialized) return;
    lastSerialized = serialized;
    rows = (value ?? []).map(([threshold, rate]) => ({
      threshold: String(threshold),
      // toFixed would render 9.3 as "9.300"; a round-trip through Number
      // drops the float noise that 0.093 * 100 otherwise leaves behind.
      ratePercent: String(Number((rate * 100).toFixed(6))),
    }));
  });

  function emit() {
    const next: number[][] = [];
    for (const row of rows) {
      const threshold = Number(row.threshold);
      const percent = Number(row.ratePercent);
      if (row.threshold.trim() === '' || row.ratePercent.trim() === '') continue;
      if (!Number.isFinite(threshold) || !Number.isFinite(percent)) continue;
      next.push([threshold, Number((percent / 100).toFixed(8))]);
    }
    lastSerialized = JSON.stringify(next);
    onchange(next);
  }

  function addRow() {
    // Seed the threshold above the last one so an added row is already in
    // ascending order — the server refuses an unsorted table, and the common
    // case should not need the user to know that.
    const last = rows[rows.length - 1];
    const seed = last && Number.isFinite(Number(last.threshold)) ? Number(last.threshold) : 0;
    rows = [...rows, { threshold: rows.length ? String(seed + 1) : '0', ratePercent: '' }];
  }

  function removeRow(index: number) {
    rows = rows.filter((_, i) => i !== index);
    emit();
  }

  // Advisory only — the server is the authority and refuses the save. Showing
  // it here means the user finds out at the edit rather than at the Save.
  let problem = $derived.by(() => {
    const parsed = rows.filter((r) => r.threshold.trim() !== '').map((r) => Number(r.threshold));
    if (parsed.some((n) => !Number.isFinite(n))) return 'Thresholds must be numbers.';
    if (parsed.length && parsed[0] !== 0) return 'The first threshold must be 0.';
    for (let i = 1; i < parsed.length; i++) {
      if (parsed[i] <= parsed[i - 1]) return 'Thresholds must ascend and not repeat.';
    }
    const rates = rows.filter((r) => r.ratePercent.trim() !== '').map((r) => Number(r.ratePercent));
    if (rates.some((n) => !Number.isFinite(n) || n < 0 || n > 100)) {
      return 'Rates must be between 0 and 100 percent.';
    }
    return '';
  });
</script>

<div class="bracket-editor">
  <div class="head">
    <span class="col-threshold">Income over</span>
    <span class="col-rate">Rate</span>
    <span class="col-remove"></span>
  </div>

  {#each rows as row, i (i)}
    <div class="row control-row">
      <div class="col-threshold">
        <Input
          type="number"
          bind:value={row.threshold}
          oninput={emit}
          {disabled}
          aria-label="Bracket {i + 1} income threshold"
        />
      </div>
      <div class="col-rate">
        <Input
          type="number"
          step="0.001"
          bind:value={row.ratePercent}
          oninput={emit}
          {disabled}
          aria-label="Bracket {i + 1} rate, percent"
        />
        <span class="suffix">%</span>
      </div>
      <div class="col-remove">
        <IconButton
          label="Remove bracket {i + 1}"
          size="sm"
          danger
          {disabled}
          onclick={() => removeRow(i)}
        >
          <X size={14} />
        </IconButton>
      </div>
    </div>
  {/each}

  {#if !rows.length}
    <p class="empty small">
      No brackets. Add one to start, or leave empty to use the shipped rates.
    </p>
  {/if}

  {#if problem}
    <p class="problem">{problem}</p>
  {/if}

  <div class="actions">
    <Button variant="ghost" size="sm" {disabled} onclick={addRow}>
      <Plus size={14} /> Add bracket
    </Button>
  </div>
</div>

<style>
  .bracket-editor {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .head,
  .row {
    display: grid;
    grid-template-columns: 1fr 1fr auto;
    gap: var(--space-2);
    align-items: center;
  }

  .head {
    font-size: var(--text-2xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .col-rate {
    display: flex;
    align-items: center;
    gap: var(--space-1);
  }

  .suffix {
    font-size: var(--text-sm);
    color: var(--text-dim);
  }

  .problem {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
  }

  .actions {
    display: flex;
  }
</style>
