<script lang="ts">
  import {
    getInvoices,
    settleInvoiceReview,
    dismissInvoiceReview,
    type InvoicePaymentReview,
    getInvoiceDetails,
    markInvoicePaid,
    markInvoicePending,
    invoicePdfUrl,
    getClients,
    getBusinessSettings,
    type InvoiceRow,
    type InvoiceDetailItem,
    type ClientRow,
    type EntityRow,
    type ServiceRow,
  } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import {
    Badge,
    Button,
    EmptyState,
    Alert,
    KebabMenu,
    type KebabItem,
    SortHeader,
    LoadState,
  } from '@minot/ui';
  import VoidInvoiceDialog from '$lib/components/money/VoidInvoiceDialog.svelte';
  import GenerateInvoicesDialog from '$lib/components/money/GenerateInvoicesDialog.svelte';
  import ManualInvoiceDialog from '$lib/components/money/ManualInvoiceDialog.svelte';
  import { createTableSort, sortRows } from '@minot/ui/ui/tableSort.svelte';
  import { billingStatusVariant } from '$lib/money/status';
  import { workExportItems } from '$lib/money/workExport';
  import { Download } from '@lucide/svelte';
  import { formatDate } from '@minot/ui/dateFormat';
  import { viewer } from '$lib/fileViewer/store.svelte';
  import { isPlainClick } from '$lib/fileViewer/links';
  import { formatDecimal as formatAmount } from '$lib/format';

  let invoices: InvoiceRow[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let invoiceCount = $state(0);
  let outstandingCount = $state(0);
  type SortKey = 'date' | 'number';
  const sort = createTableSort<SortKey>('money.invoices.sort', ['date', 'number'], {
    initial: { key: 'date', direction: 'descending' },
    firstDirection: 'descending',
  });
  let reviews: InvoicePaymentReview[] = $state([]);
  let reviewsCollapsed = $state(true);
  let busyReview = $state('');
  let reviewError = $state('');

  async function handleReview(review: InvoicePaymentReview, invoiceNumber?: string) {
    if (busyReview) return;
    busyReview = review.ledger_txn_id;
    reviewError = '';
    try {
      if (invoiceNumber) await settleInvoiceReview(review.ledger_txn_id, invoiceNumber);
      else await dismissInvoiceReview(review.ledger_txn_id);
      await load();
    } catch (e) {
      reviewError = e instanceof Error ? e.message : 'Failed to update payment review';
    } finally {
      busyReview = '';
    }
  }
  // Invoice number currently running an action (disables its menu items).
  let busyInvoice = $state('');

  async function handleMarkPaid(inv: InvoiceRow) {
    busyInvoice = inv.invoice_number;
    try {
      await markInvoicePaid(inv.invoice_number);
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to mark invoice paid';
    } finally {
      busyInvoice = '';
    }
  }

  async function handleMarkPending(inv: InvoiceRow) {
    busyInvoice = inv.invoice_number;
    try {
      await markInvoicePending(inv.invoice_number);
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to mark invoice pending';
    } finally {
      busyInvoice = '';
    }
  }

  function handleDownloadPdf(inv: InvoiceRow) {
    window.open(invoicePdfUrl(inv.invoice_number), '_blank');
  }

  // A link item: KebabMenu focuses its trigger before onClick, so the viewer
  // returns focus there rather than to the menu item it removed.
  function previewItem(inv: InvoiceRow): KebabItem {
    if (!inv.has_pdf) return { label: 'Preview', onSelect: () => {}, disabled: true };
    const url = invoicePdfUrl(inv.invoice_number);
    return {
      label: 'Preview',
      href: url,
      onClick: (event) => {
        if (!isPlainClick(event)) return;
        event.preventDefault();
        viewer.openFile(url, {
          kind: 'url',
          url,
          name: inv.invoice_number,
          mime: 'application/pdf',
        });
      },
    };
  }

  function menuItems(inv: InvoiceRow): KebabItem[] {
    const busy = busyInvoice === inv.invoice_number;
    const items: KebabItem[] = [];
    // A voided invoice is a record, not a bill: nothing to pay, nothing to void.
    if (inv.status === 'voided') {
      if (inv.has_pdf) {
        items.push(previewItem(inv));
        items.push({ label: 'Download PDF', onSelect: () => handleDownloadPdf(inv) });
      }
      return items;
    }
    if (inv.status === 'paid') {
      items.push({ label: 'Mark pending', onSelect: () => handleMarkPending(inv), disabled: busy });
    } else {
      items.push({ label: 'Mark paid', onSelect: () => handleMarkPaid(inv), disabled: busy });
    }
    items.push(previewItem(inv));
    items.push({
      label: 'Download PDF',
      onSelect: () => handleDownloadPdf(inv),
      disabled: !inv.has_pdf,
    });
    items.push({
      label: 'Void invoice…',
      onSelect: () => (voiding = inv),
      danger: true,
      disabled: busy,
    });
    return items;
  }

  function voidSummary(inv: InvoiceRow): string {
    const parts = [`Voided ${formatDate(inv.voided_at?.slice(0, 10))}`];
    if (inv.void_reason) parts.push(inv.void_reason);
    if (inv.replaced_by.length > 0) parts.push(`Replaced by ${inv.replaced_by.join(', ')}`);
    return parts.join(' — ');
  }

  let voiding: InvoiceRow | null = $state(null);
  let generateOpen = $state(false);
  let manualOpen = $state(false);

  let clients: ClientRow[] = $state([]);
  let entities: EntityRow[] = $state([]);
  let services: ServiceRow[] = $state([]);

  async function loadConfig() {
    // Best-effort: the list works without it, only the dialogs' pickers are empty.
    try {
      const [clientResp, settingsResp] = await Promise.all([getClients(), getBusinessSettings()]);
      clients = clientResp.clients;
      entities = settingsResp.entities;
      services = settingsResp.services;
    } catch {
      clients = [];
      entities = [];
      services = [];
    }
  }

  function afterVoid() {
    voiding = null;
    load();
  }

  function afterGenerate(_result: unknown, matchedPreview: boolean) {
    if (matchedPreview) generateOpen = false;
    load();
  }

  function afterCreate() {
    manualOpen = false;
    load();
  }

  let expandedKeys = $state(new Set<string>());
  let detailsCache = $state(new Map<string, InvoiceDetailItem[]>());
  let detailsLoading = $state(new Set<string>());

  async function toggleExpand(inv: InvoiceRow) {
    const key = inv.invoice_number;
    if (expandedKeys.has(key)) {
      const next = new Set(expandedKeys);
      next.delete(key);
      expandedKeys = next;
      return;
    }

    const nextExpanded = new Set(expandedKeys);
    nextExpanded.add(key);
    expandedKeys = nextExpanded;

    if (!detailsCache.has(key)) {
      const nextLoading = new Set(detailsLoading);
      nextLoading.add(key);
      detailsLoading = nextLoading;
      try {
        const resp = await getInvoiceDetails(key);
        const nextCache = new Map(detailsCache);
        nextCache.set(key, resp.items);
        detailsCache = nextCache;
      } catch {
        // stay expanded but empty
      } finally {
        const nl = new Set(detailsLoading);
        nl.delete(key);
        detailsLoading = nl;
      }
    }
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const resp = await getInvoices({ show_all: true });
      invoices = resp.invoices;
      reviews = resp.payment_reviews ?? [];
      invoiceCount = resp.invoice_count;
      outstandingCount = resp.outstanding_count;
      expandedKeys = new Set();
      detailsCache = new Map();
    } catch (e) {
      if (e instanceof Error) error = e.message;
      else error = 'Failed to load invoices';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    $selectedLedger;
    load();
  });

  $effect(() => {
    $selectedLedger;
    loadConfig();
  });

  // Invoices issued the same day fall back to number order.
  let sorted = $derived(
    sortRows(invoices, sort.descending, (inv) =>
      sort.key === 'number' ? [inv.invoice_number] : [inv.date, inv.invoice_number],
    ),
  );

  function displayStatus(status: string): string {
    if (status === 'outstanding') return 'posted';
    return status;
  }

  function formatQty(value: number): string {
    if (value === Math.floor(value)) return String(value);
    return value.toFixed(2);
  }
