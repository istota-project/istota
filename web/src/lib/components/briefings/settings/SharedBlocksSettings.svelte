<script lang="ts">
  import { X } from '@lucide/svelte';
  import { createConfirm } from '$lib/ui/confirm.svelte';
  import {
    getSharedBlocks,
    putSharedBlock,
    deleteSharedBlock,
    runSharedBlock,
    type SharedBlock,
  } from '$lib/api';
  import {
    Button,
    ConfirmDialog,
    EmptyState,
    IconButton,
    KebabMenu,
    Modal,
    Select,
    type KebabItem,
    type SelectOption,
    Card,
    Field,
    Checkbox,
    Input,
    TextArea,
    Badge,
    Timestamp,
  } from '$lib/components/ui';
  import SourceConfigFields from '../SourceConfigFields.svelte';
  import { sourceKindLabel, uniqueName } from './briefingSettings';

  let { browseOptions }: { browseOptions: SelectOption[] } = $props();

  // --- Admin: shared blocks (generated once globally, read by any user) ---
  type SharedDraftSource = { kind: string; config: Record<string, unknown> };
  type SharedDraft = {
    name: string;
    cron: string;
    title: string;
    directive: string;
    render_mode: string;
    trusted: boolean;
    enabled: boolean;
    sources: SharedDraftSource[];
  };
  function emptySharedDraft(): SharedDraft {
    return {
      name: '',
      cron: '0 6 * * *',
      title: '',
      directive: '',
      render_mode: 'synthesis',
      trusted: false,
      enabled: true,
      sources: [],
    };
  }
  let sharedBlocks = $state<SharedBlock[]>([]);
  let sharedAllowedKinds = $state<string[]>(['browse', 'markets', 'email']);
  let sharedBlockTimezone = $state<string>('UTC');
  let sharedDraft = $state<SharedDraft | null>(null);
  let sharedEditingName = $state<string | null>(null); // non-null = editing existing
  let sharedError = $state('');
  let sharedSaving = $state(false);
  let sharedRunning = $state<Set<string>>(new Set());
  const confirmSharedDelete = createConfirm<string>();

  const sharedKindOptions: SelectOption[] = $derived(
    sharedAllowedKinds.map((k) => ({ value: k, label: sourceKindLabel(k) })),
  );

  async function reloadSharedBlocks() {
    const resp = await getSharedBlocks();
    sharedBlocks = resp.shared_blocks;
    if (resp.allowed_source_kinds?.length) sharedAllowedKinds = resp.allowed_source_kinds;
    if (resp.shared_block_timezone) sharedBlockTimezone = resp.shared_block_timezone;
  }

  // Compact "last run": a short local "Mon D, HH:MM".
  const LAST_RUN = { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' } as const;

  function startNewShared() {
    sharedEditingName = null;
    sharedError = '';
    sharedDraft = emptySharedDraft();
  }

  function editShared(b: SharedBlock) {
    sharedEditingName = b.name;
    sharedError = '';
    sharedDraft = {
      name: b.name,
      cron: b.cron,
      title: b.title,
      directive: b.directive ?? '',
      render_mode: b.render_mode,
      trusted: b.trusted,
      enabled: b.enabled,
      sources: (b.sources ?? []).map((s) => ({
        kind: s.kind,
        config: { ...(s.config ?? {}) },
      })),
    };
  }

  // Opens the editor pre-filled with a copy under a free name rather than
  // writing immediately — the shared-block PUT is an upsert keyed on name, and
  // a definition is worth reviewing before it starts generating on a cron.
  function cloneShared(b: SharedBlock) {
    editShared(b);
    sharedEditingName = null;
    if (sharedDraft) {
      sharedDraft.name = uniqueName(
        b.name,
        sharedBlocks.map((x) => x.name),
      );
      sharedDraft.enabled = false;
    }
  }

  function sharedMenu(b: SharedBlock): KebabItem[] {
    return [
      {
        label: sharedRunning.has(b.name) ? 'Running…' : 'Run now',
        disabled: sharedRunning.has(b.name),
        onSelect: () => void doRunShared(b.name),
      },
      { label: 'Edit', onSelect: () => editShared(b) },
      { label: 'Duplicate', onSelect: () => cloneShared(b) },
      { label: 'Delete', danger: true, onSelect: () => confirmSharedDelete.ask(b.name) },
    ];
  }

  function cancelShared() {
    sharedDraft = null;
    sharedEditingName = null;
    sharedError = '';
  }

  function addSharedSource() {
    if (!sharedDraft) return;
    sharedDraft = {
      ...sharedDraft,
      sources: [...sharedDraft.sources, { kind: sharedAllowedKinds[0] ?? 'browse', config: {} }],
    };
  }

  function removeSharedSource(idx: number) {
    if (!sharedDraft) return;
    sharedDraft = {
      ...sharedDraft,
      sources: sharedDraft.sources.filter((_, i) => i !== idx),
    };
  }

  function setSharedSourceKind(idx: number, kind: string) {
    if (!sharedDraft) return;
    // A kind change invalidates the prior kind's config shape — reset it.
    sharedDraft.sources[idx] = { kind, config: {} };
  }

  function patchSharedSourceConfig(idx: number, patch: Record<string, unknown>) {
    if (!sharedDraft) return;
    const next = { ...sharedDraft.sources[idx].config, ...patch };
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined || v === null) delete next[k];
    }
    sharedDraft.sources[idx] = { ...sharedDraft.sources[idx], config: next };
  }

  async function saveShared() {
    if (!sharedDraft) return;
    sharedError = '';
    const name = sharedDraft.name.trim();
    if (!name) {
      sharedError = 'Name is required.';
      return;
    }
    const sources = sharedDraft.sources.map((s) => ({ kind: s.kind, config: s.config }));
    sharedSaving = true;
    try {
      await putSharedBlock({
        name,
        cron: sharedDraft.cron.trim(),
        title: sharedDraft.title.trim(),
        directive: sharedDraft.directive.trim() || null,
        render_mode: sharedDraft.render_mode,
        trusted: sharedDraft.trusted,
        enabled: sharedDraft.enabled,
        sources,
      });
      await reloadSharedBlocks();
      sharedDraft = null;
      sharedEditingName = null;
    } catch (e) {
      sharedError = (e as Error).message || 'Save failed';
    } finally {
      sharedSaving = false;
    }
  }

  // The daemon generates the block on its next tick (ISSUE-700). A gather
  // can take a minute and the model call has a three-minute ceiling, so the
  // wait is generous, and running out of it is reported as not yet answered.
  const SHARED_RUN_POLL_MS = 1500;
  const SHARED_RUN_WAIT_MS = 300_000;
  const SHARED_RUN_OUTCOMES: Record<string, string> = {
    kept_prior: 'Nothing was generated; the previous content was kept.',
    brain_unavailable: 'The primary model is unavailable; the previous content was kept.',
    failed: 'Run failed; see the daemon log.',
    not_found: 'The block was deleted before it ran.',
  };

  async function doRunShared(name: string) {
    sharedRunning = new Set(sharedRunning).add(name);
    try {
      const asked = await runSharedBlock(name);
      const until = Date.now() + SHARED_RUN_WAIT_MS;
      while (Date.now() < until) {
        await new Promise((resolve) => setTimeout(resolve, SHARED_RUN_POLL_MS));
        await reloadSharedBlocks();
        const block = sharedBlocks.find((b) => b.name === name);
        if (!block) {
          sharedError = `${name}: ${SHARED_RUN_OUTCOMES.not_found}`;
          return;
        }
        const answer = block.status.run_answered;
        if (answer?.id === asked.id) {
          const message = SHARED_RUN_OUTCOMES[answer.outcome];
          if (message) sharedError = `${name}: ${message}`;
          return;
        }
      }
      sharedError =
        `${name}: the daemon has not answered yet. It may still be generating; ` +
        'if this persists, check that the daemon is running.';
    } catch (e) {
      sharedError = (e as Error).message || 'Run failed';
    } finally {
      const next = new Set(sharedRunning);
      next.delete(name);
      sharedRunning = next;
    }
  }

  async function doDeleteShared(name: string) {
    try {
      await deleteSharedBlock(name);
      await reloadSharedBlocks();
    } catch (e) {
      sharedError = (e as Error).message || 'Delete failed';
    } finally {
      confirmSharedDelete.cancel();
    }
  }

  const RENDER_OPTIONS: SelectOption[] = [
    { value: 'synthesis', label: 'Synthesis (summarize)' },
    { value: 'structured', label: 'Structured (verbatim)' },
  ];

  $effect(() => {
    reloadSharedBlocks().catch(() => (sharedBlocks = []));
  });
