<script lang="ts">
  import { untrack } from 'svelte';
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
    /** `null` means "revert to the shipped rates" — see `emit`. */
    onchange: (next: number[][] | null) => void;
    /** Reports whether the table is currently unsaveable, so the page can gate Save. */
    onproblem?: (problem: string) => void;
    disabled?: boolean;
  }

  let { value, onchange, onproblem, disabled = false }: Props = $props();

  // Rows are the edit buffer, not a projection of `value`: mid-edit a field can
  // legitimately be empty or a lone "-", which has no number to project from
  // and would be destroyed on every keystroke by a `$derived`.
  interface Row {
    threshold: string;
    ratePercent: string;
  }

  let rows: Row[] = $state([]);
  let lastSerialized = $state('');

  // Resync when the *incoming* value changes, and at no other time. The guard
  // has to be read untracked or the effect depends on state `emit` writes: an
  // edit would re-run this, find the prop still holding the pre-edit table
  // (the consumer stashes the patch rather than echoing it back), and rebuild
  // the rows from it — reverting the keystroke that had just been typed.
  $effect(() => {
    const serialized = JSON.stringify(value);
    untrack(() => {
      if (serialized === lastSerialized) return;
      lastSerialized = serialized;
      rows = (value ?? []).map(([threshold, rate]) => ({
        threshold: String(threshold),
        // toFixed would render 9.3 as "9.300"; a round-trip through Number
        // drops the float noise that 0.093 * 100 otherwise leaves behind.
        ratePercent: String(Number((rate * 100).toFixed(6))),
      }));
    });
  });

  // Read out of the DOM rather than `bind:value`, which is what keeps the rows
  // a *string* buffer. Svelte's binding coerces a number input to a number and
  // writes `null` when it is emptied, so clearing a field put a null where the
  // buffer promises a string and `emit` threw on the next keystroke — in its
  // own oninput handler, so the field could not be cleared at all. Matches how
  // the sibling number fields on the taxes page already read their input.
  function edit(row: Row, field: keyof Row, e: Event) {
    row[field] = (e.currentTarget as HTMLInputElement).value;
    emit();
  }

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
    // An emptied editor is the natural gesture for "go back to the shipped
    // rates", and the server refuses an empty array — so it is reported as a
    // revert (null) rather than saved as a validation error.
    onchange(next.length ? next : null);
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

  // Reported up so the page can refuse the Save. Not merely advisory: `emit`
  // drops a row it cannot parse, so a half-typed bracket would otherwise be
  // saved as a table that is silently missing that band — income in it falls to
  // the rate below, understating the tax with nothing to show for it.
  let problem = $derived.by(() => {
    const halfTyped = rows.some(
      (r) => (r.threshold.trim() === '') !== (r.ratePercent.trim() === ''),
    );
    if (halfTyped) return 'Every bracket needs both a threshold and a rate.';
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

  // Untracked, and not a nicety: the consumer holds these reports in its own
  // state and writes a fresh record per report, so reading that record inside
  // the callback would make this effect a dependent of what the callback
  // writes — the first report re-invalidates the effect that produced it, and
  // the page dies on mount with effect_update_depth_exceeded. The dependency is
  // `problem` and nothing the parent happens to touch.
  $effect(() => {
    const current = problem;
    untrack(() => onproblem?.(current));
  });
</script>

<div class="bracket-editor">
  <div class="head">
    <span class="col-threshold">Income over</span>
    <span class="col-rate">Rate</span>
    <span class="col-remove"></span>
  </div>

  {#each rows as row, i (i)}
    <div class="bracket-row control-row">
      <div class="col-threshold">
        <Input
          type="number"
          value={row.threshold}
          oninput={(e) => edit(row, 'threshold', e)}
          {disabled}
          aria-label="Bracket {i + 1} income threshold"
        />
      </div>
      <div class="col-rate">
        <Input
          type="number"
          step="0.001"
          value={row.ratePercent}
          oninput={(e) => edit(row, 'ratePercent', e)}
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
      No brackets. Add one to start, or save with this empty to go back to the shipped rates.
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

  /* Not `.row`: settings.css carries a global `.settings .row { display: flex }`
     for the settings pages this editor renders inside, and a Svelte-scoped
     `.row.s-xxxx` has exactly the same specificity — so which one applied came
     down to stylesheet order, and the grid collapsed to a flex row (columns no
     longer under their headers, inputs at a third of their width) on whichever
     build put settings.css last. */
  .head,
  .bracket-row {
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
