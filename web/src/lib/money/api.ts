import { base } from '$app/paths';

class AuthError extends Error {
	constructor() {
		super('Not authenticated');
		this.name = 'AuthError';
	}
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
	// `base` is istota's URL prefix (e.g. /istota). Money's routes live under /money/api.
	const resp = await fetch(`${base}/money/api${path}`, {
		...init,
		credentials: 'same-origin',
	});
	if (resp.status === 401) throw new AuthError();
	if (!resp.ok) throw new Error(`API error: ${resp.status}`);
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

export async function getAccounts(opts?: { ledger?: string; year?: number }): Promise<AccountsResponse> {
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

export async function getReport(type: string, opts?: { ledger?: string; year?: number }): Promise<ReportResponse> {
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

export async function getCashFlow(opts?: { ledger?: string; year?: number }): Promise<CashFlowResponse> {
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
	defaults: BusinessDefaults;
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

export { AuthError };
