<script lang="ts">
  import { onMount } from 'svelte';
  import { SettingsCard } from '$lib/components/settings';
  import { Badge, Button, ConfirmDialog, Input, KebabMenu, Select } from '$lib/components/ui';
  import { notifyError, notifyInfo, notifySuccess } from '$lib/stores/notices';
  import {
    ApiError,
    autoClassifyPortfolio,
    deletePortfolioClassification,
    getPortfolioClassifications,
    putPortfolioClassification,
    type PortfolioClassification,
  } from '$lib/money/api';
  import {
    assetClassOptions,
    subClassOptions,
    geographyOptions,
  } from '$lib/money/portfolioOptions';

  // Symbol → asset class / sub-class / geography. Resolved at read time, so an
  // edit here retroactively reclassifies every snapshot. Class, sub-class and
  // geography are picked from the canonical vocabulary (unioned with values
  // already in use — the columns stay free text in the DB); only the symbol is
  // typed. Add and the inline edit are per-record forms, so they keep their
  // own buttons (the app-bar Save is for page-level state).

  let classifications: PortfolioClassification[] = $state([]);
  let loaded = $state(false);
  let loadError = $state('');

  // Add form
  let addSymbol = $state('');
  let addClass = $state('');
  let addSub = $state('');
  let addGeo = $state('');
  let addBusy = $state(false);

  // Inline edit
  let editingSymbol = $state('');
  let editClass = $state('');
  let editSub = $state('');
  let editGeo = $state('');
  let editBusy = $state(false);

  let confirmDelete: string | null = $state(null);
  let autoBusy = $state(false);

  const classOptions = $derived(
    toOptions(assetClassOptions(classifications.map((c) => c.asset_class))),
  );
  const geoOptions = $derived(
    withNone(toOptions(geographyOptions(classifications.map((c) => c.geography)))),
  );
  const addSubOptions = $derived(withNone(toOptions(subClassOptions(addClass, classifications))));
  const editSubOptions = $derived(withNone(toOptions(subClassOptions(editClass, classifications))));

  function toOptions(values: string[]) {
    return values.map((v) => ({ value: v, label: v }));
  }

  function withNone(options: { value: string; label: string }[]) {
    return [{ value: '', label: '—' }, ...options];
  }

  async function load() {
    try {
      const resp = await getPortfolioClassifications();
      classifications = resp.classifications;
      loadError = '';
    } catch (e) {
      loadError = e instanceof Error ? e.message : 'Failed to load classifications';
    } finally {
      loaded = true;
    }
  }

  onMount(load);

  async function add() {
    if (!addSymbol.trim() || !addClass) return;
    addBusy = true;
    try {
      await putPortfolioClassification(addSymbol.trim(), {
        asset_class: addClass,
        sub_class: addSub,
        geography: addGeo,
      });
      notifySuccess(`Classified ${addSymbol.trim().toUpperCase()}`, {
        key: 'portfolio:classification',
      });
      addSymbol = '';
      addClass = '';
      addSub = '';
      addGeo = '';
      await load();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Save failed', {
        key: 'portfolio:classification',
      });
    } finally {
      addBusy = false;
    }
  }

  function startEdit(cls: PortfolioClassification) {
    editingSymbol = cls.symbol;
    editClass = cls.asset_class;
    editSub = cls.sub_class;
    editGeo = cls.geography;
  }

  // A sub-class belongs to its asset class, so picking a new class drops a
  // sub-class that no longer applies.
  function onClassPicked(next: string, scope: 'add' | 'edit') {
    const sub = scope === 'add' ? addSub : editSub;
    if (sub && !subClassOptions(next, classifications).includes(sub)) {
      if (scope === 'add') addSub = '';
      else editSub = '';
    }
  }

  async function saveEdit() {
    if (!editClass) return;
    editBusy = true;
    try {
      await putPortfolioClassification(editingSymbol, {
        asset_class: editClass,
        sub_class: editSub,
        geography: editGeo,
      });
      notifySuccess(`Saved ${editingSymbol}`, { key: 'portfolio:classification' });
      editingSymbol = '';
      await load();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Save failed', {
        key: 'portfolio:classification',
      });
    } finally {
      editBusy = false;
    }
  }

  async function handleDelete() {
    const symbol = confirmDelete;
    confirmDelete = null;
    if (!symbol) return;
    try {
      await deletePortfolioClassification(symbol);
      notifySuccess(`Removed ${symbol} — fallback rules apply again`, {
        key: 'portfolio:classification',
      });
      await load();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Delete failed', {
        key: 'portfolio:classification',
      });
    }
  }

  // Ticker metadata lookup + description heuristics, server-side; fills in
  // every imported symbol still resolving to Unclassified and never touches
  // an existing row. Rows it writes carry the "auto" badge.
  async function autoClassify() {
    autoBusy = true;
    try {
      const result = await autoClassifyPortfolio();
      if (result.classified.length > 0) {
        notifySuccess(`Auto-classified ${result.classified.map((c) => c.symbol).join(', ')}`, {
          key: 'portfolio:classification',
        });
      } else if (result.unresolved.length === 0) {
        // Only "nothing to do" when there is genuinely nothing left —
        // otherwise it contradicts the "could not classify" notice below.
        notifyInfo('Nothing to classify — every symbol already resolves', {
          key: 'portfolio:classification',
        });
      }
      if (result.unresolved.length > 0) {
        const why =
          result.lookups_available === false
            ? ' (ticker lookup unavailable — heuristics only)'
            : '';
        notifyInfo(`Could not classify: ${result.unresolved.join(', ')}${why}`, {
          key: 'portfolio:unclassified',
        });
      }
      await load();
    } catch (e) {
      // A run already in flight is an expected outcome of the per-user
      // lock, not a failure — reporting it in red would be wrong.
      if (e instanceof ApiError && e.status === 409) {
        notifyInfo('Auto-classification is already running', {
          key: 'portfolio:classification',
        });
      } else {
        notifyError(e instanceof Error ? e.message : 'Auto-classify failed', {
          key: 'portfolio:classification',
        });
      }
    } finally {
      autoBusy = false;
    }
  }

  function detailOf(cls: PortfolioClassification): string {
    return [cls.sub_class, cls.geography].filter(Boolean).join(' · ');
  }

  function menu(cls: PortfolioClassification) {
    return [
      { label: 'Edit', onSelect: () => startEdit(cls) },
      { label: 'Delete', danger: true, onSelect: () => (confirmDelete = cls.symbol) },
    ];
  }
