<script lang="ts">
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import {
    bulkInsertEncounters,
    extractEncounters,
    type ParsedDiagnosis,
    type ParsedEncounter,
  } from '$lib/api';
  import { Badge, Button, Select, type SelectOption } from '$lib/components/ui';
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
  let fileInput: HTMLInputElement | undefined = $state();
  let extracting = $state(false);
  let extractMode: 'text' | 'vision' | null = $state(null);
  let importing = $state(false);
  let error = $state('');
  let warnings: string[] = $state([]);
  let parsed: ParsedEncounter[] = $state([]);

  function pickFile(e: Event) {
    const input = e.target as HTMLInputElement;
    file = input.files?.[0] ?? null;
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    const f = e.dataTransfer?.files?.[0];
    if (f) file = f;
  }

  function handleFilePaste(e: ClipboardEvent) {
    const f = e.clipboardData?.files?.[0];
    if (f) {
      e.preventDefault();
      file = f;
    }
  }

  function clearFile() {
    file = null;
    documentId = null;
    if (fileInput) fileInput.value = '';
  }

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

<div class="header">
  <h1>Import encounter</h1>
  <Button href="{base}/health/history">Back</Button>
</div>

<div class="card">
  <div
    class="dropzone"
    ondragover={(e) => e.preventDefault()}
    ondrop={handleDrop}
    onpaste={handleFilePaste}
    role="presentation"
  >
    {#if file}
      <div class="picked">
        {file.name}
        <span class="hint">({Math.round(file.size / 1024)} KB)</span>
        <button type="button" class="clear" onclick={clearFile} aria-label="Clear selected file"
          >×</button
        >
      </div>
    {:else}
      <p>
        Drop, paste, or pick a screenshot or PDF of your visit paperwork — after-visit summary,
        discharge note, referral letter, etc.
      </p>
      <p class="hint">
        The LLM extracts date, provider, facility, reason, and any diagnoses listed, then matches
        each to the canonical encounter type. You'll review everything before it's saved.
      </p>
    {/if}
    <input
      bind:this={fileInput}
      type="file"
      accept="image/*,application/pdf"
      onchange={pickFile}
      class:hidden={file !== null}
    />
  </div>

  <div class="actions">
    <Button
      variant="primary"
      disabled={!file || extracting}
      onclick={doExtract}
      loading={extracting}
      loadingLabel="Extracting…">Extract</Button
    >
  </div>
</div>

{#if error}
  <div class="banner error">{error}</div>
{/if}

{#if extracting}
  <div class="card extracting">
    <span class="spinner" aria-hidden="true"></span>
    Extracting encounter from the source — this can take a few seconds.
  </div>
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
    <h2>Review {parsed.length} encounter{parsed.length === 1 ? '' : 's'}</h2>
    {#if extractMode}
      <span class="meta">Extracted via {extractMode === 'vision' ? 'vision' : 'text'} mode</span>
    {/if}
  </div>

  {#if documentId !== null && file}
    <p class="attach-note">
      <strong>{file.name}</strong> will be kept and attached to the records below.
    </p>
  {/if}

  {#each parsed as row, i (i)}
    <div class="enc-card" class:warn={!row.encounter_date}>
      <div class="enc-head">
        <Badge variant={confidenceVariant(row.confidence)}>{row.confidence}</Badge>
        <Button size="sm" onclick={() => removeRow(i)}>Remove encounter</Button>
      </div>

      <div class="grid">
        <label>
          <span>Date</span>
          <input type="date" bind:value={row.encounter_date} />
        </label>
        <label>
          <span>Type</span>
          <Select
            value={row.encounter_type}
            options={encounterTypeOptions}
            onValueChange={(v) => (row.encounter_type = v)}
            ariaLabel="Type"
            fullWidth
          />
        </label>
        <label>
          <span>Provider</span>
          <input
            type="text"
            value={row.provider || ''}
            oninput={(e) => (row.provider = (e.currentTarget as HTMLInputElement).value || null)}
          />
        </label>
        <label>
          <span>Facility</span>
          <input
            type="text"
            value={row.facility || ''}
            oninput={(e) => (row.facility = (e.currentTarget as HTMLInputElement).value || null)}
          />
        </label>
        <label>
          <span>Specialty</span>
          <input
            type="text"
            value={row.specialty || ''}
            oninput={(e) => (row.specialty = (e.currentTarget as HTMLInputElement).value || null)}
          />
        </label>
      </div>

      <label class="full">
        <span>Reason</span>
        <input
          type="text"
          value={row.reason || ''}
          oninput={(e) => (row.reason = (e.currentTarget as HTMLInputElement).value || null)}
        />
      </label>

      <label class="full">
        <span>Notes</span>
        <textarea
          rows="3"
          value={row.notes || ''}
          oninput={(e) => (row.notes = (e.currentTarget as HTMLTextAreaElement).value || null)}
        ></textarea>
      </label>

      <div class="diag-head">
        <h3>Diagnoses ({row.diagnoses.length})</h3>
        <Button size="sm" onclick={() => addDiagnosis(row)}>+ Add diagnosis</Button>
      </div>

      {#if row.diagnoses.length > 0}
        <div class="table-scroll">
          <table class="grid">
            <thead>
              <tr>
                <th>Name</th>
                <th>ICD-10</th>
                <th>Status</th>
                <th>Severity</th>
                <th class="row-actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each row.diagnoses as d, j (j)}
                <tr>
                  <td>
                    <input
                      type="text"
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
                    <input
                      type="text"
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
                  <td class="row-actions">
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

  <div class="actions">
    <Button variant="primary" disabled={importing} onclick={doImport}>
      {importing
        ? 'Importing…'
        : `Import ${parsed.length} encounter${parsed.length === 1 ? '' : 's'}`}
    </Button>
  </div>
{/if}

<style>
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }
  h1 {
    font-size: var(--text-lg);
    font-weight: 500;
    margin: 0;
  }
  h2 {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-dim);
    font-weight: 500;
    margin: 0;
  }
  h3 {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-dim);
    font-weight: 500;
    margin: 0;
  }

  .card {
    padding: 0.85rem 1rem;
    margin-bottom: 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .dropzone {
    border: 2px dashed var(--border-default);
    border-radius: var(--radius-card);
    padding: 1.5rem;
    text-align: center;
    color: var(--text-muted);
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .picked {
    color: var(--text-primary);
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    justify-content: center;
  }
  .hint {
    color: var(--text-dim);
    font-size: var(--text-xs);
    margin: 0;
  }
  .clear {
    background: none;
    border: 1px solid var(--border-default);
    border-radius: 50%;
    color: var(--text-muted);
    width: 1.4rem;
    height: 1.4rem;
    line-height: 1;
    cursor: pointer;
    font-size: var(--text-sm);
  }
  .clear:hover {
    background: var(--surface-raised);
    color: var(--text-primary);
  }
  .hidden {
    display: none;
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .card .actions {
    margin-top: 0.25rem;
  }

  .extracting {
    flex-direction: row;
    align-items: center;
    gap: 0.5rem;
    color: var(--text-muted);
    font-size: var(--text-sm);
  }
  .spinner {
    display: inline-block;
    width: 0.85rem;
    height: 0.85rem;
    border: 2px solid var(--border-default);
    border-top-color: var(--text-muted);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .attach-note {
    margin: 0 0 0.75rem;
    font-size: var(--text-xs);
    color: var(--text-muted);
  }
  .review-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.5rem;
    margin: 0.75rem 0 0.5rem;
  }
  .meta {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .enc-card {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: 0.85rem 1rem;
    margin-bottom: 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  .enc-card.warn {
    background: hsla(35, 60%, 60%, 0.08);
  }
  .enc-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    gap: 0.65rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: var(--text-sm);
    min-width: 0;
  }
  label > span {
    color: var(--text-muted);
    font-size: var(--text-xs);
  }
  input,
  textarea {
    padding: 0.3rem 0.5rem;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: 0.3rem;
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    box-sizing: border-box;
    min-width: 0;
  }
  textarea {
    resize: vertical;
    font-family: inherit;
  }

  .diag-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 0.25rem;
    border-top: 1px solid var(--border-subtle);
    padding-top: 0.65rem;
  }

  /* Compact inputs inside the diagnoses table — mirrors the immunization
	   review-table sizing so the nested grid doesn't overpower the card. */
  table.grid input {
    padding: 0.25rem 0.4rem;
  }
  td.row-actions,
  th.row-actions {
    text-align: right;
    white-space: nowrap;
  }
</style>
