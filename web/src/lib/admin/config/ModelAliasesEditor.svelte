<script lang="ts">
  import type { ConfigModelCatalogue } from '$lib/api';
  import { Button, Field, Input } from '@minot/ui';
  import FieldRow from './FieldRow.svelte';
  import ModelField from './ModelField.svelte';
  import {
    ALIAS_NAMESPACES,
    NAMESPACE_LABELS,
    targetOf,
    targetValue,
    withAlias,
    withTarget,
    type AliasesForm,
  } from './modelAliases';

  interface Props {
    form: AliasesForm;
    catalogue: ConfigModelCatalogue;
    /** The providers the native brain can run, from `[brain.native] model`'s field. */
    nativeSources: string[];
    /** False when nothing on the page can be changed, or the field is read-only. */
    editable: boolean;
    readOnlyReason?: string | null;
  }

  let {
    form = $bindable(),
    catalogue,
    nativeSources,
    editable,
    readOnlyReason = null,
  }: Props = $props();

  let newName = $state('');

  let fieldFor = $derived({
    anthropic: { sources: ['anthropic'], effort: true, aliases: true },
    openai_compat: { sources: nativeSources, effort: true, aliases: true },
  });

  function addAlias() {
    const next = withAlias(form, newName);
    if (next !== form) {
      form = next;
      newName = '';
    }
  }
</script>

<!--
  [models.aliases] as one block per alias and, in it, one target per
  namespace. What the form becomes is in modelAliases.ts: the whole table as
  one set_toml op.
-->
<div class="model-aliases">
  {#each form as row (row.name)}
    <div class="alias vstack" style:--vstack-gap="var(--space-2)" data-testid={`alias-${row.name}`}>
      <h3 class="micro-label">{row.name}</h3>
      {#each ALIAS_NAMESPACES as namespace (namespace)}
        {#if editable}
          <FieldRow>
            <ModelField
              label={NAMESPACE_LABELS[namespace]}
              value={targetValue(row.targets[namespace])}
              modelField={fieldFor[namespace]}
              {catalogue}
              effortOnly
              onValue={(value) => (form = withTarget(form, row.name, namespace, targetOf(value)))}
            />
          </FieldRow>
        {:else}
          <Field label={NAMESPACE_LABELS[namespace]} layout="inline">
            <span class="config-value">{targetValue(row.targets[namespace]) || 'default'}</span>
          </Field>
        {/if}
      {/each}
    </div>
  {/each}
  {#if editable}
    <div class="alias-add control-row">
      <Input
        bind:value={newName}
        monospace
        autocomplete="off"
        spellcheck="false"
        placeholder="new alias name"
        aria-label="New alias name"
      />
      <Button variant="secondary" size="sm" onclick={addAlias}>Add alias</Button>
    </div>
  {:else if readOnlyReason}
    <p class="caption">{readOnlyReason}</p>
  {/if}
</div>

<style>
  .model-aliases {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-2) 0;
    border-bottom: 1px solid var(--border-subtle);
  }

  .alias > .micro-label {
    margin: 0;
  }

  .model-aliases .caption {
    margin: 0;
  }

  .alias-add {
    display: flex;
    gap: var(--space-2);
    max-width: 24rem;
  }

  .config-value {
    display: flex;
    align-items: center;
    min-height: var(--control-height-lg);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }
</style>
