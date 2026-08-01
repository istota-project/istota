<script lang="ts">
  import { onMount } from 'svelte';
  import { SettingsCard } from '$lib/components/settings';
  import { Button, ConfirmDialog, Input, KebabMenu } from '$lib/components/ui';
  import { notifyError, notifySuccess } from '$lib/stores/notices';
  import {
    deletePortfolioClassification,
    getPortfolioClassifications,
    putPortfolioClassification,
    type PortfolioClassification,
  } from '$lib/money/api';

  // Symbol → asset class / sub-class / geography. Resolved at read time, so an
  // edit here retroactively reclassifies every snapshot. Per-record forms keep
  // their own buttons (app-bar Save is for page-level state).

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
    if (!addSymbol.trim() || !addClass.trim()) return;
    addBusy = true;
    try {
      await putPortfolioClassification(addSymbol.trim(), {
        asset_class: addClass.trim(),
        sub_class: addSub.trim(),
        geography: addGeo.trim(),
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

  async function saveEdit() {
    if (!editClass.trim()) return;
    editBusy = true;
    try {
      await putPortfolioClassification(editingSymbol, {
        asset_class: editClass.trim(),
        sub_class: editSub.trim(),
        geography: editGeo.trim(),
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

  function menu(cls: PortfolioClassification) {
    return [
      { label: 'Edit', onSelect: () => startEdit(cls) },
      { label: 'Delete', danger: true, onSelect: () => (confirmDelete = cls.symbol) },
    ];
  }
</script>

<SettingsCard title="Symbol classifications ({classifications.length})">
  {#if loadError}
    <div class="banner error">{loadError}</div>
  {:else}
    <p class="card-hint">
      Classifications drive the allocation charts and reclassify all history at read time.
      Unclassified symbols fall back to the built-in cash and options rules.
    </p>

    <div class="add-row control-row">
      <Input bind:value={addSymbol} placeholder="Symbol" monospace aria-label="Symbol" />
      <Input bind:value={addClass} placeholder="Asset class" aria-label="Asset class" />
      <Input bind:value={addSub} placeholder="Sub-class (optional)" aria-label="Sub-class" />
      <Input bind:value={addGeo} placeholder="Geography (optional)" aria-label="Geography" />
      <Button
        variant="primary"
        size="sm"
        disabled={!addSymbol.trim() || !addClass.trim() || addBusy}
        loading={addBusy}
        loadingLabel="Adding…"
        onclick={add}
      >
        Add
      </Button>
    </div>

    {#if loaded && classifications.length === 0}
      <p class="empty">No classifications yet.</p>
    {:else}
      <div class="table-scroll">
        <table class="grid grid--dense">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Asset class</th>
              <th>Sub-class</th>
              <th>Geography</th>
              <th class="actions" aria-label="Actions"></th>
            </tr>
          </thead>
          <tbody>
            {#each classifications as cls (cls.symbol)}
              {#if editingSymbol === cls.symbol}
                <tr>
                  <td><code>{cls.symbol}</code></td>
                  <td><Input bind:value={editClass} aria-label="Asset class" /></td>
                  <td><Input bind:value={editSub} aria-label="Sub-class" /></td>
                  <td><Input bind:value={editGeo} aria-label="Geography" /></td>
                  <td class="actions">
                    <span class="edit-actions">
                      <Button
                        variant="primary"
                        size="sm"
                        disabled={!editClass.trim() || editBusy}
                        loading={editBusy}
                        onclick={saveEdit}
                      >
                        Save
                      </Button>
                      <Button variant="ghost" size="sm" onclick={() => (editingSymbol = '')}>
                        Cancel
                      </Button>
                    </span>
                  </td>
                </tr>
              {:else}
                <tr>
                  <td><code>{cls.symbol}</code></td>
                  <td>{cls.asset_class}</td>
                  <td class="muted">{cls.sub_class || '—'}</td>
                  <td class="muted">{cls.geography || '—'}</td>
                  <td class="actions">
                    <KebabMenu items={menu(cls)} ariaLabel="Classification actions" />
                  </td>
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
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

  .add-row {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
    margin-bottom: var(--space-3);
  }

  .edit-actions {
    display: inline-flex;
    gap: var(--space-1);
  }

  .muted {
    color: var(--text-muted);
  }
</style>
