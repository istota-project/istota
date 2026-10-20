<script lang="ts">
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import {
    bulkInsertEncounters,
    extractEncounters,
    type ParsedDiagnosis,
    type ParsedEncounter,
  } from '$lib/api';
  import {
    Badge,
    Button,
    Card,
    Field,
    FileDropZone,
    FormGrid,
    Select,
    type SelectOption,
    Input,
    TextArea,
    PageHeader,
    Spinner,
  } from '$lib/components/ui';
  import { confidenceVariant } from '$lib/health/status';

  const ENCOUNTER_TYPES = [
    'visit',
    'procedure',
    'screening',
    'hospitalization',
    'er',
    'telehealth',
    'imaging',
    'dental',
    'other',
  ];

  const DIAGNOSIS_STATUSES = ['active', 'chronic', 'resolved'] as const;

  const encounterTypeOptions: SelectOption[] = ENCOUNTER_TYPES.map((t) => ({
    value: t,
    label: t,
  }));
  const statusOptions: SelectOption[] = DIAGNOSIS_STATUSES.map((s) => ({
    value: s,
    label: s,
  }));
  const severityOptions: SelectOption[] = [
    { value: '', label: '—' },
    { value: 'mild', label: 'mild' },
    { value: 'moderate', label: 'moderate' },
    { value: 'severe', label: 'severe' },
  ];

  let file: File | null = $state(null);
  // The kept upload from /extract, threaded into /bulk so it is linked to
  // every encounter and diagnosis the import creates.
  let documentId: number | null = $state(null);
  let extracting = $state(false);
  let extractMode: 'text' | 'vision' | null = $state(null);
  let importing = $state(false);
  let error = $state('');
  let warnings: string[] = $state([]);
  let parsed: ParsedEncounter[] = $state([]);

  async function doExtract() {
    if (!file) {
      error = 'Pick a file first.';
      return;
    }
    error = '';
    warnings = [];
    parsed = [];
    extractMode = null;
    documentId = null;
    extracting = true;
    try {
      const out = await extractEncounters(file);
      documentId = out.document_id;
      parsed = out.rows;
      warnings = out.warnings || [];
      extractMode = out.mode;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Extraction failed';
    } finally {
      extracting = false;
    }
  }

  async function doImport() {
    error = '';
    const missing = parsed.filter((r) => !r.encounter_date);
    if (missing.length > 0) {
      error = `${missing.length} row(s) need a date before import. Edit or remove them first.`;
      return;
    }
    importing = true;
    try {
      const out = await bulkInsertEncounters(parsed, documentId);
      if (out.status === 'ok') {
        await goto(`${base}/health/history`);
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to import';
    } finally {
      importing = false;
    }
  }

  function removeRow(i: number) {
    parsed = parsed.filter((_, idx) => idx !== i);
  }

  function addDiagnosis(row: ParsedEncounter) {
    row.diagnoses = [...row.diagnoses, { name: '', icd10: null, status: 'active', severity: null }];
  }

  function removeDiagnosis(row: ParsedEncounter, j: number) {
    row.diagnoses = row.diagnoses.filter((_, idx) => idx !== j);
  }

  function setDiagnosisField<K extends keyof ParsedDiagnosis>(
    row: ParsedEncounter,
    j: number,
    key: K,
    value: ParsedDiagnosis[K],
  ) {
    row.diagnoses[j][key] = value;
    row.diagnoses = row.diagnoses;
  }
</script>

<div class="import-page">
  <PageHeader
    title="Import encounter"
    back={{ href: `${base}/health/history`, label: 'Medical history' }}
  />

  <Card as="div" class="import-card">
    <FileDropZone bind:file onClear={() => (documentId = null)}>
      <p>
        Drop, paste, or pick a screenshot or PDF of your visit paperwork — after-visit summary,
        discharge note, referral letter, etc.
      </p>
      <p class="caption">
        The LLM extracts date, provider, facility, reason, and any diagnoses listed, then matches
        each to the canonical encounter type. You'll review everything before it's saved.
      </p>
    </FileDropZone>

    <div class="actions hstack">
      <Button
        variant="primary"
        disabled={!file || extracting}
        onclick={doExtract}
        loading={extracting}
        loadingLabel="Extracting…">Extract</Button
      >
    </div>
  </Card>

  {#if error}
    <div class="banner error">{error}</div>
  {/if}

  {#if extracting}
    <Card as="div" class="import-card">
      <div class="extracting muted">
        <Spinner />
        Extracting encounter from the source — this can take a few seconds.
      </div>
    </Card>
  {/if}

  {#if warnings.length > 0}
    <div class="banner warn">
      <ul>
        {#each warnings as w (w)}
          <li>{w}</li>
        {/each}
      </ul>
    </div>
  {/if}

  {#if parsed.length > 0}
    <div class="review-head">
      <h2 class="micro-label">Review {parsed.length} encounter{parsed.length === 1 ? '' : 's'}</h2>
      {#if extractMode}
        <span class="caption"
          >Extracted via {extractMode === 'vision' ? 'vision' : 'text'} mode</span
        >
      {/if}
    </div>

    {#if documentId !== null && file}
      <p class="attach-note caption">
        <strong>{file.name}</strong> will be kept and attached to the records below.
      </p>
    {/if}

    {#each parsed as row, i (i)}
      <div class="enc-card" class:warn={!row.encounter_date}>
        <div class="enc-head">
          <Badge variant={confidenceVariant(row.confidence)}>{row.confidence}</Badge>
          <Button size="sm" onclick={() => removeRow(i)}>Remove encounter</Button>
        </div>

        <FormGrid>
          <Field label="Date">
            <Input type="date" bind:value={row.encounter_date} />
          </Field>
          <Field label="Type">
            <Select
              value={row.encounter_type}
              options={encounterTypeOptions}
              onValueChange={(v) => (row.encounter_type = v)}
              ariaLabel="Type"
              fullWidth
            />
          </Field>
          <Field label="Provider">
            <Input
              type="text"
              value={row.provider || ''}
              oninput={(e) => (row.provider = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="Facility">
            <Input
              type="text"
              value={row.facility || ''}
              oninput={(e) => (row.facility = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
          <Field label="Specialty">
            <Input
              type="text"
              value={row.specialty || ''}
              oninput={(e) => (row.specialty = (e.currentTarget as HTMLInputElement).value || null)}
            />
          </Field>
        </FormGrid>

        <Field label="Reason">
          <Input
            type="text"
            value={row.reason || ''}
            oninput={(e) => (row.reason = (e.currentTarget as HTMLInputElement).value || null)}
          />
        </Field>

        <Field label="Notes">
          <TextArea
            rows={3}
            value={row.notes || ''}
            oninput={(e) => (row.notes = (e.currentTarget as HTMLTextAreaElement).value || null)}
          />
        </Field>

        <div class="diag-head">
          <h3 class="micro-label">Diagnoses ({row.diagnoses.length})</h3>
          <Button size="sm" onclick={() => addDiagnosis(row)}>+ Add diagnosis</Button>
        </div>

        {#if row.diagnoses.length > 0}
          <div class="table-scroll">
            <table class="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>ICD-10</th>
                  <th>Status</th>
                  <th>Severity</th>
                  <th class="col-actions"></th>
                </tr>
              </thead>
              <tbody>
                {#each row.diagnoses as d, j (j)}
                  <tr>
                    <td>
                      <Input
                        type="text"
                        dense
                        aria-label="Diagnosis {j + 1} name"
                        value={d.name}
                        oninput={(e) =>
                          setDiagnosisField(
                            row,
                            j,
                            'name',
                            (e.currentTarget as HTMLInputElement).value,
                          )}
                      />
                    </td>
                    <td>
                      <Input
                        type="text"
                        dense
                        aria-label="Diagnosis {j + 1} ICD-10 code"
                        value={d.icd10 || ''}
                        oninput={(e) =>
                          setDiagnosisField(
                            row,
                            j,
                            'icd10',
                            (e.currentTarget as HTMLInputElement).value || null,
                          )}
                      />
                    </td>
                    <td>
                      <Select
                        value={d.status}
                        options={statusOptions}
                        onValueChange={(v) =>
                          setDiagnosisField(row, j, 'status', v as ParsedDiagnosis['status'])}
                        ariaLabel="Status"
                        fullWidth
                      />
                    </td>
                    <td>
                      <Select
                        value={d.severity || ''}
                        options={severityOptions}
                        onValueChange={(v) =>
                          setDiagnosisField(
                            row,
                            j,
                            'severity',
                            (v || null) as ParsedDiagnosis['severity'],
                          )}
                        ariaLabel="Severity"
                        fullWidth
                      />
                    </td>
                    <td class="col-actions">
                      <Button size="sm" onclick={() => removeDiagnosis(row, j)}>Remove</Button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </div>
    {/each}

    <div class="actions hstack">
      <Button variant="primary" loading={importing} loadingLabel="Importing…" onclick={doImport}>
        Import {parsed.length} encounter{parsed.length === 1 ? '' : 's'}
      </Button>
    </div>
  {/if}
</div>

<style>
  h2 {
    margin: 0;
  }
  h3 {
    margin: 0;
  }

  .import-page :global(.import-card) {
    margin-bottom: var(--space-3);
  }

  /* Typography is the global .caption; only the <p> reset stays here. */
  p.caption {
    margin: 0;
  }

  .import-page :global(.import-card .actions) {
    margin-top: var(--space-1);
  }

  .extracting {
    /* `flex-direction` with no `display: flex` is inert, so the spinner sat
       above its label on this page and beside it on labs/upload, which
       had the declaration. */
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--text-sm);
  }

  .attach-note {
    margin: 0 0 var(--space-3);
  }
  .review-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-2);
    margin: var(--space-3) 0 var(--space-2);
  }
  .enc-card {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
    margin-bottom: var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .enc-card.warn {
    background: var(--row-warn-bg);
  }
  .enc-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .diag-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: var(--space-1);
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-3);
  }
</style>
