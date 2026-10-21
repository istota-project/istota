<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getLabsMatrix,
    healthCsvExportUrl,
    importHealthCsv,
    listHealthPanels,
    SPECIMENS,
    specimenLabel,
    type LabsMatrix,
    type Specimen,
    type CsvImportSummary,
    type HealthPanel,
  } from '$lib/api';
  import { notifyError } from '@minot/ui/stores/notices';
  import {
    Alert,
    Badge,
    Button,
    EmptyState,
    Select,
    LoadState,
    PageHeader,
    Timestamp,
    Disclosure,
  } from '@minot/ui';

  let loading = $state(true);
  let error = $state('');
  let matrix: LabsMatrix | null = $state(null);
  let drafts: HealthPanel[] = $state([]);
  // '' is every specimen; panels with none recorded only show under it.
  let specimen: Specimen | '' = $state('');
  const specimenOptions = [
    { value: '', label: 'All specimens' },
    ...SPECIMENS.map((s) => ({ value: s, label: specimenLabel(s) })),
  ];

  async function onSpecimenChange(v: string) {
    specimen = v as Specimen | '';
    await load();
  }

  let csvInput: HTMLInputElement | undefined = $state(undefined);
  let csvImporting = $state(false);
  let csvSummary: CsvImportSummary | null = $state(null);

  async function onCsvPicked(e: Event) {
    const input = e.target as HTMLInputElement;
    const f = input.files?.[0];
    if (!f) return;
    csvImporting = true;
    csvSummary = null;
    try {
      csvSummary = await importHealthCsv(f);
      await load();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'CSV import failed');
    } finally {
      csvImporting = false;
      if (csvInput) csvInput.value = '';
    }
  }

  function triggerCsvPick() {
    csvInput?.click();
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const [m, panelResp] = await Promise.all([
        getLabsMatrix(specimen || null),
        listHealthPanels({ limit: 200 }),
      ]);
      matrix = m;
      drafts = panelResp.panels.filter((p) => p.draft);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load labs';
    } finally {
      loading = false;
    }
  }

  const DRAWN = { year: 'numeric', month: '2-digit', day: '2-digit' } as const;

  function formatRange(low: number | null, high: number | null): string {
    if (low == null && high == null) return '';
    if (low == null) return `≤ ${high}`;
    if (high == null) return `≥ ${low}`;
    return `${low}–${high}`;
  }

  function cell(
    panelId: number,
    markerName: string,
  ): { value: number; unit: string; flag: string | null } | null {
    const row = matrix?.values[String(panelId)];
    return row?.[markerName] ?? null;
  }

  function flagClass(flag: string | null): string {
    if (!flag) return '';
    return `flag-${flag}`;
  }

  function categoryLabel(c: string): string {
    const labels: Record<string, string> = {
      CBC: 'MORPHOLOGY',
      CMP: 'CHEMISTRY',
      Liver: 'LIVER',
      Lipid: 'LIPID PANEL',
      Thyroid: 'THYROID',
      Iron: 'IRON',
      Vitamins: 'VITAMINS',
      Inflammation: 'INFLAMMATION',
      Hormones: 'HORMONES',
      Diabetes: 'DIABETES',
      Urinalysis: 'URINALYSIS',
      Stool: 'STOOL',
      Other: 'OTHER',
    };
    return labels[c] || c.toUpperCase();
  }

  function encodeMarker(name: string): string {
    return encodeURIComponent(name);
  }

  // Use the canonical name (with underscores → spaces) as the column
  // header — that gives MCHC instead of "Mean Corpuscular Hemoglobin
  // Concentration", which is what we'd write on paper. The full
  // display_name is kept on the link's title attribute as a tooltip.
  // A small abbreviation map handles the cases the canonical name
  // can't shorten on its own.
  const ABBREVIATIONS: Record<string, string> = {
    'Vitamin D': 'Vit D',
    'Vitamin B12': 'Vit B12',
    'Cholesterol HDL Ratio': 'Chol/HDL',
    'Iron Saturation': 'Iron Sat',
  };

  function shortHeader(canonical: string): string {
    const spaced = canonical.replace(/_/g, ' ');
    return ABBREVIATIONS[spaced] || spaced;
  }

  onMount(load);
