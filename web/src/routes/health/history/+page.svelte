<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    createDiagnosis,
    createEncounter,
    linkDiagnosisEncounter,
    listDiagnoses,
    listEncounters,
    type Diagnosis,
    type Encounter,
  } from '$lib/api';
  import {
    Button,
    Card,
    DateRangeFilter,
    EmptyState,
    Field,
    FormGrid,
    FormStack,
    Select,
    type SelectOption,
    Input,
    TextArea,
    Badge,
    Chip,
    LoadState,
    PageHeader,
  } from '$lib/components/ui';
  import {
    conditionOptionLabel,
    linkableConditionOptions,
    resolveById,
  } from '$lib/health/conditions';
  import { formatDate, localDateISO } from '$lib/dateFormat';
  import { severityVariant } from '$lib/health/status';

  // Suggested types — the server accepts any free-text encounter_type, so
  // these are just defaults for the dropdowns. Unknown types from the API
  // flow through `typeLabel` and a generic badge style.
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
  ] as const;

  let loading = $state(true);
  let error = $state('');
  let encounters: Encounter[] = $state([]);
  // One fetch of every condition; the two panels below are slices of it, and
  // the create form's link picker needs the whole pool (resolved included).
  // `list_diagnoses` already sorts active → chronic → resolved, so filtering
  // preserves the order each panel used to get from its own request.
  let allDiagnoses: Diagnosis[] = $state([]);
  const active = $derived(allDiagnoses.filter((d) => d.status === 'active'));
  const chronic = $derived(allDiagnoses.filter((d) => d.status === 'chronic'));

  let typeFilter = $state('');
  let sinceFilter = $state('');
  let untilFilter = $state('');

  // Quick-add encounter form
  let formOpen = $state(false);
  let formDate = $state(localDateISO());
  let formType = $state('visit');
  let formProvider = $state('');
  let formFacility = $state('');
  let formSpecialty = $state('');
  let formReason = $state('');
  let formNotes = $state('');
  let saving = $state(false);
  let formError = $state('');
  let formWarning = $state('');
  // Conditions staged for linking. The encounter does not exist yet, so these
  // are held until it does and then linked one PUT each.
  let formConditionIds: number[] = $state([]);
  let formConditionPick = $state('');

  const stagedConditions = $derived(resolveById(allDiagnoses, formConditionIds));

  const conditionOptions: SelectOption[] = $derived(
    linkableConditionOptions(allDiagnoses, formConditionIds),
  );

  function stageCondition(v: string) {
    const id = Number(v);
    if (!Number.isFinite(id) || formConditionIds.includes(id)) return;
    formConditionIds = [...formConditionIds, id];
    // The picker is a staging control, not a value: clearing it leaves the
    // placeholder showing so the next pick reads as another addition.
    formConditionPick = '';
  }

  function unstageCondition(id: number) {
    formConditionIds = formConditionIds.filter((x) => x !== id);
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const [encResp, diagResp] = await Promise.all([
        listEncounters({
          type: typeFilter || undefined,
          since: sinceFilter || undefined,
          until: untilFilter || undefined,
          limit: 200,
        }),
        listDiagnoses({ status: 'all', limit: 500 }),
      ]);
      encounters = encResp.encounters;
      allDiagnoses = diagResp.diagnoses;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load history';
    } finally {
      loading = false;
    }
  }

  async function submit(e: Event) {
    e.preventDefault();
    formError = '';
    formWarning = '';
    saving = true;
    try {
      const { id } = await createEncounter({
        encounter_date: formDate,
        encounter_type: formType,
        provider: formProvider || undefined,
        facility: formFacility || undefined,
        specialty: formSpecialty || undefined,
        reason: formReason || undefined,
        notes: formNotes || undefined,
      });

      // There is no server-side "create with diagnoses" call, so linking is one
      // request per condition after the fact. The encounter is already saved by
      // this point, so a link failure must not be reported as a failed save —
      // it is named separately and the form still closes.
      const failed: string[] = [];
      await Promise.all(
        stagedConditions.map(async (d) => {
          try {
            await linkDiagnosisEncounter(d.id, id);
          } catch {
            failed.push(d.name);
          }
        }),
      );
      if (failed.length) {
        formWarning = `Encounter saved, but could not link ${failed.join(', ')}. Link from the encounter page.`;
      }

      formProvider = '';
      formFacility = '';
      formSpecialty = '';
      formReason = '';
      formNotes = '';
      formConditionIds = [];
      formConditionPick = '';
      formOpen = false;
      await load();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  function typeLabel(t: string): string {
    const m: Record<string, string> = {
      visit: 'Visit',
      procedure: 'Procedure',
      screening: 'Screening',
      hospitalization: 'Hospital',
      er: 'ER',
      telehealth: 'Telehealth',
      imaging: 'Imaging',
      dental: 'Dental',
      other: 'Other',
    };
    if (m[t]) return m[t];
    // Unknown free-text type: title-case the first segment for display.
    if (!t) return 'Unknown';
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  function typeBadgeClass(t: string): string {
    return (ENCOUNTER_TYPES as readonly string[]).includes(t) ? `type-${t}` : 'type-other';
  }

  // All types observed in the data plus the canonical list — used so the
  // <select> never silently switches to a value not in its options.
  const allTypes = $derived(
    Array.from(
      new Set<string>([
        ...ENCOUNTER_TYPES,
        ...encounters.map((e) => e.encounter_type).filter(Boolean),
      ]),
    ),
  );

  const formTypeOptions: SelectOption[] = ENCOUNTER_TYPES.map((t) => ({
    value: t,
    label: typeLabel(t),
  }));
  const typeFilterOptions: SelectOption[] = $derived([
    { value: '', label: 'All types' },
    ...allTypes.map((t) => ({ value: t, label: typeLabel(t) })),
  ]);

  onMount(load);
</script>

{#if !loading && !error}
  <!-- Held back while loading so the pane shows nothing but the centered
       loading message, rather than centering it in the space left under
       this header. -->
  <PageHeader title="Medical history">
    {#snippet actions()}
      <Button onclick={() => (formOpen = !formOpen)}>
        {formOpen ? 'Cancel' : '+ Add encounter'}
      </Button>
      <Button href="{base}/health/history/import">Import</Button>
      <Button href="{base}/health/history/diagnoses">Conditions</Button>
    {/snippet}
  </PageHeader>
{/if}

{#if formWarning}
  <!-- Outside the form: it reports on a save that already closed the form. -->
  <div class="banner warn">{formWarning}</div>
{/if}

{#if formOpen}
  <div class="entry-form">
    <Card>
      <FormStack as="form" onsubmit={submit}>
        <FormGrid>
          <Field label="Date">
            <Input type="date" bind:value={formDate} required />
          </Field>
          <Field label="Type">
            <Select
              value={formType}
              options={formTypeOptions}
              onValueChange={(v) => (formType = v)}
              ariaLabel="Type"
              fullWidth
            />
          </Field>
          <Field label="Provider">
            <Input type="text" bind:value={formProvider} placeholder="Dr. Smith" />
          </Field>
          <Field label="Facility">
            <Input type="text" bind:value={formFacility} placeholder="Riverside Clinic" />
          </Field>
          <Field label="Specialty">
            <Input type="text" bind:value={formSpecialty} placeholder="cardiology" />
          </Field>
        </FormGrid>
        <Field label="Reason">
          <Input
            type="text"
            bind:value={formReason}
            placeholder="Chief complaint or reason for visit"
          />
        </Field>
        <Field label="Notes">
          <TextArea bind:value={formNotes} rows={3} placeholder="Findings, follow-ups, …" />
        </Field>

        <div class="conditions-field">
          <span class="field-label caption">Conditions</span>
          {#if stagedConditions.length}
            <ul class="staged">
              {#each stagedConditions as d (d.id)}
                <li>
                  <Chip
                    size="xs"
                    removable
                    removeLabel="Remove {d.name}"
                    onremove={() => unstageCondition(d.id)}>{conditionOptionLabel(d)}</Chip
                  >
                </li>
              {/each}
            </ul>
          {/if}
          <Select
            value={formConditionPick}
            options={conditionOptions}
            onValueChange={stageCondition}
            placeholder={conditionOptions.length
              ? 'Link an existing condition…'
              : 'No conditions to link'}
            disabled={conditionOptions.length === 0}
            ariaLabel="Link an existing condition"
            fullWidth
          />
        </div>

        {#if formError}
          <div class="banner error">{formError}</div>
        {/if}
        <div class="form-actions">
          <Button variant="primary" type="submit" loading={saving}>Save</Button>
        </div>
      </FormStack>
    </Card>
  </div>
{/if}

<div class="filter-bar control-row">
  <Select
    value={typeFilter}
    options={typeFilterOptions}
    onValueChange={(v) => {
      typeFilter = v;
      load();
    }}
    ariaLabel="Type filter"
    widthChars={9}
  />
  <DateRangeFilter bind:from={sinceFilter} bind:to={untilFilter} onChange={load} />
</div>

<LoadState {loading} {error}>
  <div class="layout">
    <section class="timeline">
      {#if encounters.length === 0}
        <EmptyState>
          No encounters yet. Use <strong>+ Add encounter</strong> above to record one.
        </EmptyState>
      {:else}
        <ul>
          {#each encounters as e (e.id)}
            <li>
              <Card href="{base}/health/history/encounter?id={e.id}">
                <div class="card-head">
                  <Badge class="encounter-type {typeBadgeClass(e.encounter_type)}"
                    >{typeLabel(e.encounter_type)}</Badge
                  >
                  <span class="date caption">{formatDate(e.encounter_date)}</span>
                </div>
                <div class="card-body">
                  {#if e.provider || e.facility}
                    <div class="who">
                      {e.provider || ''}{e.provider && e.facility ? ' · ' : ''}{e.facility || ''}
                    </div>
                  {/if}
                  {#if e.specialty}
                    <div class="specialty caption">{e.specialty}</div>
                  {/if}
                  {#if e.reason}
                    <div class="reason muted">{e.reason}</div>
                  {/if}
                </div>
              </Card>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <aside class="sidebar">
      <h2 class="micro-label">Active conditions</h2>
      {#if active.length === 0 && chronic.length === 0}
        <EmptyState size="sm">No active conditions on file.</EmptyState>
      {:else}
        {#if active.length > 0}
          <ul class="conditions">
            {#each active as d (d.id)}
              <li>
                <a href="{base}/health/history/diagnoses">
                  <span class="name">{d.name}</span>
                  {#if d.severity}
                    <Badge size="sm" variant={severityVariant(d.severity)}>{d.severity}</Badge>
                  {/if}
                </a>
              </li>
            {/each}
          </ul>
        {/if}
        {#if chronic.length > 0}
          <h3 class="micro-label">Chronic</h3>
          <ul class="conditions">
            {#each chronic as d (d.id)}
              <li>
                <a href="{base}/health/history/diagnoses">
                  <span class="name">{d.name}</span>
                </a>
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </aside>
  </div>
</LoadState>

<style>
  .entry-form {
    margin-bottom: var(--space-4);
  }
  /* Not a <label>: the control is a Select whose trigger is a button, and
     wrapping it would make clicking the field name open the dropdown. */
  .conditions-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }
  .staged {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  /* A long label wraps inside its chip rather than running past the page
     gutter on a phone. */
  .staged li {
    min-width: 0;
    max-width: 100%;
  }

  /* The money section-tools pattern: a `control-row` so the Select and the
	   date inputs resolve one height and corner rather than each computing its
	   own, `nowrap` so the type filter and the range stay one line, and the
	   only flexible member giving ground. Without the tier the trigger sat a
	   third shorter than the inputs beside it; without nowrap the range broke
	   into a three-line column on a phone. */
  .filter-bar {
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-4);
  }

  /* :global because the subject is DateRangeFilter's root. Placement, not
	   paint: the range is what absorbs a narrow phone, and the Select holds the
	   width its own `widthChars` reserves. */
  .filter-bar :global(.date-range) {
    flex: 1 1 auto;
    min-width: 0;
  }

  .layout {
    display: grid;
    grid-template-columns: 1fr 280px;
    gap: 1.25rem;
  }
  @media (max-width: 768px) {
    .layout {
      grid-template-columns: 1fr;
    }
  }

  .timeline ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .card-head {
    align-items: center;
  }
  .card-body .who {
    font-weight: var(--weight-medium);
    font-size: var(--text-sm);
  }
  .card-body .specialty {
    text-transform: lowercase;
  }
  .card-body .reason {
    margin-top: var(--space-1);
    font-size: var(--text-sm);
  }

  /* The encounter-type palette, tuned for the dark surface: each canonical
	   type gets its own hue via HSLA so they share intensity, handed to Badge
	   through its categorical --badge-bg / --badge-fg. type-other is the
	   catch-all for unknown free-text types. */
  /* design-lint-allow-begin: categorical palette — the hue names the encounter
     type (visit / procedure / imaging / dental), not a severity. Folding these
     onto the status scale would make a routine visit and a screening the same
     color. Each carries an explicit light value because the tinted fills are
     tuned per hue rather than derived from a token pair. */
  .card-head :global(.encounter-type.type-visit) {
    --badge-bg: hsla(210, 45%, 65%, 0.22);
    --badge-fg: #b6ccea;
  }
  .card-head :global(.encounter-type.type-procedure) {
    --badge-bg: hsla(35, 60%, 60%, 0.22);
    --badge-fg: #e6b96b;
  }
  .card-head :global(.encounter-type.type-screening) {
    --badge-bg: hsla(195, 50%, 60%, 0.22);
    --badge-fg: #9cd5ea;
  }
  .card-head :global(.encounter-type.type-hospitalization) {
    --badge-bg: hsla(0, 55%, 60%, 0.25);
    --badge-fg: #f0a09c;
  }
  .card-head :global(.encounter-type.type-er) {
    --badge-bg: hsla(0, 60%, 55%, 0.32);
    --badge-fg: #ff9d96;
  }
  .card-head :global(.encounter-type.type-telehealth) {
    --badge-bg: hsla(145, 40%, 55%, 0.22);
    --badge-fg: #9bd6a6;
  }
  .card-head :global(.encounter-type.type-imaging) {
    --badge-bg: hsla(280, 45%, 65%, 0.22);
    --badge-fg: #d0aeec;
  }
  .card-head :global(.encounter-type.type-dental) {
    --badge-bg: hsla(185, 45%, 60%, 0.22);
    --badge-fg: #95d2dc;
  }
  /* design-lint-allow-end */
  .card-head :global(.encounter-type.type-other) {
    --badge-bg: hsla(220, 8%, 60%, 0.18);
    --badge-fg: var(--text-muted);
  }

  .sidebar {
    border-left: 1px solid var(--border-subtle);
    padding-left: 1.25rem;
  }
  @media (max-width: 768px) {
    .sidebar {
      border-left: none;
      border-top: 1px solid var(--border-subtle);
      padding-left: 0;
      padding-top: var(--space-4);
    }
  }
  .sidebar h2 {
    margin: 0 0 var(--space-2);
  }
  .sidebar h3 {
    margin: var(--space-3) 0 var(--space-2);
  }
  /* Cards, not flat rows. A condition name is routinely long ("Bilateral
	   patellofemoral pain syndrome with anterior knee crepitus") and the old
	   single-line row ellipsised it away rather than wrapping. `auto-fill` with
	   a `min()` floor keeps one column inside the 280px desktop sidebar and
	   goes multi-column once the sidebar becomes full width under 768px —
	   without the floor a name wider than the track would push the grid past
	   the viewport. */
  .conditions {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(200px, 100%), 1fr));
    gap: var(--space-2);
  }
  /* Equal heights only where cards actually sit beside each other. Above the
	   breakpoint this is the 280px sidebar — a single column, where `1fr` rows
	   would stretch every card to match the longest condition name in the list
	   and leave most of them mostly empty. Below it the sidebar is full width
	   and goes multi-column, where a ragged bottom edge across a row reads as
	   damage. */
  @media (max-width: 768px) {
    .conditions {
      grid-auto-rows: 1fr;
    }
  }
  .conditions li {
    min-width: 0;
    /* The grid stretches the <li>, but the <a> inside it is height:auto and
		   would sit short inside a stretched cell — so the card would still
		   render at its own content height. Make the li a flex parent and let
		   the anchor fill it. */
    display: flex;
  }
  .conditions li a {
    flex: 1;
    box-sizing: border-box;
    /* Name on its own line with the severity chip under it. Side by side, a
		   long name and a chip competed for one 200px track, and the chip's
		   width varied per card ("mild" vs "moderate") so no two names got the
		   same wrap point. */
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    text-decoration: none;
    color: var(--text-primary);
    font-size: var(--text-sm);
  }
  .conditions li a:hover {
    border-color: var(--border-hover);
  }
  .conditions .name {
    min-width: 0;
    line-height: var(--leading-snug);
    overflow-wrap: anywhere;
  }

  /* design-lint-allow-begin: light half of the categorical encounter-type
     palette above. */
  :global(:root[data-theme='light']) .card-head :global(.encounter-type.type-visit) {
    --badge-fg: #2563b0;
  }
  :global(:root[data-theme='light']) .card-head :global(.encounter-type.type-procedure) {
    --badge-fg: #946a00;
  }
  :global(:root[data-theme='light']) .card-head :global(.encounter-type.type-screening) {
    --badge-fg: #2563b0;
  }
  :global(:root[data-theme='light']) .card-head :global(.encounter-type.type-hospitalization) {
    --badge-fg: #c0271d;
  }
  :global(:root[data-theme='light']) .card-head :global(.encounter-type.type-er) {
    --badge-fg: #c0271d;
  }
  :global(:root[data-theme='light']) .card-head :global(.encounter-type.type-telehealth) {
    --badge-fg: #15803d;
  }
  :global(:root[data-theme='light']) .card-head :global(.encounter-type.type-imaging) {
    --badge-fg: #7c3aed;
  }
  :global(:root[data-theme='light']) .card-head :global(.encounter-type.type-dental) {
    --badge-fg: #0d8f7e;
  }
  /* design-lint-allow-end */
</style>
