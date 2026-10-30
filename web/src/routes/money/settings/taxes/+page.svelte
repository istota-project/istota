<script lang="ts">
  import { onMount } from 'svelte';
  import { SettingsLayout } from '$lib/components/settings';
  import { X } from '@lucide/svelte';
  import {
    Input,
    Select,
    Card,
    Field,
    Button,
    IconButton,
    AutocompleteInput,
    FormStack,
  } from '@minot/ui';
  import { createDraft } from '@minot/ui/ui/draft.svelte';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { notifyError, notifySuccess } from '@minot/ui/stores/notices';
  import TaxDisclaimer from '$lib/components/money/TaxDisclaimer.svelte';
  import TaxRatesCard from '$lib/components/money/TaxRatesCard.svelte';
  import RateProvenanceLine from '$lib/components/money/RateProvenanceLine.svelte';
  import {
    deleteTaxSchedule,
    getResolvedTaxRates,
    getTaxJurisdictions,
    getTaxSettings,
    getTaxPatterns,
    putTaxPatterns,
    getAccounts,
    type TaxPatterns,
    putTaxSchedule,
    putTaxYearRates,
    updateTaxSettings,
    type ResolvedRates,
    type TaxJurisdiction,
    type TaxSettings,
  } from '$lib/money/api';

  let settings = $state<TaxSettings | null>(null);
  let resolved = $state<ResolvedRates | null>(null);
  let jurisdictions = $state<TaxJurisdiction[]>([]);
  let loading = $state(true);
  let loadError = $state('');
  let saving = $state(false);
  const patterns = createDraft<TaxPatterns>({ se_income: [], se_expense: [] });
  let accounts = $state<string[]>([]);
  const PATTERN_KINDS = [
    { kind: 'se_income', label: 'SE income', root: 'Income:' },
    { kind: 'se_expense', label: 'SE expense', root: 'Expenses:' },
  ] as const;
  function patternProblem(values: string[]) {
    if (!values.length || values.some((value) => !value.trim())) {
      return 'Add at least one pattern, with no blank rows.';
    }
    try {
      // The server's check is Python's; this catches what a person mistypes.
      for (const value of values) {
        if (new RegExp(`^(?:${value.trim()})`).test('')) return 'This matches every account.';
      }
    } catch {
      return 'Not a valid regular expression.';
    }
    return '';
  }
  // Only an edited list blocks Save: a ledger with no patterns stored must
  // still be able to save its other tax settings.
  // Only a first load that failed: its empty lists must not be saved. A reload
  // failing after a save leaves a form still holding real values.
  let loadFailed = $derived(!settings && !!loadError);
  let hasPatternProblem = $derived(
    patterns.dirty && PATTERN_KINDS.some(({ kind }) => !!patternProblem(patterns.value[kind])),
  );

  // Edits are held until the app-bar Save, in the shape the endpoints take:
  // one patch for the scalar settings, one per schedule row, one for payroll.
  // An explicit `null` in a schedule patch reverts that field to the shipped
  // value; an absent key leaves it alone. `null` cannot mean both.
  let settingsPatch = $state<Partial<TaxSettings>>({});
  let schedulePatches = $state<
    Record<string, { standard_deduction?: number | null; brackets?: number[][] | null }>
  >({});
  let payrollPatch = $state<Record<string, number | null>>({});

  /** How many fields a schedule row has — reverting all of them deletes it. */
  const SCHEDULE_FIELDS = 2;

  // An unsaveable bracket table blocks the Save rather than being sent and
  // 400ing — or worse, being silently trimmed of the row that is half typed.
  let bracketProblems = $state<Record<string, string>>({});
  let hasBracketProblem = $derived(Object.values(bracketProblems).some(Boolean));

  let dirty = $derived(
    Object.keys(settingsPatch).length > 0 ||
      Object.keys(schedulePatches).length > 0 ||
      Object.keys(payrollPatch).length > 0 ||
      patterns.dirty,
  );

  // What the form shows: the pending edit if there is one, else what loaded.
  let filingStatus = $derived(settingsPatch.filing_status ?? settings?.filing_status ?? 'mfj');
  let taxYear = $derived(settingsPatch.tax_year ?? settings?.tax_year ?? new Date().getFullYear());
  let stateCode = $derived(settingsPatch.state ?? settings?.state ?? '');

  let selectedJurisdiction = $derived(jurisdictions.find((j) => j.code === stateCode) ?? null);

  const FILING_OPTIONS = [
    { value: 'mfj', label: 'Married filing jointly' },
    { value: 'single', label: 'Single' },
  ];

  let stateOptions = $derived([
    { value: '', label: 'No state income tax' },
    ...jurisdictions.map((j) => ({
      value: j.code,
      label: j.taxes_income ? j.name : `${j.name} (no income tax)`,
    })),
  ]);

  // Said before they pick rather than after — the difference between choosing a
  // state and discovering the page cannot compute one.
  let stateHint = $derived.by(() => {
    if (!selectedJurisdiction) return '';
    if (!selectedJurisdiction.taxes_income) {
      return `${selectedJurisdiction.name} levies no individual income tax on wage or self-employment income, so no state figures are shown.`;
    }
    if (!selectedJurisdiction.has_bundled_data) {
      return `No rates are bundled for ${selectedJurisdiction.name}. Enter its brackets below, or the estimate will show federal only.`;
    }
    return '';
  });

  async function load() {
    loading = true;
    try {
      const [s, j, p, rates] = await Promise.all([
        getTaxSettings(),
        getTaxJurisdictions(),
        getTaxPatterns(),
        getResolvedTaxRates(),
      ]);
      settings = s;
      jurisdictions = j;
      resolved = rates;
      patterns.reset(p);
      loadError = '';
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load tax settings';
    } finally {
      loading = false;
    }
  }

  /** Re-read the resolved rates for a (year, status) the user has not saved yet. */
  async function reloadResolved() {
    try {
      resolved = await getResolvedTaxRates({
        year: taxYear,
        filingStatus,
        // Passed explicitly, including '' — the preview has to follow the
        // pick, or the form shows the previous state's brackets under the new
        // state's name.
        state: stateCode,
      });
    } catch {
      // Non-fatal: the form keeps the figures it already has, and the Save is
      // still what decides. A failed preview should not block an edit.
    }
  }

  function editSettings(patch: Partial<TaxSettings>) {
    settingsPatch = { ...settingsPatch, ...patch };
  }

  function scheduleKey(jurisdiction: string) {
    return `${taxYear}:${jurisdiction}:${filingStatus}`;
  }

  function editSchedule(
    jurisdiction: string,
    patch: { standard_deduction?: number | null; brackets?: number[][] | null },
  ) {
    const key = scheduleKey(jurisdiction);
    schedulePatches = {
      ...schedulePatches,
      [key]: { ...(schedulePatches[key] ?? {}), ...patch },
    };
  }

  async function save() {
    if (saving || loading || loadFailed || hasBracketProblem || hasPatternProblem) return;
    saving = true;
    try {
      // Settings first: a schedule row is keyed on year and filing status, so
      // saving a bracket before the year it belongs to would file it under the
      // old one.
      if (Object.keys(settingsPatch).length) {
        await updateTaxSettings(settingsPatch);
      }
      const year = taxYear;
      for (const [key, patch] of Object.entries(schedulePatches)) {
        // All three coordinates come off the key: a pending patch belongs to
        // the year and status it was typed under, and reading them off the live
        // values would file it under whatever was picked afterwards.
        const [patchYear, jurisdiction, patchStatus] = key.split(':');

        // Reverting *both* fields is a row delete — resolution reads "row
        // present" as "overridden", so a row of nulls would never fall back to
        // the shipped values. Reverting one is an ordinary write of an explicit
        // null, which the server merges per field; issuing a delete for that
        // case would silently drop the other field's override too.
        const reverted = Object.values(patch).filter((v) => v === null).length;
        const isFullRevert = reverted > 0 && reverted === SCHEDULE_FIELDS;
        if (isFullRevert) {
          await deleteTaxSchedule(Number(patchYear), jurisdiction, patchStatus);
        } else {
          await putTaxSchedule(Number(patchYear), jurisdiction, patchStatus, patch);
        }
      }
      if (Object.keys(payrollPatch).length) {
        await putTaxYearRates(year, payrollPatch);
      }
      if (patterns.dirty) {
        await putTaxPatterns({
          se_income: patterns.value.se_income.map((value) => value.trim()),
          se_expense: patterns.value.se_expense.map((value) => value.trim()),
        });
        patterns.reset();
      }
      settingsPatch = {};
      schedulePatches = {};
      payrollPatch = {};
      await load();
      notifySuccess('Tax settings saved');
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to save tax settings');
      throw e;
    } finally {
      saving = false;
    }
  }

  useSettingsSave(() => ({
    dirty,
    saving,
    save,
    disabled: loading || loadFailed || hasBracketProblem || hasPatternProblem,
  }));

  onMount(() => {
    void load();
    // Suggestions are optional; a ledger read must not block editing settings.
    void getAccounts({ ledger: $selectedLedger || undefined })
      .then((response) => {
        accounts = response.accounts.map((row) => row.account);
      })
      .catch(() => {});
  });

  const PAYROLL_FIELDS = [
    { key: 'ss_wage_base', label: 'Social Security wage base', step: '1' },
    { key: 'ss_rate', label: 'Social Security rate', step: '0.0001' },
    { key: 'medicare_rate', label: 'Medicare rate', step: '0.0001' },
    { key: 'se_taxable_fraction', label: 'SE taxable fraction', step: '0.0001' },
  ];

  function payrollValue(key: string): number | string {
    if (key in payrollPatch) return payrollPatch[key] ?? '';
    return resolved?.payroll?.[key]?.value ?? '';
  }
