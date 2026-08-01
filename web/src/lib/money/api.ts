import { base } from '$app/paths';

class AuthError extends Error {
  constructor() {
    super('Not authenticated');
    this.name = 'AuthError';
  }
}

/**
 * A non-OK response, carrying the status and the parsed error envelope.
 *
 * Callers that only render `.message` are unaffected; the work page needs
 * `.status` to tell a 409 conflict from an ordinary failure, and `.payload`
 * to show the current server-side row.
 */
class ApiError extends Error {
  status: number;
  payload: any;

  constructor(status: number, payload: any) {
    super(payload?.error || `API error: ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // `base` is istota's URL prefix (e.g. /istota). Money's routes live under /api/money.
  const resp = await fetch(`${base}/api/money${path}`, {
    ...init,
    credentials: 'same-origin',
  });
  if (resp.status === 401) throw new AuthError();
  if (!resp.ok) {
    let payload: any = null;
    try {
      payload = await resp.json();
    } catch {
      // Non-JSON error body — the status alone has to carry the message.
    }
    throw new ApiError(resp.status, payload);
  }
  return resp.json();
}

export interface User {
  username: string;
  display_name: string;
}

export interface AccountRow {
  account: string;
  'sum(position)': string;
}

export interface AccountsResponse {
  status: string;
  accounts: AccountRow[];
}

export interface TransactionRow {
  date: string;
  flag: string;
  payee: string;
  narration: string;
  account: string;
  position: string;
  /** Stable transaction id (beancount `id:` metadata). Empty for un-backfilled legacy rows. */
  id?: string;
}

export interface TransactionsResponse {
  status: string;
  transactions: TransactionRow[];
  total: number;
  page: number;
  per_page: number;
}

export async function getMe(): Promise<User> {
  return apiFetch<User>('/me');
}

export async function getAccounts(opts?: {
  ledger?: string;
  year?: number;
}): Promise<AccountsResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.year) params.set('year', String(opts.year));
  const qs = params.toString();
  return apiFetch<AccountsResponse>(`/accounts${qs ? '?' + qs : ''}`);
}

export async function getTransactions(opts?: {
  ledger?: string;
  account?: string;
  year?: number;
  filter?: string;
  page?: number;
  per_page?: number;
}): Promise<TransactionsResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.account) params.set('account', opts.account);
  if (opts?.year) params.set('year', String(opts.year));
  if (opts?.filter) params.set('filter', opts.filter);
  if (opts?.page) params.set('page', String(opts.page));
  if (opts?.per_page) params.set('per_page', String(opts.per_page));
  const qs = params.toString();
  return apiFetch<TransactionsResponse>(`/transactions${qs ? '?' + qs : ''}`);
}

export interface ReportResponse {
  status: string;
  report_type: string;
  year: number;
  row_count: number;
  results: AccountRow[];
}

export interface CheckResponse {
  status: string;
  message: string;
  error_count: number;
  errors?: string[];
}

export async function getReport(
  type: string,
  opts?: { ledger?: string; year?: number },
): Promise<ReportResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.year) params.set('year', String(opts.year));
  const qs = params.toString();
  return apiFetch<ReportResponse>(`/report/${type}${qs ? '?' + qs : ''}`);
}

export interface CashFlowRow {
  year: string;
  month: string;
  account: string;
  'sum(position)': string;
}

export interface CashFlowResponse {
  status: string;
  report_type: string;
  year: number;
  row_count: number;
  results: CashFlowRow[];
}

export async function getCashFlow(opts?: {
  ledger?: string;
  year?: number;
}): Promise<CashFlowResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.year) params.set('year', String(opts.year));
  const qs = params.toString();
  return apiFetch<CashFlowResponse>(`/report/cash-flow${qs ? '?' + qs : ''}`);
}

export async function checkLedger(opts?: { ledger?: string }): Promise<CheckResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  const qs = params.toString();
  return apiFetch<CheckResponse>(`/check${qs ? '?' + qs : ''}`);
}

export interface PostingRow {
  account: string;
  position: string;
}

export interface PostingsResponse {
  status: string;
  postings: PostingRow[];
}

export async function getPostings(opts: {
  ledger?: string;
  date: string;
  payee: string;
  narration: string;
  account?: string;
  position?: string;
}): Promise<PostingsResponse> {
  const params = new URLSearchParams();
  if (opts.ledger) params.set('ledger', opts.ledger);
  params.set('date', opts.date);
  params.set('payee', opts.payee);
  params.set('narration', opts.narration);
  if (opts.account) params.set('account', opts.account);
  if (opts.position) params.set('position', opts.position);
  return apiFetch<PostingsResponse>(`/postings?${params.toString()}`);
}

export interface EntityRow {
  key: string;
  name: string;
  address: string;
  email: string;
  payment_instructions: string;
  logo: string;
  ar_account: string;
  bank_account: string;
  currency: string;
}

export interface ServiceRow {
  key: string;
  display_name: string;
  rate: number;
  type: string;
  income_account: string;
}

export interface BusinessDefaults {
  currency: string;
  default_entity: string;
  default_ar_account: string;
  default_bank_account: string;
  invoice_output: string;
  next_invoice_number: number;
  notifications: string;
  days_until_overdue: number;
}

export interface BusinessSettingsResponse {
  status: string;
  entities: EntityRow[];
  services: ServiceRow[];
  /** null when the user has no invoicing configuration yet. */
  defaults: BusinessDefaults | null;
}

export async function getBusinessSettings(): Promise<BusinessSettingsResponse> {
  return apiFetch<BusinessSettingsResponse>('/business-settings');
}

export interface ClientRow {
  key: string;
  name: string;
  email: string;
  address: string;
  terms: number | string;
  entity: string;
  entity_name: string;
  schedule: string;
  schedule_day: number;
  ar_account: string;
}

export interface ClientsResponse {
  status: string;
  clients: ClientRow[];
}

export async function getClients(): Promise<ClientsResponse> {
  return apiFetch<ClientsResponse>('/clients');
}

/**
 * Invoicing configuration — the editable side of clients, entities and
 * services.
 *
 * Two rules every caller here depends on:
 *
 * - **Send `""`, never `null`, to clear an optional field.** The store skips
 *   `null` values when merging, so a null silently preserves the old value
 *   while the form shows the field as cleared.
 * - **Omit `bundles` and `separate` entirely.** The merge preserves what's
 *   stored, which is why the client form can leave them out without shipping
 *   a nested-list editor.
 */
export interface ClientConfigRow {
  key: string;
  name: string;
  address: string;
  email: string;
  terms: number | string;
  ar_account: string;
  /** Raw — `''` means "fall back to default_entity", unlike ClientRow.entity. */
  entity: string;
  schedule: string;
  schedule_day: number;
  reminder_days: number;
  notifications: string;
  days_until_overdue: number;
  ledger_posting: boolean;
  bundles: Record<string, unknown>[];
  separate: string[];
}

export type ClientInput = Partial<Omit<ClientConfigRow, 'key' | 'bundles' | 'separate'>>;
export type EntityInput = Partial<Omit<EntityRow, 'key'>>;
export type ServiceInput = Partial<Omit<ServiceRow, 'key'>>;

/**
 * The record-key rule, mirroring `config_store._KEY_RE`.
 *
 * Defined once here and imported by every form: a change to the rule (the
 * lowercase client requirement below arrived that way) otherwise has to be
 * chased through each of them, and a form that drifts rejects a key the
 * server accepts or vice versa.
 */
export const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
export const KEY_HINT = 'Letters, digits, - and _ only';

/**
 * Client keys are lowercase-only, unlike entities and services.
 *
 * `add_work_entry` stores the client lowercased, so a mixed-case key matches
 * no work entry and every one of that client's rows is skipped at invoice
 * time — the work is silently never billed. The form lowercases as you type so
 * the key you see is the key you get; the server rejects the rest.
 */
export function normalizeClientKey(value: string): string {
  return value.toLowerCase();
}

/** Counts of what pointed at a record — carried on delete responses and 409s. */
export interface ConfigReferences {
  work_entries?: number;
  invoices?: number;
  clients?: string[];
  default_entity?: boolean;
  /** Clients with a blank entity — they bill under whichever one is default. */
  default_for_clients?: number;
  /**
   * Year files holding a row this version can't read. Non-empty means the
   * counts above are lower bounds, so the two strict deletes refuse.
   */
  quarantined?: string[];
}

export interface ConfigDeleteResponse {
  status: string;
  removed: boolean;
  references: ConfigReferences;
}

/**
 * Clients as stored, with no defaults resolved into them.
 *
 * Distinct from `getClients()`, which resolves `entity` and `ar_account`
 * through the business defaults for display. Binding an edit form to the
 * resolved shape would *materialise* the default onto the record on save, so
 * a later change to `default_entity` would stop propagating to a client that
 * never had an explicit one.
 */
export async function getClientConfigs(): Promise<{
  status: string;
  clients: ClientConfigRow[];
}> {
  return apiFetch('/config/clients');
}

function writeJson<T>(path: string, method: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/**
 * PUT a record that must already exist.
 *
 * The routes upsert by default, for `ensure`-style CLI callers. The forms only
 * ever edit a record they just loaded, so `?create=false` makes a key another
 * tab deleted meanwhile a 404 instead of resurrecting a partial record built
 * from this form's fields plus defaults for everything the form doesn't show.
 */
function updateExisting<T>(path: string, body: unknown): Promise<T> {
  return writeJson<T>(`${path}?create=false`, 'PUT', body);
}

export async function createClient(
  key: string,
  input: ClientInput,
): Promise<{ status: string; client: ClientConfigRow }> {
  return writeJson('/config/clients', 'POST', { key, ...input });
}

export async function updateClient(
  key: string,
  input: ClientInput,
): Promise<{ status: string; client: ClientConfigRow }> {
  return updateExisting(`/config/clients/${encodeURIComponent(key)}`, input);
}

export async function deleteClient(key: string): Promise<ConfigDeleteResponse> {
  return apiFetch(`/config/clients/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

export async function createEntity(
  key: string,
  input: EntityInput,
): Promise<{ status: string; company: EntityRow }> {
  return writeJson('/config/companies', 'POST', { key, ...input });
}

export async function updateEntity(
  key: string,
  input: EntityInput,
): Promise<{ status: string; company: EntityRow }> {
  return updateExisting(`/config/companies/${encodeURIComponent(key)}`, input);
}

export async function deleteEntity(key: string): Promise<ConfigDeleteResponse> {
  return apiFetch(`/config/companies/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

export async function createService(
  key: string,
  input: ServiceInput,
): Promise<{ status: string; service: ServiceRow }> {
  return writeJson('/config/services', 'POST', { key, ...input });
}

export async function updateService(
  key: string,
  input: ServiceInput,
): Promise<{ status: string; service: ServiceRow }> {
  return updateExisting(`/config/services/${encodeURIComponent(key)}`, input);
}

export async function deleteService(key: string): Promise<ConfigDeleteResponse> {
  return apiFetch(`/config/services/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

export interface InvoiceRow {
  invoice_number: string;
  client: string;
  client_key: string;
  date: string;
  total: number;
  status: string;
  paid_date?: string;
}

export interface InvoicesResponse {
  status: string;
  invoice_count: number;
  outstanding_count: number;
  invoices: InvoiceRow[];
}

export async function getInvoices(opts?: {
  client?: string;
  show_all?: boolean;
}): Promise<InvoicesResponse> {
  const params = new URLSearchParams();
  if (opts?.client) params.set('client', opts.client);
  if (opts?.show_all) params.set('show_all', 'true');
  const qs = params.toString();
  return apiFetch<InvoicesResponse>(`/invoices${qs ? '?' + qs : ''}`);
}

export interface InvoiceDetailItem {
  description: string;
  detail: string;
  quantity: number;
  rate: number;
  discount: number;
  amount: number;
}

export interface InvoiceDetailsResponse {
  status: string;
  invoice_number: string;
  items: InvoiceDetailItem[];
}

export async function getInvoiceDetails(invoice_number: string): Promise<InvoiceDetailsResponse> {
  const params = new URLSearchParams({ invoice_number });
  return apiFetch<InvoiceDetailsResponse>(`/invoice-details?${params.toString()}`);
}

export interface InvoiceActionResponse {
  status: string;
  invoice_number: string;
  count: number;
  paid_date?: string;
}

/** Mark an invoice paid (sets paid_date; does not post a ledger payment). */
export async function markInvoicePaid(
  invoice_number: string,
  opts?: { paid_date?: string },
): Promise<InvoiceActionResponse> {
  return apiFetch<InvoiceActionResponse>(
    `/invoices/${encodeURIComponent(invoice_number)}/mark-paid`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paid_date: opts?.paid_date }),
    },
  );
}

/** Un-pay an invoice (clears paid_date, keeps the invoice number). */
export async function markInvoicePending(invoice_number: string): Promise<InvoiceActionResponse> {
  return apiFetch<InvoiceActionResponse>(
    `/invoices/${encodeURIComponent(invoice_number)}/mark-pending`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
  );
}

/** URL for the generated invoice PDF — open in a new tab / download. */
export function invoicePdfUrl(invoice_number: string): string {
  return `${base}/api/money/invoices/${encodeURIComponent(invoice_number)}/pdf`;
}

/** Work entries — the input side of invoicing. */
export interface WorkEntryRow {
  /** Stable id. Empty until the backfill runs; such a row isn't editable. */
  uid: string;
  /** 1-based display index. Presentation only — it shifts under concurrent writes. */
  index: number | null;
  /** Content hash, echoed back on write so a stale edit 409s instead of silently reverting. */
  etag: string;
  date: string;
  client: string;
  client_name: string;
  service: string;
  service_name: string;
  service_type: string;
  qty: number | null;
  amount: number | null;
  discount: number;
  description: string;
  entity: string;
  invoice: string;
  paid_date: string | null;
  /** What this entry will bill for, using the same rate rules the invoice uses. */
  computed_amount: number | null;
  editable: boolean;
  /** 'unknown_service' | 'unknown_client' | 'no_uid' */
  warnings: string[];
}

export interface WorkTotals {
  uninvoiced_count: number;
  uninvoiced_amount: number;
  invoiced_count: number;
  paid_count: number;
}

export interface WorkEntriesResponse {
  status: string;
  entries: WorkEntryRow[];
  totals: WorkTotals;
}

export type WorkStatusFilter = 'uninvoiced' | 'invoiced' | 'paid' | 'all';

export interface WorkEntryInput {
  date: string;
  client: string;
  service: string;
  qty?: number | null;
  amount?: number | null;
  discount?: number;
  description?: string;
  entity?: string;
}

export async function getWorkEntries(opts?: {
  client?: string;
  period?: string;
  status?: WorkStatusFilter;
}): Promise<WorkEntriesResponse> {
  const params = new URLSearchParams();
  if (opts?.client) params.set('client', opts.client);
  if (opts?.period) params.set('period', opts.period);
  if (opts?.status) params.set('status', opts.status);
  const qs = params.toString();
  return apiFetch<WorkEntriesResponse>(`/work${qs ? '?' + qs : ''}`);
}

export async function createWorkEntry(
  input: WorkEntryInput,
): Promise<{ status: string; entry: WorkEntryRow }> {
  return apiFetch<{ status: string; entry: WorkEntryRow }>('/work', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
}

/** Update an entry by uid. Pass the row's `etag` so a concurrent edit conflicts. */
export async function updateWorkEntry(
  uid: string,
  patch: Partial<WorkEntryInput> & { etag?: string },
): Promise<{ status: string; entry: WorkEntryRow }> {
  return apiFetch<{ status: string; entry: WorkEntryRow }>(`/work/${encodeURIComponent(uid)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

export async function deleteWorkEntry(
  uid: string,
  opts?: { etag?: string },
): Promise<{ status: string; uid: string }> {
  const params = new URLSearchParams();
  if (opts?.etag) params.set('etag', opts.etag);
  const qs = params.toString();
  return apiFetch<{ status: string; uid: string }>(
    `/work/${encodeURIComponent(uid)}${qs ? '?' + qs : ''}`,
    { method: 'DELETE' },
  );
}

export interface TransactionUpdate {
  // Stable id of the transaction to edit.
  id: string;
  // Identifies which posting (leg) to edit when an account repeats.
  old_account?: string;
  old_position?: string;
  // New values.
  new_payee?: string;
  new_narration?: string;
  new_account?: string;
  new_position?: string;
  new_date?: string;
  ledger?: string;
}

/**
 * Edit a transaction, located by its stable `id:` metadata. The backend
 * rewrites the directive in place and re-validates with `bean-check`; an edit
 * that unbalances the entry is rolled back and surfaced as a 422 error.
 */
export async function updateTransaction(payload: TransactionUpdate): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/transactions/update`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function getLedgers(): Promise<string[]> {
  const resp = await apiFetch<{ ledgers: string[] }>('/ledgers');
  return resp.ledgers;
}

export interface TaxEstimateResponse {
  status: string;
  tax_year: number;
  quarter: number;
  method: string;
  filing_status: string;
  w2_months: number;
  annualization_months: number;
  se_income_ytd: number;
  se_income_annualized: number;
  w2_income: number;
  w2_income_annualized: number;
  se_tax: number;
  half_se_deduction: number;
  additional_medicare_tax: number;
  federal_agi: number;
  federal_standard_deduction: number;
  federal_taxable_income: number;
  federal_tax: number;
  qbi_deduction: number;
  ca_agi: number;
  ca_standard_deduction: number;
  ca_taxable_income: number;
  ca_tax: number;
  federal_withholding: number;
  state_withholding: number;
  federal_estimated_paid: number;
  state_estimated_paid: number;
  federal_total_liability: number;
  state_total_liability: number;
  federal_net_due: number;
  state_net_due: number;
  federal_quarterly_amount: number;
  state_quarterly_amount: number;
  quarters_remaining: number;
}

export interface TaxEstimateInputs {
  method?: string;
  w2_income?: number;
  w2_federal_withholding?: number;
  w2_state_withholding?: number;
  federal_estimated_paid?: number;
  state_estimated_paid?: number;
  w2_months?: number;
}

export async function getTaxEstimate(opts?: {
  ledger?: string;
  method?: string;
}): Promise<TaxEstimateResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.method) params.set('method', opts.method);
  const qs = params.toString();
  return apiFetch<TaxEstimateResponse>(`/tax/estimate${qs ? '?' + qs : ''}`);
}

export async function recalculateTaxEstimate(
  inputs: TaxEstimateInputs,
  opts?: { ledger?: string },
): Promise<TaxEstimateResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  const qs = params.toString();
  return apiFetch<TaxEstimateResponse>(`/tax/estimate${qs ? '?' + qs : ''}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(inputs),
  });
}

// --- Portfolio (positions snapshots) ---

export interface PortfolioSnapshotRow {
  id: number;
  exported_at: string;
  exported_at_estimated: boolean;
  imported_at: string;
  source: string;
  source_file: string | null;
  position_count: number;
  /** Read-time total over non-excluded accounts. */
  total_value: number;
}

export interface PortfolioGroupSlice {
  key: string;
  value: number;
  pct: number;
}

export interface PortfolioAccountSlice extends PortfolioGroupSlice {
  account_id: number;
  group: string;
  account_type: string;
}

export interface PortfolioHolding {
  symbol: string;
  description: string;
  quantity: number | null;
  value: number;
  cost_basis: number | null;
  gain: number | null;
  gain_pct: number | null;
  asset_class: string;
  sub_class: string;
  geography: string;
  accounts: number;
}

export interface PortfolioSummary {
  snapshot_id: number;
  exported_at: string;
  exported_at_estimated: boolean;
  total_value: number;
  position_count: number;
  by_asset_class: PortfolioGroupSlice[];
  by_account: PortfolioAccountSlice[];
  by_account_type: PortfolioGroupSlice[];
  by_group: PortfolioGroupSlice[];
  by_geography: PortfolioGroupSlice[];
  holdings: PortfolioHolding[];
}

export interface PortfolioHistoryPoint {
  snapshot_id: number;
  exported_at: string;
  exported_at_estimated: boolean;
  total: number;
  groups?: Record<string, number>;
}

export interface PortfolioAccount {
  id: number;
  account_name: string;
  account_number: string;
  group: string;
  account_type: string;
  excluded: boolean;
  first_seen_at: string;
  last_seen_at: string;
}

export interface PortfolioClassification {
  symbol: string;
  asset_class: string;
  sub_class: string;
  geography: string;
  updated_at: string;
}

export interface PortfolioImportResult {
  status: string;
  snapshot_id?: number;
  exported_at?: string;
  exported_at_estimated?: boolean;
  position_count?: number;
  total_value?: number;
  new_accounts?: string[];
  unclassified_symbols?: string[];
  warnings?: string[];
  source_file?: string;
  dry_run?: boolean;
  snapshots?: {
    exported_at: string;
    exported_at_estimated: boolean;
    source: string;
    position_count: number;
    total_value: number;
    warnings: string[];
  }[];
  /** Multi-snapshot (fina history) import. */
  imported?: number;
  duplicates?: number;
  results?: PortfolioImportResult[];
  /** Same-day collision. */
  existing?: { id: number; exported_at: string; position_count: number };
}

export interface PortfolioDiffEntry {
  symbol: string;
  account_name: string;
  quantity: number;
  value: number;
}

export interface PortfolioDiffChange {
  symbol: string;
  account_name: string;
  quantity_from: number;
  quantity_to: number;
  value_from: number;
  value_to: number;
}

export interface PortfolioDiff {
  older_id: number;
  newer_id: number;
  opened: PortfolioDiffEntry[];
  closed: PortfolioDiffEntry[];
  changed: PortfolioDiffChange[];
}

export interface PortfolioSymbolHistory {
  symbol: string;
  points: {
    snapshot_id: number;
    exported_at: string;
    quantity: number | null;
    price: number | null;
    value: number | null;
  }[];
}

export async function importPortfolioFile(
  file: File,
  opts?: { dryRun?: boolean; replace?: number; force?: boolean; source?: string },
): Promise<PortfolioImportResult> {
  const params = new URLSearchParams();
  if (opts?.dryRun) params.set('dry_run', '1');
  if (opts?.replace != null) params.set('replace', String(opts.replace));
  if (opts?.force) params.set('force', '1');
  if (opts?.source) params.set('source', opts.source);
  const qs = params.toString();
  const form = new FormData();
  form.append('file', file);
  return apiFetch<PortfolioImportResult>(`/portfolio/import${qs ? '?' + qs : ''}`, {
    method: 'POST',
    body: form,
  });
}

export async function getPortfolioSnapshots(): Promise<{
  status: string;
  snapshots: PortfolioSnapshotRow[];
}> {
  return apiFetch('/portfolio/snapshots');
}

export async function getPortfolioSnapshotSummary(
  id: number,
  opts?: { group?: string },
): Promise<{ status: string; summary: PortfolioSummary }> {
  const params = new URLSearchParams();
  if (opts?.group) params.set('group', opts.group);
  const qs = params.toString();
  return apiFetch(`/portfolio/snapshots/${id}${qs ? '?' + qs : ''}`);
}

export async function deletePortfolioSnapshot(
  id: number,
): Promise<{ status: string; deleted: number }> {
  return apiFetch(`/portfolio/snapshots/${id}`, { method: 'DELETE' });
}

export async function getPortfolioSummary(opts?: {
  group?: string;
}): Promise<{ status: string; summary: PortfolioSummary | null }> {
  const params = new URLSearchParams();
  if (opts?.group) params.set('group', opts.group);
  const qs = params.toString();
  return apiFetch(`/portfolio/summary${qs ? '?' + qs : ''}`);
}

export async function getPortfolioHistory(opts?: {
  groupBy?: 'total' | 'group' | 'account_type' | 'asset_class';
  group?: string;
}): Promise<{ status: string; group_by: string; series: PortfolioHistoryPoint[] }> {
  const params = new URLSearchParams();
  if (opts?.groupBy) params.set('group_by', opts.groupBy);
  if (opts?.group) params.set('group', opts.group);
  const qs = params.toString();
  return apiFetch(`/portfolio/history${qs ? '?' + qs : ''}`);
}

export async function getPortfolioDiff(
  older: number,
  newer: number,
): Promise<{ status: string; diff: PortfolioDiff }> {
  return apiFetch(`/portfolio/diff?older=${older}&newer=${newer}`);
}

export async function getPortfolioSymbolHistory(
  symbol: string,
): Promise<{ status: string; history: PortfolioSymbolHistory }> {
  return apiFetch(`/portfolio/symbols/${encodeURIComponent(symbol)}/history`);
}

export async function getPortfolioAccounts(): Promise<{
  status: string;
  accounts: PortfolioAccount[];
}> {
  return apiFetch('/portfolio/accounts');
}

export async function patchPortfolioAccount(
  id: number,
  fields: { group?: string; account_type?: string; excluded?: boolean },
): Promise<{ status: string; account: PortfolioAccount }> {
  return apiFetch(`/portfolio/accounts/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  });
}

export async function getPortfolioClassifications(): Promise<{
  status: string;
  classifications: PortfolioClassification[];
}> {
  return apiFetch('/portfolio/classifications');
}

export async function putPortfolioClassification(
  symbol: string,
  fields: { asset_class: string; sub_class?: string; geography?: string },
): Promise<{ status: string; classification: PortfolioClassification }> {
  return apiFetch(`/portfolio/classifications/${encodeURIComponent(symbol)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  });
}

export async function deletePortfolioClassification(symbol: string): Promise<{ status: string }> {
  return apiFetch(`/portfolio/classifications/${encodeURIComponent(symbol)}`, {
    method: 'DELETE',
  });
}

export { AuthError, ApiError };
