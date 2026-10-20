<script lang="ts">
  import Alert from './Alert.svelte';

  let {
    title,
    variant,
    appearance,
    role,
    collapsed = $bindable(),
    bindCollapsed = false,
    withBody = true,
    withActions = false,
  }: {
    title?: string;
    variant?: 'info' | 'warn' | 'danger' | 'success';
    appearance?: 'bordered' | 'inline';
    role?: 'note' | 'alert' | 'status';
    collapsed?: boolean;
    bindCollapsed?: boolean;
    withBody?: boolean;
    withActions?: boolean;
  } = $props();
</script>

{#snippet body()}<p>Alert body</p>{/snippet}
{#snippet actions()}<button type="button">Fix it</button>{/snippet}

{#if bindCollapsed}
  <Alert
    {title}
    {variant}
    {appearance}
    {role}
    bind:collapsed
    actions={withActions ? actions : undefined}
    children={withBody ? body : undefined}
  />
  <output data-testid="collapsed">{collapsed ? 'collapsed' : 'open'}</output>
{:else}
  <Alert
    {title}
    {variant}
    {appearance}
    {role}
    {collapsed}
    actions={withActions ? actions : undefined}
    children={withBody ? body : undefined}
  />
{/if}
