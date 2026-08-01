<script lang="ts">
  import { SettingsCard, SettingsField } from '$lib/components/settings';
  import { Button, Input } from '$lib/components/ui';
  import BracketEditor from './BracketEditor.svelte';
  import RateProvenanceLine from './RateProvenanceLine.svelte';
  import type { RateProvenance, ResolvedField } from '$lib/money/api';

  // One rate card, used for federal and for the state — they differ in title
  // and in nothing else, which is the whole point of the jurisdiction work.

  interface Props {
    title: string;
    description?: string;
    taxYear: number;
    standardDeduction: ResolvedField<number | null>;
    brackets: ResolvedField<number[][]>;
    provenance: RateProvenance;
    /** Shown above the fields when the jurisdiction has nothing to compute. */
    unavailableNotice?: string;
    onEdit: (patch: { standard_deduction?: number | null; brackets?: number[][] | null }) => void;
  }

  let {
    title,
    description,
    taxYear,
    standardDeduction,
    brackets,
    provenance,
    unavailableNotice = '',
    onEdit,
  }: Props = $props();

  // A revert is a separate action from a save: it deletes the override rather
  // than editing it, so it takes its own button per field rather than riding
  // the app-bar Save. Without it the editor could only ever replace one number
  // with another, never get back to the shipped one.
  function revert(field: 'standard_deduction' | 'brackets') {
    onEdit({ [field]: null });
  }

  let deductionValue = $derived(standardDeduction.value ?? '');
</script>

<SettingsCard {title} {description}>
  {#if unavailableNotice}
    <p class="unavailable">{unavailableNotice}</p>
  {/if}

  <RateProvenanceLine {provenance} {taxYear} />

  <SettingsField
    label="Standard deduction"
    hint="Leave as shipped unless you have a figure from the authority that differs."
  >
    <div class="field-row control-row">
      <Input
        type="number"
        value={deductionValue}
        oninput={(e) => {
          const raw = (e.currentTarget as HTMLInputElement).value;
          onEdit({ standard_deduction: raw.trim() === '' ? null : Number(raw) });
        }}
      />
      {#if standardDeduction.overridden}
        <Button variant="ghost" size="sm" onclick={() => revert('standard_deduction')}>
          Revert
        </Button>
      {/if}
    </div>
    {#if standardDeduction.overridden}
      <p class="overridden">Your value, not the shipped one.</p>
    {/if}
  </SettingsField>

  <SettingsField label="Brackets" labelled={false} wide>
    <BracketEditor value={brackets.value ?? []} onchange={(next) => onEdit({ brackets: next })} />
    {#if brackets.overridden}
      <div class="revert-row">
        <p class="overridden">Your brackets, not the shipped ones.</p>
        <Button variant="ghost" size="sm" onclick={() => revert('brackets')}>Revert</Button>
      </div>
    {/if}
  </SettingsField>
</SettingsCard>

<style>
  .field-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .overridden {
    margin: var(--space-1) 0 0;
    font-size: var(--text-xs);
    color: var(--status-info-fg);
  }

  .revert-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    margin-top: var(--space-1);
  }

  .unavailable {
    margin: 0 0 var(--space-2);
    font-size: var(--text-sm);
    color: var(--status-warn-fg);
  }
</style>