</script>

{#if !loading && (!error || specimen)}
  <!-- Held back while loading, and on a failed unfiltered load, so the pane
       shows nothing but the centered message. A failed filtered load keeps
       it: the filter is the only way to change what failed. -->
  <PageHeader title="Labs">
    {#snippet actions()}
      <Select
        value={specimen}
        options={specimenOptions}
        onValueChange={onSpecimenChange}
        ariaLabel="Filter by specimen"
        size="md"
        minChars={13}
      />
      <Button onclick={triggerCsvPick} loading={csvImporting} loadingLabel="Importing…"
        >Import CSV</Button
      >
      <input
        bind:this={csvInput}
        type="file"
        accept=".csv,text/csv"
        style="display: none"
        onchange={onCsvPicked}
      />
      <Button href={healthCsvExportUrl()} download="labs.csv">Export CSV</Button>
      <Button variant="primary" href="{base}/health/labs/upload">Upload lab results</Button>
    {/snippet}
  </PageHeader>
{/if}

{#if csvSummary}
  <Alert appearance="inline" variant="info" role="status">
    {#if csvSummary.panels_created > 0}
      Added {csvSummary.panels_created} panel{csvSummary.panels_created === 1 ? '' : 's'}
      ({csvSummary.biomarkers_created} biomarkers).
    {:else}
      Nothing new to add.
    {/if}
    {#if csvSummary.panels_skipped_identical > 0}
      {csvSummary.panels_skipped_identical} already on file —
      {csvSummary.panels_skipped_identical === 1 ? 'it was' : 'they were'} skipped.
    {/if}
    {#if csvSummary.panels_needs_review > 0}
      {csvSummary.panels_needs_review} differ from existing panel{csvSummary.panels_needs_review ===
      1
        ? ''
        : 's'}
      for the same date + lab — saved as drafts for review below.
    {/if}
    {#if csvSummary.warnings.length > 0}
      <Disclosure
        summary="{csvSummary.warnings.length} warning{csvSummary.warnings.length === 1 ? '' : 's'}"
      >
        <ul>
          {#each csvSummary.warnings as w}<li>{w}</li>{/each}
        </ul>
      </Disclosure>
    {/if}
  </Alert>
{/if}

<LoadState {loading} {error} empty={!!matrix && matrix.panels.length === 0 && drafts.length === 0}>
  {#snippet emptyState()}
    {#if specimen}
      <EmptyState>No confirmed {specimen} panels on file.</EmptyState>
    {:else}
      <EmptyState actionLabel="Upload your first lab report" actionHref="{base}/health/labs/upload">
        No lab results on file yet.
      </EmptyState>
    {/if}
  {/snippet}
  {#if matrix}
    {#if drafts.length > 0}
      <section class="drafts">
        <h2 class="micro-label">Pending review</h2>
        <ul>
          {#each drafts as p (p.id)}
            <li>
              <a href="{base}/health/labs/panel?id={p.id}">
                <Badge variant="warn">Draft</Badge>
                <Timestamp value={p.drawn_at} options={DRAWN} title={false} />
                <span class="muted">{p.lab_name || '—'}</span>
                <span class="muted">{p.panel_type || ''}</span>
              </a>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    {#if matrix.panels.length === 0}
      <EmptyState actionLabel="Upload a lab report" actionHref="{base}/health/labs/upload">
        {specimen ? `No confirmed ${specimen} panels yet.` : 'No confirmed panels yet.'}
        {#if drafts.length > 0}
          Review the draft above to add it to your history.
        {/if}
      </EmptyState>
    {:else}
      <section class="spreadsheet">
        <div class="scroll">
          <!-- Markers run down the sticky left axis (one row each); draw
  			     dates run across as scrolling columns. The marker names stay
  			     pinned while you scroll sideways through dates (ISSUE-108). -->
          <table>
            <thead>
              <tr class="dates">
                <th class="sticky-left marker-col">Marker</th>
                <th class="sticky-left ref-col">Range</th>
                {#each matrix.panels as p (p.id)}
                  <th class="date-cell">
                    <a
                      href="{base}/health/labs/panel?id={p.id}"
                      class="date-link"
                      title={[p.lab_name, p.specimen && specimenLabel(p.specimen)]
                        .filter(Boolean)
                        .join(' · ') || undefined}
                    >
                      <Timestamp class="date" value={p.drawn_at} options={DRAWN} title={false} />
                    </a>
                  </th>
                {/each}
              </tr>
            </thead>
            <tbody>
              {#each matrix.categories as cat, ci (cat.name)}
                <tr class="cat-row" class:section-start={ci > 0} data-category={cat.name}>
                  <th class="sticky-left cat-cell" colspan="2">{categoryLabel(cat.name)}</th>
                  <td class="cat-band" colspan={matrix.panels.length}></td>
                </tr>
                {#each cat.markers as mk (mk.name)}
                  <tr data-category={cat.name}>
                    <th class="sticky-left marker-col">
                      <a
                        href="{base}/health/labs/marker?name={encodeMarker(mk.name)}"
                        class="marker-link"
                        title={mk.display_name}
                      >
                        <span class="marker-name">{shortHeader(mk.name)}</span>
                        {#if mk.unit}<span class="marker-unit">{mk.unit}</span>{/if}
                      </a>
                    </th>
                    <th class="sticky-left ref-col ref"
                      >{formatRange(mk.ref_range_low, mk.ref_range_high)}</th
                    >
                    {#each matrix.panels as p (p.id)}
                      {@const c = cell(p.id, mk.name)}
                      <td class="data-cell {flagClass(c?.flag ?? null)}">
                        {#if c}{c.value}{/if}
                      </td>
                    {/each}
                  </tr>
                {/each}
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}
  {/if}
</LoadState>

<style>
  .drafts {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-3) var(--space-4);
    margin-bottom: var(--space-4);
  }
  .drafts h2 {
    margin: 0 0 var(--space-2);
  }
  .drafts ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .drafts a {
    display: grid;
    grid-template-columns: auto 7rem 1fr 1fr;
    gap: var(--space-2);
    align-items: center;
    padding: 0;
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    text-decoration: none;
    font-size: var(--text-sm);
  }
  .drafts a:hover {
    background: var(--hover-bg);
  }
  .spreadsheet {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    overflow: hidden;
  }
  .scroll {
    overflow: auto;
    /* design-lint-allow: lib/viewport.ts writes --app-height and --kb-height at
       runtime; this is body's own height expression, less the page chrome. */
    max-height: calc(var(--app-height, 100dvh) - var(--kb-height, 0px) - 200px);
  }
  table {
    border-collapse: separate;
    border-spacing: 0;
    font-size: var(--text-xs);
    /* Natural (shrink-to-fit) width: with few panels the table stays
		   compact and left-aligned instead of stretching date columns to
		   fill a wide desktop viewport. Horizontal scroll kicks in once the
		   fixed-width date columns overflow the container. */
    width: max-content;
    max-width: 100%;
  }

  /* Date header row — pinned to the top so dates stay visible while you
	   scroll down through the markers. */
  thead th {
    position: sticky;
    top: 0;
    background: var(--surface-raised);
    z-index: 2;
    text-align: center;
    padding: var(--space-1) var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
    font-weight: var(--weight-regular);
    color: var(--text-muted);
    white-space: nowrap;
  }
  /* Fixed-width date / value columns so they don't auto-expand to fill a
	   wide viewport. Same value applied to the header th and every data td so
	   the column width is pinned regardless of row count. */
  .date-cell,
  td.data-cell {
    width: 5.5rem;
    min-width: 5.5rem;
    max-width: 5.5rem;
  }
  .date-link {
    display: inline-flex;
    align-items: center;
    color: var(--text-primary);
    text-decoration: none;
    white-space: nowrap;
  }
  .date-link:hover :global(.date) {
    text-decoration: underline;
  }
  .date-link :global(.date) {
    font-weight: var(--weight-medium);
  }

  /* Category banner row — section divider running across the markers it
	   groups. The label cell is pinned left with the marker columns; the
	   coloured band scrolls with the date columns. The tint itself is the
	   separator: the band carries no top/bottom border, so it doesn't pick
	   up the generic ``tbody td`` bottom line (which the label th doesn't
	   have) and leave a broken half-line under the banner. */
  .cat-cell {
    text-align: left;
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    letter-spacing: var(--tracking-caps);
    color: var(--text-muted);
    text-transform: uppercase;
    padding: var(--space-1) var(--space-2);
    background: hsla(var(--cat-h), var(--cat-s), 65%, 0.22);
  }
  .cat-band {
    padding: 0;
    border-top: 0;
    border-bottom: 0;
    background: hsla(var(--cat-h), var(--cat-s), 65%, 0.22);
  }

  .marker-link {
    /* Name and unit on one line. The name takes the room it needs and
		   ellipsises first if the row is cramped; the unit stays put. */
    display: flex;
    flex-direction: row;
    align-items: baseline;
    gap: var(--space-2);
    color: inherit;
    text-decoration: none;
    width: 100%;
    min-width: 0;
  }
  .marker-link:hover .marker-name {
    text-decoration: underline;
  }
  .marker-name {
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
    /* The fixed column width clips overlong names to an ellipsis (full
		   name lives on the link's title tooltip). Keeping the column a fixed
		   width is what keeps the ref column's sticky offset exact. */
    flex: 0 1 auto;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .marker-unit {
    font-size: var(--text-2xs);
    color: var(--text-dim);
    white-space: nowrap;
    /* Yield before the name: under a space deficit the unit collapses
		   (and ellipsises) first, so the marker name stays intact and only
		   truncates as a last resort once the unit is gone. */
    flex: 0 9999 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ref {
    font-size: var(--text-2xs);
    color: var(--text-dim);
    font-style: italic;
    font-weight: var(--weight-regular);
  }
  tbody td {
    text-align: center;
    padding: var(--space-1) var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
    white-space: nowrap;
    color: var(--text-primary);
    background: hsla(var(--cat-h), var(--cat-s), 65%, 0.08);
  }

  /* Per-category palette. Each section (Thyroid, Lipid, …) carries its
	   own hue: the category banner gets the stronger tint, the marker's
	   data cells a subtler version of the same hue. Hues are stored as CSS
	   vars on the row so both intensity levels share one rule pair. */
  [data-category='CBC'] {
    --cat-h: 0;
    --cat-s: 50%;
  }
  [data-category='CMP'] {
    --cat-h: 210;
    --cat-s: 45%;
  }
  [data-category='Liver'] {
    --cat-h: 30;
    --cat-s: 55%;
  }
  [data-category='Lipid'] {
    --cat-h: 145;
    --cat-s: 40%;
  }
  [data-category='Thyroid'] {
    --cat-h: 280;
    --cat-s: 45%;
  }
  [data-category='Iron'] {
    --cat-h: 18;
    --cat-s: 60%;
  }
  [data-category='Vitamins'] {
    --cat-h: 50;
    --cat-s: 55%;
  }
  [data-category='Inflammation'] {
    --cat-h: 335;
    --cat-s: 50%;
  }
  [data-category='Hormones'] {
    --cat-h: 185;
    --cat-s: 45%;
  }
  [data-category='Diabetes'] {
    --cat-h: 85;
    --cat-s: 45%;
  }
  [data-category='Other'] {
    --cat-h: 220;
    --cat-s: 8%;
  }

  /* Sticky left columns (marker name + reference range) sit above every
	   row's data, fully opaque so scrolling values never bleed through.
	   These are the pinned axis — they stay put while date columns scroll
	   sideways (ISSUE-108). */
  /* Stacking order, low → high:
	     0  body data cells
	     1  body sticky-left columns (marker / range / category label)
	     2  header row (dates) — must sit above the body's frozen column
	     3  header corner cells (pinned on both axes) — above everything
	   Keeping the body column below the header row is what stops the
	   category label and frozen columns from painting over the date
	   header when you scroll down/right. */
  td.sticky-left,
  th.sticky-left {
    position: sticky;
    background: var(--surface-card);
    z-index: 1;
    text-align: left;
    padding: var(--space-1) var(--space-2);
    vertical-align: middle;
  }
  thead th.sticky-left {
    z-index: 3;
  }
  /* Both sticky columns get a fixed width so the ref column's ``left``
	   offset (= marker column width) is always exact — otherwise a long
	   marker name grows the first column and the ref column drifts out of
	   its pinned position on horizontal scroll. */
  .marker-col {
    left: 0;
    width: 9rem;
    min-width: 9rem;
    max-width: 9rem;
    border-right: 1px solid var(--border-default);
  }
  .ref-col {
    left: 9rem;
    width: 5rem;
    min-width: 5rem;
    max-width: 5rem;
    border-right: 1px solid var(--border-default);
  }
  /* The category label cell spans both sticky columns, so it pins at the
	   left edge and carries the right border itself. The tint is composited
	   over an opaque card base (gradient layer over background-color) so the
	   cell is fully opaque — a translucent sticky cell would let the
	   scrolling category band show through and stack to a brighter strip. */
  th.cat-cell.sticky-left {
    left: 0;
    background-color: var(--surface-card);
    background-image: linear-gradient(
      hsla(var(--cat-h), var(--cat-s), 65%, 0.22),
      hsla(var(--cat-h), var(--cat-s), 65%, 0.22)
    );
  }
  tbody td.flag-H {
    background: rgba(204, 102, 102, 0.22);
    color: var(--status-danger-fg);
  }
  tbody td.flag-L {
    background: rgba(122, 163, 216, 0.22);
    color: var(--status-info-fg);
  }
  /* Critical is a step above High, so it keeps a solid saturated fill rather
	   than the tinted cell — but it needs a light-theme value of its own, or the
	   near-black block lands on a white table. */
  tbody td.flag-C {
    background: var(--status-critical-bg);
    color: var(--status-critical-fg);
    font-weight: var(--weight-medium);
  }

  /* Subtle full-row highlight on hover to make scanning a marker's values
	   across dates easier. An inset overlay lightens every cell uniformly
	   over its own background (tinted data, opaque frozen columns, flag
	   cells) without replacing the base colour. Hover-capable devices only,
	   and not on the category banner rows. Mixed from --text-primary so it
	   darkens on the light theme and lightens on the dark one. */
  @media (hover: hover) {
    tbody tr:not(.cat-row):hover td,
    tbody tr:not(.cat-row):hover th {
      box-shadow: inset 0 0 0 9999px color-mix(in srgb, var(--text-primary) 4%, transparent);
    }
  }

  @media (max-width: 768px) {
    .drafts {
      padding: var(--space-3) var(--space-2);
    }
    .drafts a {
      /* Stack the columns; the desktop 4-column grid wraps off-screen. */
      grid-template-columns: auto auto 1fr;
      grid-template-rows: auto auto;
      gap: 0.2rem var(--space-2);
      font-size: var(--text-xs);
    }
    .drafts a > :nth-child(4) {
      grid-column: 1 / -1;
      font-size: var(--text-2xs);
    }
    .spreadsheet {
      border-radius: 0;
      border-left: none;
      border-right: none;
      margin: 0 -0.75rem;
    }
    .scroll {
      /* design-lint-allow: runtime viewport tokens, as above. */
      max-height: calc(var(--app-height, 100dvh) - var(--kb-height, 0px) - 160px);
    }
    /* Marker name stays pinned; the reference column scrolls away
		   horizontally because it's secondary and would eat the narrow
		   viewport. Drop only the *left* stick — the header row must keep its
		   top stick so "Range" stays visible on vertical scroll. */
    td.ref-col,
    th.ref-col {
      left: auto;
      width: 4.5rem;
      min-width: 4.5rem;
      max-width: 4.5rem;
    }
    /* Body range cells aren't pinned at all — they scroll with the row. */
    tbody td.ref-col,
    tbody th.ref-col {
      position: static;
    }
    /* The "Range" header is no longer left-pinned here, so drop it to the
		   date-header level — otherwise it ties with the marker header's
		   corner z-index and paints over it on horizontal scroll. */
    thead th.ref-col {
      z-index: 2;
    }
    /* Header "Range" keeps sticky top (from the base thead rule); left:auto
		   above lets it scroll sideways with the date columns. */
    .marker-col {
      width: 7rem;
      min-width: 7rem;
      max-width: 7rem;
    }
    /* Narrow viewport: stack the unit under the name instead of cramming
		   both on one line. The name fills the column and ellipsises if long. */
    .marker-link {
      flex-direction: column;
      align-items: flex-start;
      gap: 0;
    }
    .marker-name {
      width: 100%;
    }
  }
</style>
