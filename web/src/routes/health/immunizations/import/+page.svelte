<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import {
    bulkInsertImmunizations,
    extractImmunizations,
    listImmunizationRefs,
    parseImmunizations,
    type ImmunizationRef,
    type ParsedImmunization,
  } from '$lib/api';
  import { Badge, Button, Select, type SelectOption } from '$lib/components/ui';
  import { confidenceVariant } from '$lib/health/status';
  import FileDropZone from '$lib/components/health/FileDropZone.svelte';

  type Mode = 'file' | 'paste';
  let mode: Mode = $state('file');

  // File path
  let file: File | null = $state(null);
  // The kept upload from /extract, threaded into /bulk so every imported
  // row carries the card that proves it. Null on the paste-text path.
  let documentId: number | null = $state(null);
  let extracting = $state(false);
  let extractMode: 'text' | 'vision' | null = $state(null);

  // Paste path
  let raw = $state('');
  let parsing = $state(false);

  // Shared state
  let importing = $state(false);
  let error = $state('');
  let warnings: string[] = $state([]);
  let parsed: ParsedImmunization[] = $state([]);
  let refs: ImmunizationRef[] = $state([]);

  const nameOptions: SelectOption[] = $derived([
    { value: 'Unknown', label: 'Unknown — leave as note' },
    ...refs.map((r) => ({ value: r.name, label: r.display_name })),
  ]);

  onMount(async () => {
    try {
      const r = await listImmunizationRefs();
      refs = r.refs;
    } catch {
      // non-fatal
    }
  });

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
      const out = await extractImmunizations(file);
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

  async function doParse() {
    error = '';
    warnings = [];
    parsing = true;
    documentId = null;
    try {
      const out = await parseImmunizations(raw);
      parsed = out.rows;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to parse';
    } finally {
      parsing = false;
    }
  }

  async function doImport() {
    error = '';
    const missing = parsed.filter((r) => !r.date_given);
    if (missing.length > 0) {
      error = `${missing.length} row(s) need a date before import. Edit or remove them first.`;
      return;
    }
    importing = true;
    try {
      const out = await bulkInsertImmunizations(parsed, documentId);
      if (out.status === 'ok') {
        await goto(`${base}/health/immunizations`);
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

  function switchMode(m: Mode) {
    if (m === mode) return;
    mode = m;
    parsed = [];
    warnings = [];
    error = '';
    extractMode = null;
    documentId = null;
  }
</script>

<div class="header">
  <h1>Import immunizations</h1>
  <Button href="{base}/health/immunizations">Back</Button>
</div>

<div class="tabs" role="tablist">
  <button
    type="button"
    role="tab"
    aria-selected={mode === 'file'}
    class="tab"
    class:active={mode === 'file'}
    onclick={() => switchMode('file')}
  >
    Screenshot or PDF
  </button>
  <button
    type="button"
    role="tab"
    aria-selected={mode === 'paste'}
    class="tab"
    class:active={mode === 'paste'}
    onclick={() => switchMode('paste')}
  >
    Paste text
  </button>
</div>

{#if mode === 'file'}
  <div class="card">
    <FileDropZone bind:file onClear={() => (documentId = null)}>
      <p>Drop, paste, or pick a screenshot or PDF of your immunization list.</p>
      <p class="hint">
        The LLM extracts vaccine name + date and matches each row to a canonical family. You'll
        review the table before anything is saved.
      </p>
    </FileDropZone>

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
{:else}
  <div class="card">
    <p class="hint">
      Paste a MyChart / EHR vaccine list below. Lines like
      <code>"Influenza (Given 11/28/2025)"</code> are recognised and matched to a canonical vaccine family.
      Review the table before importing.
    </p>
    <textarea
      class="paste"
      rows="10"
      bind:value={raw}
      placeholder={'INFS Pres Free 6mos-Adult (Fluzone trivalent) (influenza) (Given 11/28/2025)\nTdap (Tetanus, diphtheria, acellular pertussis) (Given 12/1/2016)\nTYDvi (Typhoid, ViCPs) (Given 10/23/2023)'}
    ></textarea>
    <div class="actions">
      <Button
        variant="primary"
        disabled={!raw.trim() || parsing}
        onclick={doParse}
        loading={parsing}
        loadingLabel="Parsing…">Parse</Button
      >
    </div>
  </div>
{/if}

{#if error}
  <div class="banner error">{error}</div>
{/if}

{#if extracting}
  <div class="card extracting">
    <span class="spinner" aria-hidden="true"></span>
    Extracting immunizations from the source — this can take a few seconds.
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
    <h2>Review {parsed.length} row{parsed.length === 1 ? '' : 's'}</h2>
    {#if extractMode}
      <span class="meta">Extracted via {extractMode === 'vision' ? 'vision' : 'text'} mode</span>
    {/if}
  </div>

  {#if documentId !== null && file}
    <p class="attach-note">
      <strong>{file.name}</strong> will be kept as proof and attached to the rows below.
    </p>
  {/if}

  <div class="table-scroll">
    <table class="grid">
      <thead>
        <tr>
          <th>Vaccine</th>
          <th>Date</th>
          <th>Product</th>
          <th>Confidence</th>
          {#if mode === 'paste'}
            <th>Source line</th>
          {/if}
          <th class="row-actions"></th>
        </tr>
      </thead>
      <tbody>
        {#each parsed as row, i (i)}
          <tr class:warn={row.name === 'Unknown' || !row.date_given}>
            <td>
              <Select
                value={row.name}
                options={nameOptions}
                onValueChange={(v) => (row.name = v)}
                ariaLabel="Vaccine"
                fullWidth
              />
            </td>
            <td>
              <input type="date" bind:value={row.date_given} />
            </td>
            <td>
              <input
                type="text"
                value={row.product_name || ''}
                oninput={(e) =>
                  (row.product_name = (e.currentTarget as HTMLInputElement).value || null)}
              />
            </td>
            <td>
              <Badge variant={confidenceVariant(row.confidence)}>{row.confidence}</Badge>
            </td>
            {#if mode === 'paste'}
              <td class="src">{row.source_line}</td>
            {/if}
            <td class="row-actions">
              <Button size="sm" onclick={() => removeRow(i)}>Remove</Button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="actions">
    <Button variant="primary" disabled={importing} onclick={doImport}>
      {importing ? 'Importing…' : `Import ${parsed.length} rows`}
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

  .tabs {
    display: flex;
    gap: 0.25rem;
    margin-bottom: 0.75rem;
    border-bottom: 1px solid var(--border-subtle);
  }
  .tab {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-sm);
    padding: 0.45rem 0.75rem;
    cursor: pointer;
    margin-bottom: -1px;
  }
  .tab:hover {
    color: var(--text-primary);
  }
  .tab.active {
    color: var(--text-primary);
    border-bottom-color: var(--accent-blue);
  }

  .card {
    padding: 0.85rem 1rem;
    margin-bottom: 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .hint {
    color: var(--text-dim);
    font-size: var(--text-xs);
    margin: 0;
  }

  .paste {
    width: 100%;
    padding: 0.5rem 0.65rem;
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    color: var(--text-primary);
    font-family: var(--font-mono);
    font-size: var(--text-sm);
    box-sizing: border-box;
    resize: vertical;
  }
  .paste:focus {
    outline: 1px solid var(--accent-blue);
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
    /* `flex-direction` with no `display: flex` is inert, so the spinner sat
       above its label on this page and beside it on bloodwork/upload, which
       had the declaration. */
    display: flex;
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
  code {
    background: var(--surface-raised);
    padding: 0 0.3rem;
    border-radius: 0.2rem;
    font-size: 0.85em;
  }

  tr.warn td {
    background: hsla(35, 60%, 60%, 0.08);
  }
  td.row-actions,
  th.row-actions {
    text-align: right;
    white-space: nowrap;
  }
  td.src {
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--text-dim);
  }
  input {
    padding: 0.25rem 0.4rem;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: 0.3rem;
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
  }
</style>
