<script lang="ts">
  import { Button, CategoryGroup } from '$lib/components/ui';
  import type { WorkspaceConfigFile } from '$lib/api';
  import { formatBytes } from '$lib/format';
  let {
    files,
    error,
    onEdit,
    onRetry,
  }: {
    files: WorkspaceConfigFile[];
    error: string;
    onEdit: (file: WorkspaceConfigFile) => void;
    onRetry: () => void;
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
    <Button
      size="sm"
      variant="ghost"
      ariaLabel={`Edit ${file.name}`}
      disabled={!file.editable}
      onclick={() => onEdit(file)}>Edit</Button
    >
  </div>
{/snippet}

<section aria-label="Configuration">
  <CategoryGroup label="Configuration">
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
    <CategoryGroup label="Skill instructions" collapsible defaultOpen={false}>
      <p class="caption skills-note">Added to a skill's instructions whenever it loads.</p>
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
    padding: var(--space-1) var(--space-2);
  }
  .config-name {
    display: flex;
    flex-direction: column;
    min-width: 0;
    font-size: var(--text-sm);
  }
  .config-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .config-error,
  .skills-note {
    padding: var(--space-1) var(--space-2);
  }
  .skills-note {
    margin: 0;
  }
  .config-error {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
</style>
