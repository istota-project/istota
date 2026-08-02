<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    extractHealthPanel,
    saveHealthBiomarkers,
    updateHealthPanel,
    uploadHealthPanel,
    healthPanelSourceUrl,
    type Biomarker,
  } from '$lib/api';
  import { Button, Field, Select } from '$lib/components/ui';
  import FileDropZone from '$lib/components/health/FileDropZone.svelte';

  const flagOptions = [
    { value: '', label: '—' },
    { value: 'H', label: 'H' },
    { value: 'L', label: 'L' },
    { value: 'C', label: 'C' },
  ];

  let file: File | null = $state(null);

  let uploading = $state(false);
  let extracting = $state(false);
  let saving = $state(false);

  let panelId: number | null = $state(null);
  let mime: string | null = $state(null);
  let collision: { existing_id: number; drawn_at: string; lab_name: string | null } | null =
    $state(null);
  let warnings: string[] = $state([]);
  let extracted: Partial<Biomarker>[] = $state([]);

  // Panel-level metadata — uploaded with a placeholder date, replaced from
  // the LLM extraction, then editable before confirm.
  const today = new Date().toISOString().slice(0, 10);
  let drawnAt = $state(today);
  let labName = $state('');
  let panelType = $state('');

  let error = $state('');
  let info = $state('');

  function handleFile(e: Event) {
    const input = e.target as HTMLInputElement;
    file = input.files?.[0] ?? null;
  }

  async function doUpload() {
    if (!file) {
      error = 'Please pick a file first.';
      return;
    }
    error = '';
    info = '';
    uploading = true;
    try {
      // Use today as a placeholder date so the panel row creates cleanly;
      // the LLM will fill the real ``drawn_at`` during extraction.
      const resp = await uploadHealthPanel(file, today);
      panelId = resp.id;
      mime = file.type || null;
      collision = resp.collision ?? null;
      info = 'Uploaded. Running extraction…';
      await doExtract();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Upload failed';
    } finally {
      uploading = false;
    }
  }

  async function doExtract() {
    if (panelId == null) return;
    extracting = true;
    try {
      const resp = await extractHealthPanel(panelId);
      extracted = resp.biomarkers.map((b, idx) => ({
        id: -(idx + 1),
        panel_id: panelId!,
        name: b.name || '',
        display_name: b.display_name ?? null,
        value: Number(b.value ?? 0),
        unit: b.unit || '',
        ref_range_low: b.ref_range_low ?? null,
        ref_range_high: b.ref_range_high ?? null,
        flag: b.flag ?? null,
      }));
      warnings = resp.warnings || [];
      // Prefill the editable header from the LLM's metadata extraction.
      // Falls back to today (the placeholder we uploaded with) when the
      // model couldn't find a date.
      if (resp.drawn_at) drawnAt = resp.drawn_at;
      if (resp.lab_name) labName = resp.lab_name;
      if (resp.panel_type) panelType = resp.panel_type;
      info = `Extracted ${extracted.length} biomarkers. Review and confirm.`;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Extraction failed';
    } finally {
      extracting = false;
    }
  }

  function addRow() {
    extracted = [
      ...extracted,
      {
        id: -Date.now(),
        panel_id: panelId!,
        name: '',
        display_name: null,
        value: 0,
        unit: '',
        ref_range_low: null,
        ref_range_high: null,
        flag: null,
      },
    ];
  }

  function removeRow(i: number) {
    extracted = extracted.filter((_, idx) => idx !== i);
  }

  async function confirm() {
    if (panelId == null) return;
    saving = true;
    error = '';
    try {
      // Persist whatever the user has in the metadata fields (extracted
      // values or their edits) before flipping the panel out of draft.
      await updateHealthPanel(panelId, {
        drawn_at: drawnAt,
        lab_name: labName,
        panel_type: panelType,
      });
      await saveHealthBiomarkers(
        panelId,
        extracted.map((b) => ({
          name: b.name!,
          display_name: b.display_name ?? undefined,
          value: Number(b.value),
          unit: b.unit!,
          ref_range_low: b.ref_range_low ?? undefined,
          ref_range_high: b.ref_range_high ?? undefined,
          flag: b.flag ?? undefined,
        })),
        true,
      );
      goto(`${base}/health/bloodwork/panel?id=${panelId}`);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }
</script>

<div class="page">
  <a class="back" href="{base}/health/bloodwork">← Bloodwork</a>
  <h1>Upload lab results</h1>

  {#if panelId == null}
    <div class="card">
      <FileDropZone bind:file>
        <p>Drop or paste a PDF or image of the lab report here, or use the file picker.</p>
        <p class="caption">
          Date drawn, lab, and panel type are extracted automatically; you'll review them next.
        </p>
      </FileDropZone>

      {#if error}<div class="banner error">{error}</div>{/if}
      {#if info}<div class="banner info">{info}</div>{/if}

      <div class="actions">
        <Button
          variant="primary"
          onclick={doUpload}
          disabled={uploading || !file}
          loading={uploading}
          loadingLabel="Uploading…">Upload + extract</Button
        >
      </div>
    </div>
  {:else}
    {#if collision}
      <div class="banner warn">
        A panel from {collision.lab_name || '—'} on {collision.drawn_at} already exists. This upload is
        saved separately;
        <a href="{base}/health/bloodwork/panel?id={collision.existing_id}">view the existing one</a>
        to decide which to keep.
      </div>
    {/if}

    <div class="split">
      <div class="review-table">
        {#if !extracting}
          <div class="metadata">
            <Field label="Date drawn">
              <input type="date" bind:value={drawnAt} required />
            </Field>
            <Field label="Lab">
              <input type="text" bind:value={labName} placeholder="Quest, Kaiser, …" />
            </Field>
            <Field label="Panel type">
              <input type="text" bind:value={panelType} placeholder="CBC, CMP, Lipid, …" />
            </Field>
          </div>
        {/if}

        <h2>Extracted biomarkers</h2>

        {#if extracting}
          <div class="empty extracting">
            <span class="spinner" aria-hidden="true"></span>
            Extracting biomarkers and panel metadata from the source file…
          </div>
        {:else}
          {#if warnings.length > 0}
            <div class="banner warn">
              <strong>Heads up:</strong>
              <ul>
                {#each warnings as w}<li>{w}</li>{/each}
              </ul>
            </div>
          {/if}

          {#if extracted.length === 0}
            <div class="empty">
              No biomarkers extracted yet. Add rows manually, or retry extraction.
            </div>
          {:else}
            <table class="grid grid--dense">
              <thead>
                <tr>
                  <th>Marker</th><th>Value</th><th>Unit</th><th>Range (low / high)</th><th>Flag</th
                  ><th></th>
                </tr>
              </thead>
              <tbody>
                {#each extracted as b, i (b.id)}
                  <tr>
                    <td><input bind:value={b.name} placeholder="Hemoglobin" /></td>
                    <td><input type="number" step="any" bind:value={b.value} /></td>
                    <td><input bind:value={b.unit} placeholder="g/dL" /></td>
                    <td class="range-pair">
                      <input
                        type="number"
                        step="any"
                        bind:value={b.ref_range_low}
                        placeholder="low"
                      />
                      <input
                        type="number"
                        step="any"
                        bind:value={b.ref_range_high}
                        placeholder="high"
                      />
                    </td>
                    <td>
                      <Select
                        value={b.flag ?? ''}
                        options={flagOptions}
                        onValueChange={(v) => (b.flag = v === '' ? null : v)}
                        ariaLabel="Flag"
                      />
                    </td>
                    <td
                      ><button class="del" type="button" onclick={() => removeRow(i)}>×</button></td
                    >
                  </tr>
                {/each}
              </tbody>
            </table>
          {/if}

          {#if error}<div class="banner error">{error}</div>{/if}

          <div class="actions">
            <Button onclick={addRow}>+ Add row</Button>
            <Button onclick={doExtract}>Retry extraction</Button>
            <div class="spacer"></div>
            <Button
              variant="primary"
              disabled={saving || extracted.length === 0}
              onclick={confirm}
              loading={saving}>Confirm and save</Button
            >
          </div>
        {/if}
      </div>

      <div class="source">
        <div class="source-header">Source preview</div>
        {#if mime?.startsWith('image/')}
          <img src={healthPanelSourceUrl(panelId)} alt="Lab report" />
        {:else}
          <embed src={healthPanelSourceUrl(panelId)} type={mime || 'application/pdf'} />
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }
  .back {
    font-size: var(--text-xs);
    color: var(--text-muted);
    text-decoration: none;
  }
  h1 {
    font-size: var(--text-lg);
    font-weight: 500;
    margin: 0;
  }
  h2 {
    font-size: var(--text-base);
    font-weight: 500;
    margin: 0 0 var(--space-2);
  }
  .card {
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .metadata {
    display: grid;
    grid-template-columns: auto 1fr 1fr;
    gap: var(--space-2) var(--space-3);
    margin-bottom: var(--space-3);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
  }
  input {
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-1) var(--space-2);
  }
  .actions {
    display: flex;
    gap: var(--space-2);
    align-items: center;
    margin-top: var(--space-3);
  }
  .actions .spacer {
    flex: 1;
  }
  .extracting {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .split {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: var(--space-4);
  }
  @media (max-width: 900px) {
    .split {
      grid-template-columns: 1fr;
    }
  }
  .range-pair {
    display: flex;
    gap: var(--space-1);
  }
  .range-pair input {
    max-width: 5rem;
  }
  .review-table input {
    max-width: 9rem;
    font-size: var(--text-xs);
    padding: 0.15rem var(--space-1);
  }
  .del {
    background: none;
    border: none;
    color: var(--text-dim);
    cursor: pointer;
  }
  .del:hover {
    color: var(--status-danger-fg);
  }
  .source {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3);
  }
  .source-header {
    font-size: var(--text-sm);
    color: var(--text-muted);
    margin-bottom: var(--space-2);
  }
  .source img {
    width: 100%;
    height: auto;
  }
  .source embed {
    width: 100%;
    height: 600px;
  }
</style>
