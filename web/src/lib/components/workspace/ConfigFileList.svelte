<script lang="ts">
  import { Button, CategoryGroup, KebabMenu, type KebabItem } from '$lib/components/ui';
  import type { WorkspaceConfigFile } from '$lib/api';
  import { formatBytes } from '$lib/format';
  let {
    files,
    error,
    onEdit,
    onRetry,
    revealAction,
  }: {
    files: WorkspaceConfigFile[];
    error: string;
    onEdit: (file: WorkspaceConfigFile) => void;
    onRetry: () => void;
    revealAction: (
      ref: string | null | undefined,
      provider?: 'workspace' | 'drive',
      available?: boolean,
    ) => KebabItem;
  } = $props();
  const core = $derived(files.filter((file) => file.kind === 'config'));
  const skills = $derived(files.filter((file) => file.kind === 'skill'));
  function label(file: WorkspaceConfigFile) {
    return file.kind === 'skill' ? file.name.slice('skills/'.length) : file.name;
  }
  function detail(file: WorkspaceConfigFile) {
    if (!file.editable) return 'Not a plain file';
    if (!file.exists) return 'Not created yet';
    return file.size === null ? '' : formatBytes(file.size);
  }
</script>

{#snippet row(file: WorkspaceConfigFile)}
  <div class="list-row config-row">
    <span class="config-name" title={file.name}
      ><span class="config-label">{label(file)}</span><span class="caption">{detail(file)}</span
      ></span
    >
    <KebabMenu
      ariaLabel={`Actions for ${file.name}`}
      items={[
        { label: 'Edit', disabled: !file.editable, onSelect: () => onEdit(file) },
        revealAction(file.view_path, 'workspace', file.exists),
      ]}
    />
  </div>
{/snippet}

<section aria-label="Configuration">
  <CategoryGroup
    label="Configuration"
    collapsible
    defaultOpen={false}
    storageKey="workspace:section:configuration"
  >
    {#if error}
      <div class="config-error">
        <span class="form-error">{error}</span>
        <Button size="sm" variant="ghost" onclick={onRetry}>Retry</Button>
      </div>
    {:else}
      {#each core as file (file.name)}{@render row(file)}{/each}
    {/if}
  </CategoryGroup>
  {#if skills.length}
    <CategoryGroup
      label="Skill instructions"
      collapsible
      defaultOpen={false}
      storageKey="workspace:section:skills"
    >
      <p class="caption">Added to a skill's instructions whenever it loads.</p>
      {#each skills as file (file.name)}{@render row(file)}{/each}
    </CategoryGroup>
  {/if}
</section>

<style>
  .config-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding-block: var(--space-1);
    padding-left: var(--space-3);
  }
  .config-name {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    font-size: var(--text-sm);
  }
  .config-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .config-error {
    padding: var(--space-1) var(--space-2);
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
</style>
