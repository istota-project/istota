<script lang="ts">
  import { onMount } from 'svelte';
  import { ConfirmDialog } from '$lib/components/ui';
  import {
    deleteDocument,
    getDocument,
    listDocuments,
    unlinkDocument,
    uploadDocument,
    type DocumentEntity,
    type HealthDocument,
  } from '$lib/api';
  import { documentName, otherRecordsWarning } from '$lib/health/documents';
  import DocumentCard from './DocumentCard.svelte';

  interface Props {
    entityType: DocumentEntity;
    entityId: number;
    /** Server-supplied initial list. The component owns it after mount. */
    documents?: HealthDocument[];
    /**
     * Fetch the list on mount instead of trusting `documents`. For a surface
     * whose parent payload doesn't carry them — the conditions modal, which
     * opens off a list row rather than a detail response.
     */
    autoload?: boolean;
    /** Called after any change, so a parent list can refresh its counts. */
    onChanged?: () => void;
  }

  let { entityType, entityId, documents = [], autoload = false, onChanged }: Props = $props();

  // The parent's `documents` is the source of truth until this component
  // changes something; `fetched` then takes over. Modelling it that way (an
  // override rather than a copy) means a parent reload can't be silently
  // ignored, and the entity-change reset is one line.
  let fetched = $state<HealthDocument[] | null>(null);
  let error = $state('');
  let uploading = $state(false);
  let dragging = $state(false);
  let fileInput = $state<HTMLInputElement | null>(null);

  const entity = $derived({ type: entityType, id: entityId });
  const entityKey = $derived(`${entityType}:${entityId}`);
  const docs = $derived(fetched ?? documents);

  $effect(() => {
    entityKey; // Track: a different record means our override is stale.
    fetched = null;
  });

  onMount(() => {
    if (autoload) void refresh();
  });

  let confirmOpen = $state(false);
  let confirmTitle = $state('');
  let confirmMessage = $state('');
  let confirmLabel = $state('Delete');
  let pendingAction: (() => Promise<void>) | null = $state(null);

  async function refresh() {
    try {
      const out = await listDocuments(entity);
      fetched = out.documents;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load documents';
    }
    onChanged?.();
  }

  async function upload(files: FileList | File[] | null) {
    if (!files || files.length === 0) return;
    error = '';
    uploading = true;
    try {
      for (const file of Array.from(files)) {
        await uploadDocument(file, entity);
      }
      await refresh();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Upload failed';
    } finally {
      uploading = false;
      if (fileInput) fileInput.value = '';
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragging = false;
    upload(e.dataTransfer?.files ?? null);
  }

  function askDetach(doc: HealthDocument) {
    confirmTitle = 'Detach document';
    confirmMessage =
      `Are you sure you want to detach "${documentName(doc)}" from this record? ` +
      'The document itself is kept and stays attached to any other record.';
    confirmLabel = 'Detach';
    pendingAction = async () => {
      await unlinkDocument(doc.id, entity);
      await refresh();
    };
    confirmOpen = true;
  }

  async function askDelete(doc: HealthDocument) {
    // The consequence depends on what else points at it, so read the links
    // before wording the warning rather than guessing.
    let otherLinks = 0;
    try {
      const detail = await getDocument(doc.id);
      otherLinks = detail.links.filter(
        (l) => !(l.entity_type === entityType && l.entity_id === entityId),
      ).length;
    } catch {
      otherLinks = 0;
    }
    confirmTitle = 'Delete document';
    const extra = otherRecordsWarning(otherLinks);
    confirmMessage =
      `Are you sure you want to delete "${documentName(doc)}"? ` +
      `This cannot be undone.${extra ? ` ${extra}` : ''}`;
    confirmLabel = 'Delete';
    pendingAction = async () => {
      await deleteDocument(doc.id);
      await refresh();
    };
    confirmOpen = true;
  }

  async function runPending() {
    const action = pendingAction;
    pendingAction = null;
    confirmOpen = false;
    if (!action) return;
    error = '';
    try {
      await action();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Action failed';
    }
  }
</script>

{#if error}
  <div class="doc-error">{error}</div>
{/if}

<ul class="doc-grid">
  {#each docs as doc (doc.id)}
    <DocumentCard {doc} {entityType} {entityId} onDetach={askDetach} onDelete={askDelete} />
  {/each}
  <li>
    <!-- The drop zone is the last grid cell rather than a separate band, so
         "where documents go" and "where documents are" are one target. -->
    <button
      type="button"
      class="dropzone"
      class:dragging
      disabled={uploading}
      ondragover={(e) => {
        e.preventDefault();
        dragging = true;
      }}
      ondragleave={() => (dragging = false)}
      ondrop={onDrop}
      onclick={() => fileInput?.click()}
    >
      {uploading ? 'Uploading…' : 'Drop a file, or click to choose'}
    </button>
    <input
      bind:this={fileInput}
      class="hidden-input"
      type="file"
      multiple
      accept="image/*,application/pdf,text/plain"
      onchange={(e) => upload((e.currentTarget as HTMLInputElement).files)}
    />
  </li>
</ul>

<ConfirmDialog
  bind:open={confirmOpen}
  title={confirmTitle}
  message={confirmMessage}
  {confirmLabel}
  confirmVariant="danger"
  onConfirm={runPending}
  onCancel={() => (pendingAction = null)}
/>

<style>
  .doc-grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--space-2);
  }

  @media (max-width: 1100px) {
    .doc-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @media (max-width: 768px) {
    .doc-grid {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  .dropzone {
    font: inherit;
    width: 100%;
    height: 100%;
    min-height: 4.5rem;
    padding: var(--space-3) var(--space-4);
    background: transparent;
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-card);
    color: var(--text-dim);
    font-size: var(--text-xs);
    cursor: pointer;
  }

  .dropzone:hover:not(:disabled),
  .dropzone.dragging {
    border-color: var(--accent-blue);
    color: var(--text-muted);
  }

  .dropzone:disabled {
    cursor: default;
  }

  .hidden-input {
    display: none;
  }

  .doc-error {
    margin-bottom: var(--space-2);
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }
</style>
