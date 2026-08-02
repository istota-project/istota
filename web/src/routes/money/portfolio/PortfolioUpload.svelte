<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Button, FileDropZone, Modal } from '$lib/components/ui';
  import { notifyError, notifyInfo, notifySuccess } from '$lib/stores/notices';
  import { ApiError, importPortfolioFile, type PortfolioImportResult } from '$lib/money/api';

  interface Props {
    /** Runs after any import that changed data (ok / replace / force). */
    onImported?: () => void;
    /** The empty-state prose inside the drop zone; defaults to the Fidelity copy. */
    prompt?: Snippet;
    /** Importer-registry source name; absent = server-side auto-detect. */
    source?: string;
  }

  let { onImported, prompt, source }: Props = $props();

  let file: File | null = $state(null);
  let importing = $state(false);
  let collision: { existing: { id: number; exported_at: string }; file: File } | null =
    $state(null);

  function describeResult(result: PortfolioImportResult): string {
    if (result.imported != null) {
      const dup = result.duplicates ? ` (${result.duplicates} already imported)` : '';
      return `Imported ${result.imported} snapshot${result.imported === 1 ? '' : 's'}${dup}`;
    }
    return `Imported ${result.position_count} positions`;
  }

  async function runImport(picked: File, opts?: { replace?: number; force?: boolean }) {
    importing = true;
    try {
      const result = await importPortfolioFile(picked, { ...opts, source });
      if (result.status === 'date_collision' && result.existing) {
        collision = { existing: result.existing, file: picked };
        return;
      }
      notifySuccess(describeResult(result), { key: 'portfolio:import' });
      if (result.auto_classified?.length) {
        notifyInfo(
          `Auto-classified ${result.auto_classified.map((c) => c.symbol).join(', ')} — review in Money settings`,
          { key: 'portfolio:autoclassified' },
        );
      }
      if (result.unclassified_symbols?.length) {
        notifyInfo(
          `Unclassified symbols: ${result.unclassified_symbols.join(', ')} — classify them in Money settings`,
          { key: 'portfolio:unclassified' },
        );
      }
      for (const warning of result.warnings ?? []) {
        notifyInfo(warning, { key: 'portfolio:import-warning' });
      }
      file = null;
      onImported?.();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.payload?.status === 'duplicate') {
        notifyInfo('Already imported — this file matches an existing snapshot', {
          key: 'portfolio:import',
        });
        file = null;
        return;
      }
      notifyError(e instanceof Error ? e.message : 'Import failed', { key: 'portfolio:import' });
    } finally {
      importing = false;
    }
  }

  $effect(() => {
    if (file && !importing && !collision) {
      void runImport(file);
    }
  });

  function resolveCollision(mode: 'replace' | 'keep') {
    const pending = collision;
    collision = null;
    if (!pending) return;
    void runImport(
      pending.file,
      mode === 'replace' ? { replace: pending.existing.id } : { force: true },
    );
  }

  function cancelCollision() {
    collision = null;
    file = null;
  }
</script>

<FileDropZone bind:file accept=".csv,text/csv">
  {#if importing}
    Importing…
  {:else if prompt}
    {@render prompt()}
  {:else}
    Drop a Fidelity Portfolio Positions CSV here, or pick a file.
    <span class="hint">Re-importing the same file is safe — duplicates are skipped.</span>
  {/if}
</FileDropZone>

<Modal
  open={collision !== null}
  title="A snapshot from this day already exists"
  onOpenChange={(open) => {
    if (!open) cancelCollision();
  }}
>
  {#if collision}
    <p class="collision-text">
      Snapshot #{collision.existing.id} was exported on
      {collision.existing.exported_at.slice(0, 10)} with different content. Replace it with this file,
      or keep both?
    </p>
  {/if}
  {#snippet footer()}
    <Button variant="secondary" size="sm" onclick={cancelCollision}>Cancel</Button>
    <Button variant="secondary" size="sm" onclick={() => resolveCollision('keep')}>
      Keep both
    </Button>
    <Button variant="primary" size="sm" onclick={() => resolveCollision('replace')}>Replace</Button>
  {/snippet}
</Modal>

<style>
  .collision-text {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
</style>
