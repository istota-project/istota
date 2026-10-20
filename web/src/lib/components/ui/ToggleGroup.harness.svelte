<script lang="ts">
  import { LayoutGrid, List } from '@lucide/svelte';
  import ToggleGroup from './ToggleGroup.svelte';
  import Field from './Field.svelte';

  let {
    mode = 'single',
    onValueChange,
  }: {
    mode?: 'single' | 'multiple' | 'icons' | 'field';
    onValueChange?: (v: string | string[]) => void;
  } = $props();

  let range = $state('90d');
  let picks = $state<string[]>(['feeds']);
  let view = $state('grid');
</script>

<!-- A bound parent per mode, so a test can watch the value change. -->
{#if mode === 'single'}
  <ToggleGroup
    label="Range"
    bind:value={range}
    {onValueChange}
    options={[
      { value: '30d', label: '30d' },
      { value: '90d', label: '90d' },
      { value: '1y', label: '1y', disabled: true },
      { value: 'all', label: 'All' },
    ]}
  />
  <output data-testid="value">{range}</output>
{:else if mode === 'multiple'}
  <ToggleGroup
    type="multiple"
    label="Disabled modules"
    bind:value={picks}
    {onValueChange}
    options={[
      { value: 'feeds', label: 'feeds' },
      { value: 'money', label: 'money' },
    ]}
  />
  <output data-testid="value">{picks.join(',')}</output>
{:else if mode === 'icons'}
  <ToggleGroup
    label="View"
    iconOnly
    bind:value={view}
    options={[
      { value: 'grid', label: 'Grid view', icon: LayoutGrid },
      { value: 'list', label: 'List view', icon: List },
    ]}
  />
{:else}
  <Field label="Disabled modules">
    <ToggleGroup
      type="multiple"
      label="Disabled modules"
      bind:value={picks}
      options={[{ value: 'feeds', label: 'feeds' }]}
    />
  </Field>
{/if}