</script>

<SettingsLayout
  title="Taxes"
  description="The rates behind the quarterly estimate, and where they came from. Shipped figures are used unless you override them."
  {loading}
  error={settings ? loadError : ''}
  loadError={settings ? '' : loadError}
  onRetry={load}
>
  <Card title="Filing">
    <Field label="Filing status">
      <Select
        fullWidth
        value={filingStatus}
        options={FILING_OPTIONS}
        onValueChange={(v: string) => {
          editSettings({ filing_status: v as 'mfj' | 'single' });
          reloadResolved();
        }}
      />
    </Field>

    <Field
      label="Tax year"
      hint="The year the estimate is for. Rates are per-year; changing this changes which set you are editing below."
    >
      <Input
        type="number"
        value={taxYear}
        oninput={(e) => {
          const v = Number((e.currentTarget as HTMLInputElement).value);
          if (Number.isFinite(v) && v > 1900) {
            editSettings({ tax_year: v });
            reloadResolved();
          }
        }}
      />
    </Field>

    <Field label="State" warning={stateHint || undefined}>
      <Select
        fullWidth
        value={stateCode}
        options={stateOptions}
        onValueChange={(v: string) => {
          editSettings({ state: v });
          reloadResolved();
        }}
      />
    </Field>
  </Card>

  <Card
    title="Accounts counted as self-employment"
    description="Each pattern is a regular expression anchored at the start of the account name, so Income:Consulting includes its subaccounts. Net SE income is the matched income minus the matched expenses."
  >
    <FormStack disabled={saving}>
      {#each PATTERN_KINDS as { kind, label, root } (kind)}
        <Field
          {label}
          wide
          error={patterns.dirty ? patternProblem(patterns.value[kind]) : ''}
          description={!patterns.dirty && !patterns.value[kind].length
            ? 'None set, so the estimate counts none.'
            : undefined}
        >
          <div class="vstack" style:--vstack-gap="var(--space-2)">
            {#each patterns.value[kind] as _, index (index)}
              <div class="pattern-row">
                <AutocompleteInput
                  ariaLabel={`${label} pattern ${index + 1}`}
                  bind:value={patterns.value[kind][index]}
                  options={accounts.filter((account) => account.startsWith(root))}
                  monospace
                />
                <IconButton
                  icon={X}
                  size="sm"
                  danger
                  label={`Remove ${label} pattern ${index + 1}`}
                  onclick={() => {
                    patterns.value[kind].splice(index, 1);
                  }}
                />
              </div>
            {/each}
            <div>
              <Button
                variant="secondary"
                size="sm"
                onclick={() => {
                  patterns.value[kind].push('');
                }}>Add {label} pattern</Button
              >
            </div>
          </div>
        </Field>
      {/each}
    </FormStack>
  </Card>

  {#if resolved}
    <TaxRatesCard
      title="Federal rates"
      description="Brackets and the standard deduction for the year and filing status above."
      {taxYear}
      standardDeduction={resolved.federal.standard_deduction}
      brackets={resolved.federal.brackets}
      provenance={resolved.federal.provenance}
      onproblem={(p) => (bracketProblems = { ...bracketProblems, federal: p })}
      onEdit={(patch) => editSchedule('federal', patch)}
    />

    {#if resolved.state}
      <TaxRatesCard
        title="{resolved.state.name} rates"
        description={resolved.state.starts_from === 'federal_taxable_income'
          ? 'Applied to federal taxable income, so the federal standard deduction is already inside the base.'
          : resolved.state.starts_from === 'gross_compensation'
            ? 'Applied to gross wage and self-employment income, with no above-the-line deductions.'
            : 'Applied to federal AGI, which already carries the above-the-line half-SE deduction.'}
        {taxYear}
        standardDeduction={resolved.state.standard_deduction}
        brackets={resolved.state.brackets}
        provenance={resolved.state.provenance}
        unavailableNotice={resolved.state.available
          ? ''
          : resolved.state.reason === 'no_income_tax'
            ? `${resolved.state.name} levies no individual income tax, so nothing here affects the estimate.`
            : 'No brackets for this state and filing status. The estimate shows federal only until you enter some.'}
        available={resolved.state.available}
        onproblem={(p) => (bracketProblems = { ...bracketProblems, state: p })}
        onEdit={(patch) => editSchedule(resolved!.state!.code, patch)}
      />
    {/if}

    <Card
      title="Payroll rates"
      description="Federal, year-keyed and the same for every filing status."
    >
      <RateProvenanceLine provenance={resolved.federal.provenance} {taxYear} />
      {#each PAYROLL_FIELDS as f (f.key)}
        <Field label={f.label}>
          <Input
            type="number"
            step={f.step}
            value={payrollValue(f.key)}
            oninput={(e) => {
              const raw = (e.currentTarget as HTMLInputElement).value;
              payrollPatch = {
                ...payrollPatch,
                [f.key]: raw.trim() === '' ? null : Number(raw),
              };
            }}
          />
          {#if resolved.payroll?.[f.key]?.overridden}
            <p class="overridden">Your value, not the shipped one.</p>
          {/if}
        </Field>
      {/each}
    </Card>
  {/if}

  <TaxDisclaimer />
</SettingsLayout>

<style>
  .pattern-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--space-2);
    align-items: center;
  }

  .overridden {
    margin: var(--space-1) 0 0;
    font-size: var(--text-xs);
    color: var(--status-info-fg);
  }
</style>
