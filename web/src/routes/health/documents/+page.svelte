<script lang="ts">
  /**
   * Every uploaded health document, and what it is attached to (ISSUE-423).
   *
   * The other three document surfaces hang off an entity — the encounter page,
   * the immunization detail page, the conditions modal — so finding a document
   * meant already knowing which visit or condition it had been filed under,
   * and a document whose links were never made, or were removed, appeared on
   * no page at all while still sitting on disk until the orphan sweep took it.
   * This is the view that has no entity: the table is the whole store.
   *
   * Associations come off the list payload rather than a request per row —
   * `GET /documents` carries `links` for exactly this table, and
   * `tests/test_health_routes.py::...::test_list_link_labels_cost_no_query_per_row`
   * is what holds that. A chip's label is the server's spelling, so this page
   * and `GET /documents/{id}` cannot disagree about what a document is on.
   */
  import { onMount, tick, untrack } from 'svelte';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  // A plain `Set` in `$state` is not deeply reactive in Svelte 5 — mutating it
  // would not re-render the rows reading `busy.has(...)`.
  import { SvelteSet } from 'svelte/reactivity';
  import {
    deleteDocument,
    getDocument,
    linkDocument,
    listDiagnoses,
    listDocuments,
    listEncounters,
    listImmunizations,
    unlinkDocument,
    type Diagnosis,
    type DocumentEntity,
    type DocumentLink,
    type Encounter,
    type HealthDocument,
    type Immunization,
  } from '$lib/api';
  import {
    Badge,
    Button,
    NoticeBanner,
    Chip,
    ConfirmDialog,
    EmptyState,
    Field,
    KebabMenu,
    Modal,
    Select,
    type KebabItem,
    type SelectOption,
    PageLoading,
  } from '$lib/components/ui';
  import {
    attachOptions,
    attachPoolNotice,
    attachedRecordsWarning,
    documentName,
    openDocumentImage,
    entityTypeLabel,
    fetchAllPages,
    formatBytes,
    MAX_PAGES,
    PAGE_SIZES,
    sourceLabel,
    truncationNotice,
    type TruncationFlags,
  } from '$lib/health/documents';
  import { formatDate } from '$lib/dateFormat';
  import { isPlainClick } from '$lib/fileViewer/links';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import { notifyError, notifyWarning } from '$lib/stores/notices';

  const entityTypes: SelectOption[] = [
    { value: 'encounter', label: 'Visit' },
    { value: 'diagnosis', label: 'Condition' },
    { value: 'immunization', label: 'Vaccination' },
  ];

  // `loading` is the first load only. A reload after an edit sets `refreshing`
  // instead, which dims the table rather than replacing it with the whole-pane
  // message — swapping a populated table for "Loading…" on every detach makes
  // the row-level busy state below unreachable and the page flash.
  let loading = $state(true);
  let refreshing = $state(false);
  let loadError = $state('');
  let documents = $state<HealthDocument[]>([]);
  // Which of the four walks ran out of pages before it ran out of records, one
  // flag each. On `documents` it means the header must not state a total and
  // the unattached count is a count of what is loaded rather than of what
  // exists. On the three pools it means the attach picker is short — a
  // different consequence on a different surface, which is why this is not one
  // boolean (ISSUE-441).
  let truncated = $state<TruncationFlags>({
    documents: false,
    encounters: false,
    diagnoses: false,
    immunizations: false,
  });
  let encounters = $state<Encounter[]>([]);
  let diagnoses = $state<Diagnosis[]>([]);
  let immunizations = $state<Immunization[]>([]);

  let unattachedOnly = $state(false);
  let requestedDocumentId = $state<number | null>(null);
  let selectedDocumentId = $state<number | null>(null);
  let documentList: HTMLDivElement | undefined = $state();
  const urlSelection = createUrlSelection<{ id: number | null }>({
    key: 'healthDocument',
    params: ['id'],
    encode: ({ id }): Record<string, string> => (id === null ? {} : { id: String(id) }),
    decode: (params) => ({
      id: /^\d+$/.test(params.id ?? '') && Number(params.id) > 0 ? Number(params.id) : null,
    }),
    read: () => null,
    apply: ({ id }) => {
      requestedDocumentId = id;
    },
  });
  urlSelection.start();

  async function revealDocument(id: number) {
    selectedDocumentId = null;
    try {
      if (!byId.has(id)) {
        const result = await getDocument(id);
        if (requestedDocumentId !== id) return;
        documents = [...documents, { ...result.document, links: result.links }];
      }
      if (requestedDocumentId !== id) return;
      unattachedOnly = false;
      selectedDocumentId = id;
      await tick();
      if (requestedDocumentId !== id) return;
      documentList
        ?.querySelector(`[data-document-id="${id}"]`)
        ?.scrollIntoView({ block: 'center' });
    } catch {
      if (requestedDocumentId === id) notifyError("Couldn't locate that document.");
    }
  }

  $effect(() => {
    const id = requestedDocumentId;
    if (loading) return;
    if (id === null) selectedDocumentId = null;
    else
      untrack(() => {
        void revealDocument(id);
      });
  });

  // A set, not a scalar: two mutations on different rows are both reachable
  // (each row's buttons are disabled only by its own id), and one scalar means
  // whichever finishes first clears the other's guard.
  let busy = $state(new SvelteSet<number>());
  // Both dialogs address a document by **id**, never by holding the object.
  // `load()` replaces `documents` wholesale, so a held reference keeps the
  // pre-reload `links` — which would let the attach picker offer a record that
  // is already attached, and the delete confirmation name a record count that
  // no longer holds. That count is the one number the user is acting on.
  let attachForId = $state<number | null>(null);
  let attachType = $state<DocumentEntity>('encounter');
  let attachId = $state('');
  let attachError = $state('');
  let deleteTargetId = $state<number | null>(null);

  const pool = $derived({ encounters, diagnoses, immunizations });

  const byId = $derived(new Map(documents.map((d) => [d.id, d])));
  const attachFor = $derived(attachForId === null ? null : (byId.get(attachForId) ?? null));
  const deleteTarget = $derived(
    deleteTargetId === null ? null : (byId.get(deleteTargetId) ?? null),
  );

  const rows = $derived(
    unattachedOnly ? documents.filter((d) => links(d).length === 0) : documents,
  );

  const unattachedCount = $derived(documents.filter((d) => links(d).length === 0).length);

  const attachChoices = $derived(
    attachFor ? attachOptions(attachType, pool, links(attachFor), formatDate) : [],
  );

  // Two surfaces, because the banner is behind the modal and cannot warn
  // somebody who is already picking from a short list.
  const pageNotice = $derived(truncationNotice(truncated));
  const poolNotice = $derived(attachPoolNotice(attachType, truncated));

  /**
   * A document's links, defaulting to none.
   *
   * `HealthDocument.links` is optional on the type because the upload
   * acknowledgement omits it, but every row here comes from a listing, which
   * always carries it — including as an explicit `[]` for the unattached
   * documents this page exists to surface.
   */
  function links(doc: HealthDocument): DocumentLink[] {
    return doc.links ?? [];
  }

  /**
   * Read everything the page shows.
   *
   * `quiet` is what a reload after an edit passes: it keeps the table mounted
   * and merely dims it, where the initial load owns the whole pane. Without
   * that split every detach blanks the page, and the row-level busy state
   * below is never on screen long enough to be seen.
   *
   * Every list is walked to the end rather than taking the server's default
   * page. This page states a total and derives an orphan count from what it
   * holds, and `list_documents` orders newest-first — so a silent cut drops
   * the *oldest* documents, which are exactly the ones the 24h orphan sweep is
   * about to delete and the ones this view exists to surface.
   */
  async function load({ quiet = false } = {}) {
    if (quiet) refreshing = true;
    else loading = true;
    loadError = '';
    // Named at the call site rather than left to the helper's defaults, so the
    // page's paging policy is visible here and a test can shrink it without
    // rendering twenty thousand rows to reach the ceiling. One size per list,
    // because each endpoint caps `limit` at its own value and asking for more
    // is a 422 that fails this whole `Promise.all`.
    const paged = (list: keyof typeof PAGE_SIZES) => ({
      pageSize: PAGE_SIZES[list],
      maxPages: MAX_PAGES,
    });
    try {
      const [docs, enc, dx, imm] = await Promise.all([
        fetchAllPages(async (offset, limit) => {
          const out = await listDocuments(undefined, { limit, offset });
          return out.documents;
        }, paged('documents')),
        fetchAllPages(async (offset, limit) => {
          const out = await listEncounters({ limit, offset });
          return out.encounters;
        }, paged('encounters')),
        fetchAllPages(async (offset, limit) => {
          const out = await listDiagnoses({ status: 'all', limit, offset });
          return out.diagnoses;
        }, paged('diagnoses')),
        fetchAllPages(async (offset, limit) => {
          const out = await listImmunizations({ limit, offset });
          return out.immunizations;
        }, paged('immunizations')),
      ]);
      documents = docs.items;
      encounters = enc.items;
      diagnoses = dx.items;
      immunizations = imm.items;
      truncated = {
        documents: docs.truncated,
        encounters: enc.truncated,
        diagnoses: dx.truncated,
        immunizations: imm.truncated,
      };
      // The filter's chip is only rendered while something is unattached, so
      // attaching the last loose document would otherwise leave the filter on
      // with no control to turn it off and an empty table behind it — reached
      // by the exact workflow this view is for.
      if (unattachedCount === 0) unattachedOnly = false;
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load documents';
    } finally {
      loading = false;
      refreshing = false;
    }
  }

  /** Run a mutation on one row, then re-read. Owns the per-row busy guard. */
  async function mutate(id: number, action: () => Promise<void>, failure: string) {
    busy.add(id);
    try {
      await action();
      await load({ quiet: true });
    } catch (e) {
      notifyError(e instanceof Error ? e.message : failure);
    } finally {
      busy.delete(id);
    }
  }

  function detach(doc: HealthDocument, link: DocumentLink) {
    return mutate(
      doc.id,
      () => unlinkDocument(doc.id, { type: link.entity_type, id: link.entity_id }).then(() => {}),
      'Failed to detach',
    );
  }

  function openAttach(doc: HealthDocument) {
    attachForId = doc.id;
    attachType = 'encounter';
    attachId = '';
    attachError = '';
  }

  function pickType(v: string) {
    attachType = v as DocumentEntity;
    // The chosen record belongs to the old type; keeping it would attach the
    // document to whatever shares that id in the new one.
    attachId = '';
  }

  async function attach() {
    const doc = attachFor;
    const id = Number(attachId);
    if (!doc || !Number.isFinite(id) || id <= 0) return;
    const target = { type: attachType, id };
    attachError = '';
    busy.add(doc.id);
    try {
      const out = await linkDocument(doc.id, target);
      attachForId = null;
      await load({ quiet: true });
      // `link_document` is INSERT OR IGNORE, so an already-attached record is
      // a success with `created: false`. The picker filters those out, so
      // seeing one means the pool was stale — say so rather than reporting a
      // change that did not happen.
      if (!out.created) {
        notifyWarning('That document was already attached to this record.');
      }
    } catch (e) {
      attachError = e instanceof Error ? e.message : 'Failed to attach';
    } finally {
      busy.delete(doc.id);
    }
  }

  async function confirmDelete() {
    const doc = deleteTarget;
    deleteTargetId = null;
    if (!doc) return;
    await mutate(doc.id, () => deleteDocument(doc.id).then(() => {}), 'Failed to delete');
  }

  function previewDocument(event: MouseEvent, doc: HealthDocument) {
    if (!isPlainClick(event)) return;
    if (doc.mime === 'application/pdf') {
      event.preventDefault();
      viewer.openFile(doc.url, {
        kind: 'url',
        url: doc.url,
        name: documentName(doc),
        mime: 'application/pdf',
      });
    } else {
      openDocumentImage(event, doc);
    }
  }

  function menu(doc: HealthDocument): KebabItem[] {
    return [
      ...(doc.mime === 'application/pdf' || doc.mime.startsWith('image/')
        ? [
            {
              label: 'Preview',
              href: doc.url,
              onClick: (event: MouseEvent) => previewDocument(event, doc),
            },
          ]
        : []),
      { label: 'Download', href: doc.url, download: documentName(doc) },
      { label: 'Attach to a record', onSelect: () => openAttach(doc) },
      { label: 'Delete', danger: true, onSelect: () => (deleteTargetId = doc.id) },
    ];
  }

  onMount(() => {
    requestedDocumentId = urlSelection.current()?.id ?? null;
    void load();
  });