</script>

<Card
  title="Shared blocks ({sharedBlocks.length})"
  description="Structured blocks are stored verbatim with no model call. Only user-agnostic sources are allowed."
>
  {#snippet actions()}
    <Button size="sm" onclick={startNewShared}>New shared block</Button>
  {/snippet}
  {#if sharedError}
    <div class="banner error">{sharedError}</div>
  {/if}

  {#if sharedBlocks.length === 0}
    <EmptyState>No shared blocks yet.</EmptyState>
  {:else}
    <div class="table-scroll">
      <table class="table sb-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Cron ({sharedBlockTimezone})</th>
            <th class="col-render">Render</th>
            <th>Trust</th>
            <th>Last run</th>
            <th class="col-actions"></th>
          </tr>
        </thead>
        <tbody>
          {#each sharedBlocks as b (b.name)}
            <tr>
              <td class="sb-name">
                <strong>{b.name}</strong>
                {#if !b.enabled}<span class="caption"> (disabled)</span>{/if}
                {#if b.status.has_content}
                  <div class="caption sb-preview" title={b.status.value_preview ?? ''}>
                    {(b.status.value_preview ?? '').slice(0, 60)}
                  </div>
                {/if}
              </td>
              <td class="nowrap"><code>{b.cron}</code></td>
              <td class="col-render"
                ><Badge tone="subtle" uppercase={false}>{b.render_mode}</Badge></td
              >
              <td>{b.trusted ? 'trusted' : 'untrusted'}</td>
              <td class="nowrap"
                ><Timestamp
                  value={b.status.last_run_at}
                  format="datetime"
                  options={LAST_RUN}
                  empty="—"
                /></td
              >
              <td class="col-actions">
                <KebabMenu items={sharedMenu(b)} ariaLabel="Shared block actions" />
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</Card>

{#if sharedDraft}
  <Modal
    open={true}
    title={sharedEditingName ? `Edit ${sharedEditingName}` : 'New shared block'}
    onOpenChange={(open) => {
      if (!open) cancelShared();
    }}
    width="40rem"
  >
    <div class="vstack">
      {#if sharedError}<div class="banner error">{sharedError}</div>{/if}
      <div class="sb-grid">
        <Field label="Name" hint="Slug: lowercase, digits, - and _">
          <Input
            type="text"
            placeholder="world-headlines"
            bind:value={sharedDraft.name}
            disabled={!!sharedEditingName}
          />
        </Field>
        <Field label={`Cron (${sharedBlockTimezone})`}>
          <Input type="text" placeholder="0 6 * * *" bind:value={sharedDraft.cron} />
        </Field>
      </div>
      <Field label="Title">
        <Input type="text" placeholder="🌍 World headlines" bind:value={sharedDraft.title} />
      </Field>
      <Field label="Render mode">
        <Select
          value={sharedDraft.render_mode}
          options={RENDER_OPTIONS}
          onValueChange={(v) => sharedDraft && (sharedDraft.render_mode = v)}
          ariaLabel="Render mode"
          fullWidth
        />
      </Field>
      {#if sharedDraft.render_mode === 'synthesis'}
        <Field label="Directive" hint="How to synthesize the sources.">
          <TextArea rows={2} bind:value={sharedDraft.directive} />
        </Field>
      {/if}
      <div class="sb-grid">
        <Field label="Trusted" checkbox hint="Only for injection-safe content.">
          <Checkbox bind:checked={sharedDraft.trusted} />
        </Field>
        <Field label="Enabled" checkbox>
          <Checkbox bind:checked={sharedDraft.enabled} />
        </Field>
      </div>

      <div class="vstack" style:--vstack-gap="var(--space-2)">
        <div class="shared-sources-head">
          <span class="field-label muted">Sources</span>
          <Button variant="secondary" size="sm" onclick={addSharedSource}>+ Add source</Button>
        </div>
        {#if sharedDraft.sources.length === 0}
          <p class="caption">No sources — add at least one browse/markets/email source.</p>
        {/if}
        {#each sharedDraft.sources as src, i (i)}
          <div class="sb-source">
            <span class="sb-source-remove">
              <IconButton
                label="Remove source {i + 1}"
                title="Remove source"
                size="sm"
                danger
                onclick={() => removeSharedSource(i)}
                icon={X}
              />
            </span>
            <Field label="Type">
              <Select
                value={src.kind}
                options={sharedKindOptions}
                onValueChange={(v) => setSharedSourceKind(i, v)}
                ariaLabel="Source kind"
                fullWidth
              />
            </Field>
            <SourceConfigFields
              kind={src.kind}
              config={src.config}
              onChange={(patch) => patchSharedSourceConfig(i, patch)}
              {browseOptions}
            />
          </div>
        {/each}
      </div>
    </div>
    {#snippet footer()}
      <Button variant="secondary" onclick={cancelShared}>Cancel</Button>
      <Button variant="primary" onclick={saveShared} loading={sharedSaving}
        >Save shared block</Button
      >
    {/snippet}
  </Modal>
{/if}

{#if confirmSharedDelete.target}
  <ConfirmDialog
    bind:open={confirmSharedDelete.open}
    title="Delete shared block"
    confirmLabel="Delete"
    onConfirm={() => confirmSharedDelete.target && doDeleteShared(confirmSharedDelete.target)}
  >
    {#snippet body()}
      <p>
        Are you sure you want to delete the shared block <strong
          >{confirmSharedDelete.target}</strong
        >? The last generated value stays until it goes stale. Users referencing it lose the section
        once it expires.
      </p>
    {/snippet}
  </ConfirmDialog>
{/if}

<style>
  .col-render {
    width: 7rem;
  }

  /* Admin shared-blocks editor */
  .nowrap {
    white-space: nowrap;
  }
  /* A natural min-width, so .table-scroll scrolls this table's six content
	   columns horizontally on a phone instead of overlapping them. The floor
	   used to be 44rem to fit three text action buttons; the actions are a
	   kebab now, so only the content columns set it. */
  .sb-table {
    min-width: 34rem;
  }
  .sb-name {
    min-width: 9rem;
  }
  .sb-preview {
    max-width: 16rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Two-column field row that top-aligns, so a field with a hint doesn't push
	   its sibling's input out of line (unlike .inline-fields' flex-end). */
  .sb-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-2) var(--space-4);
    align-items: start;
  }
  .shared-sources-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .field-label {
    font-size: var(--text-sm);
  }
  /* Type + the source's config fields flow inline by default, wrapping on
	   narrow widths. The remove button pins to the top-right corner. */
  .sb-source {
    position: relative;
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-2) var(--space-4);
    padding: var(--space-3) var(--space-8) var(--space-3) var(--space-3);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--surface-base);
  }
  .sb-source :global(.field) {
    flex: 1 1 12rem;
    min-width: 10rem;
  }
  /* Full-width notes / helper text (e.g. the markets "leave blank" hint) break
	   the inline row rather than squeezing between the selects. */
  .sb-source :global(p) {
    flex-basis: 100%;
    margin: 0;
  }
  /* A positioning wrapper, so the IconButton inside keeps its own padding and
     hit area rather than having them overwritten to place it. */
  .sb-source-remove {
    position: absolute;
    top: 0.35rem;
    right: 0.4rem;
    font-size: var(--text-xl);
    line-height: var(--leading-none);
  }
  @container settings (max-width: 560px) {
    .sb-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