</script>

<SettingsCard title="Symbol classifications ({classifications.length})">
  {#snippet actions()}
    <Button
      variant="pill"
      size="sm"
      disabled={autoBusy}
      loading={autoBusy}
      loadingLabel="Classifying…"
      onclick={autoClassify}
    >
      Auto-classify
    </Button>
  {/snippet}
  {#if loadError}
    <div class="banner error">{loadError}</div>
  {:else}
    <p class="card-hint">
      New symbols classify themselves on import (ticker lookup, marked <em>auto</em>); edit a row to
      override — your edit always wins, and an automatic guess never replaces one. Auto-classify
      fills in anything still unclassified. Classifications reclassify all history at read time.
    </p>

    <div class="cls-form add-form control-row">
      <label class="ctl ctl-symbol">
        <span class="micro-label">Symbol</span>
        <Input bind:value={addSymbol} placeholder="VTI" monospace aria-label="Symbol" />
      </label>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Asset class</span>
        <Select
          bind:value={addClass}
          options={classOptions}
          placeholder="Pick…"
          fullWidth
          ariaLabel="Asset class"
          onValueChange={(v) => onClassPicked(v, 'add')}
        />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Sub-class</span>
        <Select
          bind:value={addSub}
          options={addSubOptions}
          placeholder="—"
          fullWidth
          ariaLabel="Sub-class"
          disabled={!addClass}
        />
      </div>
      <div class="ctl">
        <span class="micro-label" aria-hidden="true">Geography</span>
        <Select
          bind:value={addGeo}
          options={geoOptions}
          placeholder="—"
          fullWidth
          ariaLabel="Geography"
        />
      </div>
      <div class="ctl-action">
        <Button
          variant="primary"
          size="sm"
          disabled={!addSymbol.trim() || !addClass || addBusy}
          loading={addBusy}
          loadingLabel="Adding…"
          onclick={add}
        >
          Add
        </Button>
      </div>
    </div>

    {#if loaded && classifications.length === 0}
      <p class="empty">No classifications yet.</p>
    {:else}
      <ul class="cls-list">
        {#each classifications as cls (cls.symbol)}
          <li class="cls-row">
            {#if editingSymbol === cls.symbol}
              <div class="cls-form control-row">
                <div class="ctl ctl-symbol">
                  <span class="micro-label">Symbol</span>
                  <code class="cls-symbol">{cls.symbol}</code>
                </div>
                <div class="ctl">
                  <span class="micro-label" aria-hidden="true">Asset class</span>
                  <Select
                    bind:value={editClass}
                    options={classOptions}
                    placeholder="Pick…"
                    fullWidth
                    ariaLabel="Asset class"
                    onValueChange={(v) => onClassPicked(v, 'edit')}
                  />
                </div>
                <div class="ctl">
                  <span class="micro-label" aria-hidden="true">Sub-class</span>
                  <Select
                    bind:value={editSub}
                    options={editSubOptions}
                    placeholder="—"
                    fullWidth
                    ariaLabel="Sub-class"
                    disabled={!editClass}
                  />
                </div>
                <div class="ctl">
                  <span class="micro-label" aria-hidden="true">Geography</span>
                  <Select
                    bind:value={editGeo}
                    options={geoOptions}
                    placeholder="—"
                    fullWidth
                    ariaLabel="Geography"
                  />
                </div>
                <div class="ctl-action">
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={!editClass || editBusy}
                    loading={editBusy}
                    onclick={saveEdit}
                  >
                    Save
                  </Button>
                  <Button variant="ghost" size="sm" onclick={() => (editingSymbol = '')}>
                    Cancel
                  </Button>
                </div>
              </div>
            {:else}
              <div class="cls-line">
                <code class="cls-symbol">{cls.symbol}</code>
                <!-- Written without template whitespace so the line reads
                     exactly "Class · Sub · Geo" with no stray gaps. -->
                <span class="cls-desc"
                  >{cls.asset_class}{#if detailOf(cls)}<span class="muted"
                      >{' · ' + detailOf(cls)}</span
                    >{/if}</span
                >
                {#if cls.source === 'auto'}
                  <Badge variant="info">auto</Badge>
                {/if}
                <KebabMenu items={menu(cls)} ariaLabel="Actions for {cls.symbol}" />
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</SettingsCard>

<ConfirmDialog
  open={confirmDelete !== null}
  title="Delete classification"
  message="Are you sure you want to remove the classification for {confirmDelete}? Its holdings fall back to the built-in rules (usually Unclassified) across all history."
  confirmLabel="Delete"
  confirmVariant="danger"
  onConfirm={handleDelete}
  onCancel={() => (confirmDelete = null)}
/>

<style>
  .card-hint {
    margin: 0 0 var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .cls-form {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-2) var(--space-3);
  }

  .add-form {
    margin-bottom: var(--space-3);
  }

  .ctl {
    display: grid;
    gap: var(--space-1);
    flex: 1 1 9rem;
    min-width: 0;
  }

  .ctl-symbol {
    flex: 0 1 7rem;
  }

  .ctl-action {
    display: flex;
    align-items: center;
    gap: var(--space-1);
  }

  .cls-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .cls-row {
    padding: var(--space-2) 0;
  }

  .cls-row + .cls-row {
    border-top: 1px solid var(--border-subtle);
  }

  .cls-line {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .cls-symbol {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-primary);
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    min-width: 3rem;
    text-align: center;
  }

  /* In the edit form the symbol chip sits where a control would, so it
     centres on the control line instead of hanging from the label. */
  .cls-form .cls-symbol {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: var(--control-height-md);
  }

  .cls-desc {
    flex: 1 1 auto;
    min-width: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }

  .muted {
    color: var(--text-muted);
  }
</style>