</script>

{#if !loading && !loadError}
  <!-- Held back while loading so the pane centres its message rather than
       centring it in the space left under this header. -->
  <div class="header">
    <div>
      <h1 class="title-page">Documents</h1>
      <p class="sub caption">
        <!-- Never "N on file" once the walk stopped early: the count would be
             a claim about the store rather than about what is loaded. -->
        {#if truncated.documents}
          showing the {documents.length} most recent
        {:else}
          {documents.length}
          {documents.length === 1 ? 'document' : 'documents'} on file
        {/if}
      </p>
    </div>
    {#if unattachedCount > 0 || unattachedOnly}
      <!-- Rendered while the filter is on even at zero, so turning it off is
           always reachable. `aria-pressed` because `checked` only draws the
           state; see the Chip component's own note. -->
      <Chip
        checked={unattachedOnly}
        aria-pressed={unattachedOnly}
        onclick={() => (unattachedOnly = !unattachedOnly)}
      >
        {unattachedCount} unattached
      </Chip>
    {/if}
  </div>
{/if}

<!-- Held back on a load failure alongside the header above it: that branch
     replaces the pane, so there is no table and no picker for this to be
     about, and the flags are whatever the last successful load left. -->
{#if pageNotice && !loadError}
  <div class="page-notice">
    <NoticeBanner title="Some records are not shown" variant="warn" collapsed={false}
      >{pageNotice}</NoticeBanner
    >
  </div>
{/if}

{#if loading}
  <PageLoading />
{:else if loadError}
  <div class="center-msg error">{loadError}</div>
{:else if documents.length === 0}
  <EmptyState>
    Nothing uploaded yet. Documents attach to a visit, a condition or a vaccination from those
    pages, and every one of them shows up here.
  </EmptyState>
{:else if rows.length === 0}
  <EmptyState>Every document is attached to a record.</EmptyState>
{:else}
  <div class="table-scroll" class:refreshing bind:this={documentList}>
    <!-- svelte-ignore a11y_no_redundant_roles (Explicit roles preserve table semantics when Safari lays out rows as grids.) -->
    <table class="grid documents-grid" aria-label="Health documents" role="table">
      <thead role="rowgroup">
        <tr role="row">
          <th scope="col" class="document-column">Document</th>
          <th scope="col" class="size-column">Size</th>
          <th scope="col" class="added-column">Added</th>
          <th scope="col" class="links-column">Attached to</th>
          <th scope="col" class="actions"><span class="visually-hidden">Actions</span></th>
        </tr>
      </thead>
      <tbody role="rowgroup">
        {#each rows as doc (doc.id)}
          <tr
            role="row"
            data-document-id={doc.id}
            class:search-target={selectedDocumentId === doc.id}
            class:busy={busy.has(doc.id)}
          >
            <td class="document-column">
              <div class="document-title">
                <a
                  class="name"
                  href={doc.url}
                  onclick={(event) => previewDocument(event, doc)}
                  title={documentName(doc)}>{documentName(doc)}</a
                >
                <Badge size="sm">{sourceLabel(doc.source)}</Badge>
                <span class="mobile-meta caption"
                  >{formatBytes(doc.byte_size)}<span aria-hidden="true"> · </span><time
                    datetime={doc.created_at}>{formatDate(doc.created_at)}</time
                  ></span
                >
              </div>
              {#if doc.notes}<p class="notes caption" title={doc.notes}>{doc.notes}</p>{/if}
            </td>
            <td class="size-column">{formatBytes(doc.byte_size)}</td>
            <td class="added-column"
              ><time datetime={doc.created_at}>{formatDate(doc.created_at)}</time></td
            >
            <td class="links-column">
              <div class="links">
                {#each links(doc) as link (`${link.entity_type}:${link.entity_id}`)}
                  <span
                    class="link-chip"
                    data-kind={link.entity_type}
                    title="{entityTypeLabel(link.entity_type)}: {link.label}"
                  >
                    <span class="visually-hidden">{entityTypeLabel(link.entity_type)}</span>
                    <span class="link-label">{link.label}</span>
                    <button
                      type="button"
                      class="detach touch-target"
                      disabled={busy.has(doc.id)}
                      onclick={() => detach(doc, link)}
                      aria-label="Detach from {link.label}">×</button
                    >
                  </span>
                {/each}
                {#if links(doc).length === 0}
                  <button
                    type="button"
                    class="attach touch-target"
                    disabled={busy.has(doc.id)}
                    onclick={() => openAttach(doc)}
                  >
                    + Attach
                  </button>
                {/if}
              </div>
            </td>
            <td class="actions">
              <KebabMenu items={menu(doc)} ariaLabel="Document actions" />
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

{#if attachFor}
  <Modal
    open={true}
    title="Attach {documentName(attachFor)}"
    description="Pick the record this document is evidence for. To move it, attach the new record and detach the old one."
    width="30rem"
    onOpenChange={(open) => {
      if (!open) attachForId = null;
    }}
  >
    <div class="attach-form">
      <!-- `labelled={false}` on both: a Select's trigger is a <button>, so
           inside a <label> it becomes that label's implicit control and
           clicking the caption opens the dropdown. See web/AGENTS.md. -->
      <Field label="Record type" labelled={false}>
        <Select
          value={attachType}
          options={entityTypes}
          onValueChange={pickType}
          ariaLabel="Record type"
          fullWidth
        />
      </Field>
      <!-- `warning` rather than `hint`: a hover popover is discoverable, not
           seen, and a short pool is the reason the record you are looking for
           is not in the list. -->
      <Field label="Record" warning={poolNotice} labelled={false}>
        <Select
          value={attachId}
          options={attachChoices}
          onValueChange={(v) => (attachId = v)}
          placeholder={attachChoices.length ? 'Choose a record…' : 'Nothing left to attach to'}
          disabled={attachChoices.length === 0}
          ariaLabel="Record"
          fullWidth
        />
      </Field>
      {#if attachError}
        <p class="form-error" role="alert">{attachError}</p>
      {/if}
    </div>
    {#snippet footer()}
      <Button onclick={() => (attachForId = null)}>Cancel</Button>
      <Button
        variant="primary"
        onclick={attach}
        disabled={!attachId || (attachFor !== null && busy.has(attachFor.id))}
      >
        Attach
      </Button>
    {/snippet}
  </Modal>
{/if}

<ConfirmDialog
  open={deleteTarget !== null}
  title="Delete document"
  message={deleteTarget
    ? `Are you sure you want to delete "${documentName(deleteTarget)}"? ` +
      `This cannot be undone. ${attachedRecordsWarning(links(deleteTarget).length)}`
    : ''}
  confirmLabel="Delete"
  confirmVariant="danger"
  onConfirm={confirmDelete}
  onCancel={() => (deleteTargetId = null)}
/>

<style>
  tr.search-target {
    background: var(--status-info-bg);
  }

  .header {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
  }

  .sub {
    margin: var(--space-1) 0 0;
  }

  .name {
    color: var(--accent-blue);
    text-decoration: none;
    font-weight: var(--weight-medium);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .name:hover {
    text-decoration: underline;
  }

  .document-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .document-title :global(.badge) {
    flex-shrink: 0;
  }

  .mobile-meta {
    display: none;
  }
  .mobile-meta > span {
    margin-inline: var(--space-1);
  }

  .page-notice {
    margin-bottom: var(--space-3);
  }

  .notes {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    margin: var(--space-1) 0 0;
    line-height: var(--leading-snug);
  }

  .documents-grid {
    display: block;
  }
  .documents-grid thead {
    display: block;
  }
  .documents-grid tbody {
    display: grid;
    grid-auto-rows: 1fr;
  }
  .documents-grid tr {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 5rem 8rem minmax(12rem, 24%) 2rem;
  }
  .grid.documents-grid td {
    min-width: 0;
    align-content: center;
  }

  .grid.documents-grid th:first-child,
  .grid.documents-grid td:first-child {
    padding-left: 0;
  }

  .grid.documents-grid th:last-child,
  .grid.documents-grid td:last-child {
    padding-right: 0;
  }

  .grid.documents-grid .size-column {
    text-align: right;
    font-variant-numeric: tabular-nums;
    color: var(--text-muted);
  }

  .added-column {
    white-space: nowrap;
  }
  td.added-column {
    color: var(--text-muted);
  }
  .actions {
    text-align: right;
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1);
  }

  .link-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--text-xs);
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-pill);
    padding: 0 var(--space-1) 0 var(--space-2);
    min-height: var(--control-height-sm);
    max-width: 100%;
    min-width: 0;
  }

  .link-chip[data-kind='encounter'] {
    background: var(--category-blue-bg);
    color: var(--category-blue-fg);
  }

  .link-chip[data-kind='diagnosis'] {
    background: var(--category-purple-bg);
    color: var(--category-purple-fg);
  }

  .link-chip[data-kind='immunization'] {
    background: var(--category-teal-bg);
    color: var(--category-teal-fg);
  }

  .link-label {
    color: inherit;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .detach,
  .attach {
    flex-shrink: 0;
    font: inherit;
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-dim);
    padding: 0 var(--space-1);
    border-radius: var(--radius-pill);
  }

  .detach {
    color: inherit;
  }

  .detach:hover:not(:disabled) {
    color: var(--status-danger-fg);
  }

  .attach {
    font-size: var(--text-xs);
    border: 1px dashed var(--border-default);
    padding: var(--space-1) var(--space-2);
  }

  .attach:hover:not(:disabled) {
    border-color: var(--accent-blue);
    color: var(--text-muted);
  }

  .detach:disabled,
  .attach:disabled {
    cursor: default;
    opacity: var(--disabled-opacity);
  }

  .busy {
    opacity: 0.6;
  }

  /* A reload after an edit dims the table rather than replacing it, so the
     rows stay in place and readable while the new ones arrive. */
  .refreshing {
    opacity: 0.7;
  }

  .attach-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  @media (max-width: 768px) {
    .documents-grid thead {
      display: none;
    }
    .documents-grid tr {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-content: start;
      gap: var(--space-2) var(--space-3);
      padding: var(--space-3) 0;
      border-bottom: 1px solid var(--border-subtle);
    }
    .grid.documents-grid tbody tr td {
      align-content: start;
      width: auto;
      min-width: 0;
      padding: 0;
      border: 0;
    }
    .grid.documents-grid tbody tr .size-column,
    .grid.documents-grid tbody tr .added-column {
      display: none;
    }
    .document-title {
      flex-wrap: wrap;
      row-gap: var(--space-1);
    }
    .name {
      flex-basis: 100%;
    }
    .mobile-meta {
      display: inline;
    }
    .links-column {
      grid-column: 1 / -1;
    }
    .actions {
      grid-column: 2;
      grid-row: 1;
      align-self: start;
    }
    .links {
      gap: var(--space-1);
    }
    .link-chip {
      border-radius: var(--radius-sm);
    }
    .header {
      margin-bottom: var(--space-2);
    }
  }
</style>
