<script lang="ts">
	import {
		getTaxEstimate,
		recalculateTaxEstimate,
		type TaxEstimateResponse,
	} from '$lib/money/api';
	import { selectedLedger } from '$lib/money/stores/ledger';

	let data: TaxEstimateResponse | null = $state(null);
	let loading = $state(true);
	let error = $state('');

	// Editable inputs
	let method = $state('annualized');
	let w2Income = $state(0);
	let w2FedWithholding = $state(0);
	let w2StateWithholding = $state(0);
	let fedEstimatedPaid = $state(0);
	let stateEstimatedPaid = $state(0);
	let w2Months = $state(12);

	let debounceTimer: ReturnType<typeof setTimeout> | undefined;

	async function loadInitial() {
		loading = true;
		error = '';
		try {
			const resp = await getTaxEstimate({ ledger: $selectedLedger || undefined });
			data = resp;
			// Populate editable fields from response
			method = resp.method;
			w2Income = resp.w2_income;
			const annualizeFactor = resp.w2_months / (resp.quarter * 3); // same as backend
			w2FedWithholding = resp.federal_withholding / annualizeFactor;
			w2StateWithholding = resp.state_withholding / annualizeFactor;
			fedEstimatedPaid = resp.federal_estimated_paid;
			stateEstimatedPaid = resp.state_estimated_paid;
			w2Months = resp.w2_months;
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Failed to load tax estimate';
		} finally {
			loading = false;
		}
	}

	function scheduleRecalc() {
		clearTimeout(debounceTimer);
		debounceTimer = setTimeout(recalc, 400);
	}

	async function recalc() {
		if (!data) return;
		error = '';
		try {
			const resp = await recalculateTaxEstimate(
				{
					method,
					w2_income: w2Income,
					w2_federal_withholding: w2FedWithholding,
					w2_state_withholding: w2StateWithholding,
					federal_estimated_paid: fedEstimatedPaid,
					state_estimated_paid: stateEstimatedPaid,
					w2_months: w2Months,
				},
				{ ledger: $selectedLedger || undefined },
			);
			data = resp;
		} catch (e) {
			if (e instanceof Error) error = e.message;
			else error = 'Recalculation failed';
		}
	}

	$effect(() => {
		$selectedLedger;
		loadInitial();
	});

	function fmt(n: number): string {
		return n.toLocaleString(undefined, {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		});
	}

	function fmtDollar(n: number): string {
		return '$' + fmt(n);
	}

	function fmtPct(n: number): string {
		return (n * 100).toFixed(1) + '%';
	}

	let totalQuarterly = $derived(
		data ? data.federal_quarterly_amount + data.state_quarterly_amount : 0,
	);

	let seTaxableBase = $derived(data ? data.se_income_annualized * 0.9235 : 0);
	let seEffectiveRate = $derived(
		data && data.se_income_annualized > 0
			? (data.federal_total_liability + data.state_total_liability - data.federal_withholding - data.state_withholding) / (data.se_income_annualized)
			: 0,
	);

	let totalGrossIncome = $derived(
		data ? data.se_income_annualized + data.w2_income_annualized : 0,
	);
	let effectiveTaxRate = $derived(
		data && totalGrossIncome > 0
			? (data.federal_total_liability + data.state_total_liability) / totalGrossIncome
			: 0,
	);
</script>

