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

/** Counts of what pointed at a record — carried on delete responses and 409s. */
export interface ConfigReferences {
  work_entries?: number;
  invoices?: number;
  clients?: string[];
  default_entity?: boolean;
  /** Clients with a blank entity — they bill under whichever one is default. */
  default_for_clients?: number;
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
  return writeJson(`/config/clients/${encodeURIComponent(key)}`, 'PUT', input);
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
  return writeJson(`/config/companies/${encodeURIComponent(key)}`, 'PUT', input);
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
  return writeJson(`/config/services/${encodeURIComponent(key)}`, 'PUT', input);
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

export { AuthError, ApiError };
