<script lang="ts">
  import { Badge, Card, KebabMenu, type KebabItem } from '@minot/ui';
  import type { DocumentEntity, HealthDocument } from '$lib/api';
  import { documentName, openDocumentImage, formatBytes, mimeLabel } from '$lib/health/documents';
  import { formatDate } from '@minot/ui/dateFormat';

  interface Props {
    doc: HealthDocument;
    /** The record this card is being shown under, if any. */
    entityType?: DocumentEntity;
    entityId?: number;
    /** Omitted on a read-only surface (no detach target). */
    onDetach?: (doc: HealthDocument) => void;
    onDelete?: (doc: HealthDocument) => void;
  }

  let { doc, entityType, entityId, onDetach, onDelete }: Props = $props();

  const name = $derived(documentName(doc));

  const menu = $derived.by(() => {
    const items: KebabItem[] = [
      { label: 'Open', href: doc.url, onClick: (event) => openDocumentImage(event, doc) },
    ];
    if (onDetach && entityType && entityId) {
      items.push({ label: 'Detach', onSelect: () => onDetach(doc) });
    }
    if (onDelete) {
      items.push({ label: 'Delete', danger: true, onSelect: () => onDelete(doc) });
    }
    return items;
  });
</script>

<li class="doc-card">
  <Card as="article">
    <div class="card-head">
      <!-- The whole card is not a link: the kebab lives inside it, and a
         nested interactive element inside an anchor is not addressable. -->
      <a
        class="name title-card"
        href={doc.url}
        onclick={(event) => openDocumentImage(event, doc)}
        title={name}>{name}</a
      >
      <KebabMenu items={menu} ariaLabel="Document actions" />
    </div>
    <div class="tags">
      <Badge tone="subtle" uppercase={false}>{mimeLabel(doc.mime)}</Badge>
      <Badge tone="subtle" uppercase={false}>{doc.source}</Badge>
    </div>
    <div class="doc-meta caption">
      <span>{formatBytes(doc.byte_size)}</span>
      <span>{formatDate(doc.created_at)}</span>
    </div>
    {#if doc.notes}
      <p class="notes caption">{doc.notes}</p>
    {/if}
  </Card>
</li>

<style>
  /* The list item is the grid cell; the card fills it. */
  .doc-card {
    display: flex;
    min-width: 0;
  }

  .doc-card > :global(.ui-card) {
    flex: 1;
    gap: var(--space-2);
    min-width: 0;
  }

  /* flex-start: a long document name wraps, and a centred row would float
     the kebab against the middle of it. */
  .doc-card .card-head {
    align-items: flex-start;
  }

  .name {
    margin: 0;
    color: var(--accent-blue);
    line-height: var(--leading-snug);
    text-decoration: none;
    /* A long scanner-generated filename must not widen its grid column. */
    overflow-wrap: anywhere;
    min-width: 0;
  }

  .name:hover {
    text-decoration: underline;
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .doc-meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .notes {
    margin: 0;
    line-height: var(--leading-snug);
  }
</style>