</script>

<div class="invoices-content">
  <LoadState {loading} {error} empty={invoices.length === 0}>
    {#snippet header()}
      <div class="money-toolbar toolbar toolbar--bar control-row">
        <span class="caption num">{invoiceCount} invoices ({outstandingCount} outstanding)</span>
        <div class="hstack">
          <Button variant="secondary" onclick={() => (manualOpen = true)}>New manual invoice</Button
          >
          <Button variant="primary" onclick={() => (generateOpen = true)}>Generate invoices</Button>
          <KebabMenu items={workExportItems()} icon={Download} ariaLabel="Download work log" />
        </div>
      </div>
      {#if reviews.length > 0}
        <div class="money-notice-bar">
          <Alert title={`Payments to review (${reviews.length})`} bind:collapsed={reviewsCollapsed}>
            {#each reviews as review (review.ledger_txn_id)}
              <div class="payment-review">
                <p>
                  ${formatAmount(review.amount)} on {formatDate(review.txn_date)} — {review.payee}
                </p>
                <p class="muted">{review.account}</p>
                <div class="review-actions">
                  {#each review.candidate_details as candidate (candidate.invoice_number)}
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={!!busyReview}
                      onclick={() => handleReview(review, candidate.invoice_number)}
                    >
                      Settle {candidate.invoice_number} ({candidate.client}, ${formatAmount(
                        candidate.total,
                      )})
                    </Button>
                  {/each}
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={!!busyReview}
                    onclick={() => handleReview(review)}
                  >
                    Not an invoice payment
                  </Button>
                </div>
                {#if busyReview === review.ledger_txn_id}<p role="status">Saving…</p>{/if}
              </div>
            {/each}
            {#if reviewError}<p class="form-error" role="alert">{reviewError}</p>{/if}
          </Alert>
        </div>
      {/if}
    {/snippet}
    {#snippet emptyState()}
      <EmptyState>No invoices found.</EmptyState>
    {/snippet}
    <div class="record-list">
      <div class="record-header micro-label">
        <SortHeader
          label="Invoice"
          name="invoice number"
          class="inv-number"
          direction={sort.directionOf('number')}
          onclick={() => sort.toggle('number')}
        />
        <span class="inv-client">Client</span>
        <SortHeader
          label="Date"
          class="inv-date"
          direction={sort.directionOf('date')}
          onclick={() => sort.toggle('date')}
        />
        <span class="inv-status">Status</span>
        <span class="inv-amount num">Amount</span>
        <span class="col-actions"></span>
      </div>
      {#each sorted as inv (inv.invoice_number)}
        {@const key = inv.invoice_number}
        {@const isExpanded = expandedKeys.has(key)}
        {@const voided = inv.status === 'voided'}
        <div
          class="record-row"
          class:is-expanded={isExpanded}
          class:voided
          role="button"
          tabindex="0"
          aria-expanded={isExpanded}
          onclick={() => toggleExpand(inv)}
          onkeydown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              toggleExpand(inv);
            }
          }}
        >
          <span
            class="inv-number"
            class:status-paid={inv.status === 'paid'}
            class:status-posted={inv.status === 'outstanding'}
            class:status-draft={inv.status === 'draft'}
            class:status-voided={voided}>{inv.invoice_number}</span
          >
          <span class="inv-client">
            {inv.client}
            {#if inv.paid_date}
              <span class="paid-note caption">
                Paid {formatDate(inv.paid_date)}
                {#if inv.paid_by_sync_date}
                  <span>paid by sync on {formatDate(inv.paid_by_sync_date)}</span>
                {/if}
              </span>
            {/if}
          </span>
          <span class="inv-date">{formatDate(inv.date)}</span>
          {#if voided}
            <span class="inv-status">
              <Badge size="sm" title={voidSummary(inv)}>Voided</Badge>
            </span>
          {:else}
            <span class="inv-status"
              ><Badge
                size="sm"
                variant={billingStatusVariant(
                  inv.status === 'paid'
                    ? 'paid'
                    : inv.status === 'outstanding'
                      ? 'posted'
                      : 'draft',
                )}>{displayStatus(inv.status)}</Badge
              ></span
            >
          {/if}
          <span class="inv-amount num">${formatAmount(inv.total)}</span>
          <KebabMenu items={menuItems(inv)} ariaLabel="Actions for {inv.invoice_number}" />
        </div>
        {#if isExpanded}
          <div class="record-detail">
            {#if detailsLoading.has(key)}
              <div class="record-detail-row"><span class="detail-desc">Loading...</span></div>
            {:else if detailsCache.has(key)}
              {#each detailsCache.get(key) ?? [] as item}
                <div class="record-detail-row">
                  <span class="detail-desc">
                    {item.description}
                    {#if item.detail}
                      <span class="detail-note">{item.detail}</span>
                    {/if}
                  </span>
                  <span class="detail-qty"
                    >{formatQty(item.quantity)} &times; ${formatAmount(item.rate)}</span
                  >
                  {#if item.discount > 0}
                    <span class="detail-discount">-${formatAmount(item.discount)}</span>
                  {/if}
                  <span class="detail-amount num">${formatAmount(item.amount)}</span>
                </div>
              {/each}
            {/if}
          </div>
        {/if}
      {/each}
    </div>
  </LoadState>
</div>

{#if voiding}
  <VoidInvoiceDialog
    invoiceNumber={voiding.invoice_number}
    paid={voiding.status === 'paid'}
    onVoided={afterVoid}
    onCancel={() => (voiding = null)}
  />
{/if}

{#if generateOpen}
  <GenerateInvoicesDialog
    {clients}
    {entities}
    onIssued={afterGenerate}
    onClose={() => (generateOpen = false)}
  />
{/if}

{#if manualOpen}
  <ManualInvoiceDialog
    {clients}
    {entities}
    {services}
    onCreated={afterCreate}
    onCancel={() => (manualOpen = false)}
  />
{/if}

<style>
  /* Muted rather than hidden: a voided number stays on record. */
  .voided .inv-client,
  .voided .inv-amount {
    color: var(--text-dim);
  }

  .voided .inv-amount {
    text-decoration: line-through;
  }

  .payment-review + .payment-review {
    margin-top: var(--space-4);
  }

  .review-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .paid-note {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-2);
    white-space: normal;
  }

  .invoices-content {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }

  /* Columns only — the record rows are table.css's, the toolbar is
     moneyTable.css's. */

  .invoices-content :global(.inv-number) {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-muted);
    flex-shrink: 0;
    min-width: 6.5rem;
    box-sizing: border-box;
  }

  .inv-client {
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--text-primary);
    font-weight: var(--weight-medium);
  }

  /* Fixed slot: the client column absorbs the slack, so a variable-width date
     (or status) would slide the whole right side of the row per row. */
  /* Global so it places the SortHeader too; the colour stays on the cell, or
     it would outrank the header's own sorted and hover colours. */
  .inv-date {
    color: var(--text-dim);
  }

  .invoices-content :global(.inv-date) {
    white-space: nowrap;
    flex-shrink: 0;
    font-size: var(--text-xs);
    min-width: 6.5rem;
  }

  /* Snug around the longest label ("posted") in caps: a slot much wider than
     the text turns the chip into a mostly-empty capsule. */
  .inv-status {
    display: inline-flex;
    min-width: 4rem;
    flex-shrink: 0;
  }

  .inv-amount {
    min-width: 5.5rem;
  }

  .detail-desc {
    flex: 1;
    min-width: 0;
    color: var(--text-secondary);
  }

  .detail-note {
    color: var(--text-dim);
    margin-left: var(--space-1);
  }

  .detail-qty {
    color: var(--text-dim);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .detail-discount {
    color: var(--money-expense);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .detail-amount {
    margin-left: auto;
    color: var(--text-secondary);
    min-width: 4rem;
  }

  @media (max-width: 640px) {
    .invoices-content :global(.inv-date) {
      display: none;
    }
    /* No status column at this width, so the invoice number carries the
       status colors instead — the chip's own tokens. */
    .invoices-content :global(.inv-number) {
      min-width: 5.25rem;
      padding: 0.1rem var(--space-2);
      border-radius: var(--radius-pill);
      text-align: center;
    }
    .inv-number.status-posted {
      color: var(--status-warn-fg);
      background: var(--status-warn-bg);
    }
    .inv-number.status-paid {
      color: var(--status-success-fg);
      background: var(--status-success-bg);
    }
    .inv-number.status-draft,
    .inv-number.status-voided {
      color: var(--text-muted);
      background: var(--surface-badge);
    }
    .inv-status {
      display: none;
    }
    .inv-amount {
      min-width: 4rem;
    }
    .record-detail {
      --record-detail-indent: var(--space-3);
    }
    .detail-qty {
      display: none;
    }
  }
</style>