<div class="tax-content">
	{#if loading}
		<div class="loading">Loading...</div>
	{:else if error && !data}
		<div class="error-msg">{error}</div>
	{:else if !data}
		<div class="empty">No tax configuration found. Add a <code>TAX.md</code> (or legacy <code>tax.toml</code>) to your money workspace config.</div>
	{:else}
		{#if error}
			<div class="error-msg">{error}</div>
		{/if}

		<div class="tax-layout">
			<!-- Inputs -->
			<section class="input-section">
				<h2>Inputs</h2>
				<div class="input-card">
					<div class="input-group">
						<label class="input-label" for="w2-income">W-2 income YTD</label>
						<div class="input-field">
							<span class="input-prefix">$</span>
							<input
								id="w2-income"
								type="number"
								bind:value={w2Income}
								oninput={scheduleRecalc}
							/>
						</div>
					</div>

					<div class="input-group">
						<label class="input-label" for="w2-fed">Federal withholding YTD</label>
						<div class="input-field">
							<span class="input-prefix">$</span>
							<input
								id="w2-fed"
								type="number"
								bind:value={w2FedWithholding}
								oninput={scheduleRecalc}
							/>
						</div>
					</div>

					<div class="input-group">
						<label class="input-label" for="w2-state">State withholding YTD</label>
						<div class="input-field">
							<span class="input-prefix">$</span>
							<input
								id="w2-state"
								type="number"
								bind:value={w2StateWithholding}
								oninput={scheduleRecalc}
							/>
						</div>
					</div>

					<div class="input-group">
						<label class="input-label" for="w2-months">W-2 employment months</label>
						<div class="input-field">
							<input
								id="w2-months"
								type="number"
								min="1"
								max="12"
								bind:value={w2Months}
								oninput={scheduleRecalc}
							/>
							<span class="input-suffix">of 12</span>
						</div>
					</div>

					<div class="input-group">
						<label class="input-label" for="fed-est">Federal estimated paid</label>
						<div class="input-field">
							<span class="input-prefix">$</span>
							<input
								id="fed-est"
								type="number"
								bind:value={fedEstimatedPaid}
								oninput={scheduleRecalc}
							/>
						</div>
					</div>

					<div class="input-group">
						<label class="input-label" for="state-est">State estimated paid</label>
						<div class="input-field">
							<span class="input-prefix">$</span>
							<input
								id="state-est"
								type="number"
								bind:value={stateEstimatedPaid}
								oninput={scheduleRecalc}
							/>
						</div>
					</div>
				</div>

				<div class="input-meta">
					<span>Q{data.quarter} {data.tax_year}</span>
					<span>{data.filing_status.toUpperCase()}</span>
					<span>{data.quarters_remaining} quarter{data.quarters_remaining !== 1 ? 's' : ''} remaining</span>
				</div>
			</section>

			<!-- Results -->
			<section class="results-section">
				<h2>Estimate</h2>

				<div class="summary-cards">
					<div class="summary-card">
						<span class="card-label">Federal due</span>
						<span class="card-amount">{fmtDollar(data.federal_quarterly_amount)}</span>
					</div>
					<div class="summary-card">
						<span class="card-label">State due</span>
						<span class="card-amount">{fmtDollar(data.state_quarterly_amount)}</span>
					</div>
					<div class="summary-card total">
						<span class="card-label">Total due this quarter</span>
						<span class="card-amount">{fmtDollar(totalQuarterly)}</span>
					</div>
				</div>

				<div class="breakdown-table">
					<div class="breakdown-header">
						<span class="breakdown-label"></span>
						<span class="breakdown-val">Federal</span>
						<span class="breakdown-val">State (CA)</span>
					</div>

					<div class="breakdown-group-label">Income</div>
					<div class="breakdown-row">
						<span class="breakdown-label">SE income (YTD)</span>
						<span class="breakdown-val">{fmtDollar(data.se_income_ytd)}</span>
						<span class="breakdown-val"></span>
					</div>
					<div class="breakdown-row">
						<span class="breakdown-label">SE income (annualized)</span>
						<span class="breakdown-val">{fmtDollar(data.se_income_annualized)}</span>
						<span class="breakdown-val">{fmtDollar(data.se_income_annualized)}</span>
					</div>
					<div class="breakdown-row">
						<span class="breakdown-label">W-2 income (annualized)</span>
						<span class="breakdown-val">{fmtDollar(data.w2_income_annualized)}</span>
						<span class="breakdown-val">{fmtDollar(data.w2_income_annualized)}</span>
					</div>
					<details class="info-panel">
						<summary>How income is annualized</summary>
						<p>
							SE income is pulled from the ledger through the end of Q{data.quarter}
							and multiplied by {4 / data.quarter} to project a full-year figure.
							{#if data.w2_months < 12}
								W-2 income is projected to {data.w2_months} months of employment
								(YTD through {data.quarter * 3} months, scaled by {data.w2_months}/{data.quarter * 3}).
								This models the W-2 job ending after {data.w2_months} months instead of running all year.
							{:else}
								W-2 income is the YTD amount you entered, annualized the same way as SE income.
							{/if}
							{#if method === 'annualized'}
								This self-corrects each quarter as actual income data replaces projections.
							{/if}
						</p>
					</details>

					<div class="breakdown-group-label">Self-employment tax</div>
					<div class="breakdown-row">
						<span class="breakdown-label">SE tax</span>
						<span class="breakdown-val">{fmtDollar(data.se_tax)}</span>
						<span class="breakdown-val dim">n/a</span>
					</div>
					<div class="breakdown-row">
						<span class="breakdown-label">Half SE deduction</span>
						<span class="breakdown-val">{fmtDollar(data.half_se_deduction)}</span>
						<span class="breakdown-val dim">n/a</span>
					</div>
					{#if data.additional_medicare_tax > 0}
						<div class="breakdown-row">
							<span class="breakdown-label">Additional Medicare tax (0.9%)</span>
							<span class="breakdown-val">{fmtDollar(data.additional_medicare_tax)}</span>
							<span class="breakdown-val dim">n/a</span>
						</div>
					{/if}
					<details class="info-panel">
						<summary>How SE tax works</summary>
						<p>
							SE tax is 15.3% (12.4% Social Security + 2.9% Medicare) on 92.35% of net SE income.
							The taxable base is {fmtDollar(seTaxableBase)}.
							{#if seTaxableBase > 176100}
								Social Security applies only up to the wage base ($176,100); income above that pays only the 2.9% Medicare rate.
							{/if}
							SE tax is computed on the SE person's income alone; the spouse's W-2 wages do not affect the SS cap.
							Half of SE tax ({fmtDollar(data.half_se_deduction)}) is an above-the-line deduction that reduces AGI.
							{#if data.additional_medicare_tax > 0}
								An additional 0.9% Medicare tax applies to combined earned income (W-2 + SE) above the filing-status threshold.
							{/if}
						</p>
					</details>

					<div class="breakdown-group-label">Tax calculation</div>
					<div class="breakdown-row">
						<span class="breakdown-label">AGI</span>
						<span class="breakdown-val">{fmtDollar(data.federal_agi)}</span>
						<span class="breakdown-val">{fmtDollar(data.ca_agi)}</span>
					</div>
					<div class="breakdown-row">
						<span class="breakdown-label">Standard deduction</span>
						<span class="breakdown-val">{fmtDollar(data.federal_standard_deduction)}</span>
						<span class="breakdown-val">{fmtDollar(data.ca_standard_deduction)}</span>
					</div>
					{#if data.qbi_deduction > 0}
						<div class="breakdown-row">
							<span class="breakdown-label">QBI deduction</span>
							<span class="breakdown-val">{fmtDollar(data.qbi_deduction)}</span>
							<span class="breakdown-val dim">n/a</span>
						</div>
					{/if}
					<div class="breakdown-row">
						<span class="breakdown-label">Taxable income</span>
						<span class="breakdown-val">{fmtDollar(data.federal_taxable_income)}</span>
						<span class="breakdown-val">{fmtDollar(data.ca_taxable_income)}</span>
					</div>
					<div class="breakdown-row">
						<span class="breakdown-label">Income tax</span>
						<span class="breakdown-val">{fmtDollar(data.federal_tax)}</span>
						<span class="breakdown-val">{fmtDollar(data.ca_tax)}</span>
					</div>
					<div class="breakdown-row highlight">
						<span class="breakdown-label">Total liability</span>
						<span class="breakdown-val">{fmtDollar(data.federal_total_liability)}</span>
						<span class="breakdown-val">{fmtDollar(data.state_total_liability)}</span>
					</div>
					<div class="breakdown-row">
						<span class="breakdown-label">Effective tax rate</span>
						<span class="breakdown-val combined">{fmtPct(effectiveTaxRate)}</span>
					</div>
					<details class="info-panel">
						<summary>How tax liability is computed</summary>
						<p>
							AGI = annualized SE income + annualized W-2 income - half SE deduction.
							Federal taxable income = AGI - standard deduction ({fmtDollar(data.federal_standard_deduction)})
							{#if data.qbi_deduction > 0}
								- QBI deduction ({fmtDollar(data.qbi_deduction)}, which is 20% of qualified business income under Section 199A)
							{/if}.
							Federal income tax is computed using progressive {data.tax_year} MFJ brackets (10% through 37%).
							Federal total liability includes both income tax and SE tax.
							CA uses its own brackets and a lower standard deduction ({fmtDollar(data.ca_standard_deduction)}); QBI and half-SE do not apply to CA taxable income calculation beyond AGI.
						</p>
					</details>

					<div class="breakdown-group-label">Credits and payments</div>
					<div class="breakdown-row">
						<span class="breakdown-label">Withholding (annualized)</span>
						<span class="breakdown-val">{fmtDollar(data.federal_withholding)}</span>
						<span class="breakdown-val">{fmtDollar(data.state_withholding)}</span>
					</div>
					<div class="breakdown-row">
						<span class="breakdown-label">Estimated payments made</span>
						<span class="breakdown-val">{fmtDollar(data.federal_estimated_paid)}</span>
						<span class="breakdown-val">{fmtDollar(data.state_estimated_paid)}</span>
					</div>
					<div class="breakdown-row highlight">
						<span class="breakdown-label">Net due</span>
						<span class="breakdown-val">{fmtDollar(data.federal_net_due)}</span>
						<span class="breakdown-val">{fmtDollar(data.state_net_due)}</span>
					</div>
					<div class="breakdown-row result">
						<span class="breakdown-label">Due this quarter (Q{data.quarter})</span>
						<span class="breakdown-val">{fmtDollar(data.federal_quarterly_amount)}</span>
						<span class="breakdown-val">{fmtDollar(data.state_quarterly_amount)}</span>
					</div>
					<details class="info-panel">
						<summary>How the quarterly amount is determined</summary>
						<p>
							{#if method === 'annualized'}
								Net due = total annual liability - annualized withholding - estimated payments already made.
								Federal installments are 25% each quarter. CA uses a 30/40/0/30 schedule (Apr/Jun/Sep/Jan).
								As W-2 withholding and income data are updated each quarter, the per-quarter amount self-corrects.
							{:else}
								Safe harbor uses last year's total tax divided by 4 as each quarterly payment.
								Withholding is subtracted first. This avoids underpayment penalties regardless of current-year income changes.
							{/if}
						</p>
						{#if data.se_income_annualized > 0}
							<p>
								The SE income bears an effective marginal rate of ~{fmtPct(seEffectiveRate)} when stacked on top of W-2 income,
								because it's taxed at the household's marginal bracket (not starting from zero).
							</p>
						{/if}
					</details>
				</div>
			</section>
		</div>
	{/if}
</div>

<style>
	.tax-content {
		padding: 0.5rem;
	}

	.tax-layout {
		display: grid;
		grid-template-columns: 280px 1fr;
		gap: 1.5rem;
		align-items: start;
	}

	section h2 {
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
		margin: 0 0 0.5rem 0.25rem;
	}

	/* Inputs */
	.input-card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}

	.input-group {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}

	.input-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

.input-field {
		display: flex;
		align-items: center;
		background: var(--surface-base);
		border: 1px solid var(--border-default);
		border-radius: 0.25rem;
		overflow: hidden;
	}

	.input-prefix {
		padding: 0.3rem 0 0.3rem 0.5rem;
		font-size: var(--text-sm);
		color: var(--text-dim);
	}

	.input-field input {
		flex: 1;
		padding: 0.3rem 0.5rem 0.3rem 0.25rem;
		background: transparent;
		border: none;
		color: var(--text-primary);
		font-size: var(--text-sm);
		font-variant-numeric: tabular-nums;
		outline: none;
		min-width: 0;
		-moz-appearance: textfield;
	}

	.input-field input::-webkit-outer-spin-button,
	.input-field input::-webkit-inner-spin-button {
		-webkit-appearance: none;
		margin: 0;
	}

	.input-suffix {
		padding: 0.3rem 0.5rem 0.3rem 0;
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.input-field input:focus {
		outline: none;
	}

	.input-field:focus-within {
		border-color: var(--text-muted);
	}

	.input-meta {
		display: flex;
		gap: 0.75rem;
		margin-top: 0.25rem;
		padding: 0 0.25rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	/* Summary cards */
	.summary-cards {
		display: flex;
		gap: 0.75rem;
		margin-bottom: 1rem;
	}

	.summary-card {
		flex: 1;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 0.6rem 0.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.summary-card.total {
		border-color: var(--text-dim);
	}

	.card-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.card-amount {
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
		font-variant-numeric: tabular-nums;
	}

	/* Breakdown table */
	.breakdown-table {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		overflow: hidden;
	}

	.breakdown-header {
		display: flex;
		padding: 0.4rem 0.75rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		border-bottom: 1px solid var(--border-subtle);
	}

	.breakdown-group-label {
		padding: 0.4rem 0.75rem 0.15rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
		font-weight: 500;
		border-top: 1px solid var(--border-subtle);
	}

	.breakdown-row {
		display: flex;
		padding: 0.2rem 0.75rem;
		font-size: var(--text-sm);
	}

	.breakdown-row.highlight {
		font-weight: 600;
		padding-top: 0.3rem;
		padding-bottom: 0.3rem;
	}

	.breakdown-row.result {
		font-weight: 600;
		background: var(--surface-raised);
		padding-top: 0.4rem;
		padding-bottom: 0.4rem;
	}

	.breakdown-label {
		flex: 1;
		color: var(--text-secondary);
	}

	.breakdown-val {
		width: 8rem;
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--text-primary);
	}

	.breakdown-val.dim {
		color: var(--text-dim);
	}

	.breakdown-val.combined {
		width: 16rem;
		color: var(--text-muted);
	}

	.breakdown-header .breakdown-label {
		color: transparent;
	}

	.breakdown-header .breakdown-val {
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
	}

	/* Info panels */
	.info-panel {
		margin: 0;
		padding: 0 0.75rem;
		border-top: 1px solid var(--border-subtle);
	}

	.info-panel summary {
		padding: 0.35rem 0;
		font-size: var(--text-xs);
		color: var(--text-dim);
		cursor: pointer;
		user-select: none;
		list-style: none;
	}

	.info-panel summary::before {
		content: '+ ';
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
	}

	.info-panel[open] summary::before {
		content: '- ';
	}

	.info-panel summary::-webkit-details-marker {
		display: none;
	}

	.info-panel p {
		margin: 0 0 0.5rem;
		font-size: var(--text-xs);
		color: var(--text-muted);
		line-height: 1.5;
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-base);
		padding: 2rem 1rem;
		text-align: center;
	}

	.empty code {
		background: var(--surface-raised);
		padding: 0.1rem 0.3rem;
		border-radius: 0.2rem;
		font-size: var(--text-sm);
	}

	@media (max-width: 720px) {
		.tax-layout {
			grid-template-columns: 1fr;
		}

		.summary-cards {
			flex-direction: column;
		}

		.breakdown-val {
			width: 6rem;
		}
	}
</style>
