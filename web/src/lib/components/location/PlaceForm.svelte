<script lang="ts">
  import { untrack } from 'svelte';
  import type { DiscoveredCluster, Place } from '$lib/api';
  import { locationPlaces } from '$lib/stores/location';
  import {
    Button,
    ConfirmDialog,
    FormModal,
    Field,
    Input,
    Select,
    TextArea,
    type SelectOption,
  } from '$lib/components/ui';

  interface Props {
    cluster?: DiscoveredCluster;
    place?: Place;
    initialLat?: number;
    initialLon?: number;
    onSave: (data: {
      name: string;
      lat: number;
      lon: number;
      radius_meters: number;
      category: string;
      notes: string;
    }) => void | Promise<void>;
    onCancel: () => void;
    onDismiss?: (data: { lat: number; lon: number; radius_meters: number }) => void;
    onDelete?: (place: Place) => void;
  }

  let { cluster, place, initialLat, initialLon, onSave, onCancel, onDismiss, onDelete }: Props =
    $props();

  const focusOnMount = (el: HTMLElement) => el.focus();

  const editing = $derived(!!place);
  const manual = $derived(!place && !cluster);
  const showCoords = $derived(editing || manual);

  let name = $state(untrack(() => place?.name ?? ''));
  let category = $state(untrack(() => place?.category ?? 'other'));
  let radius = $state(untrack(() => place?.radius_meters ?? cluster?.radius_meters ?? 100));
  let lat = $state(untrack(() => place?.lat ?? cluster?.lat ?? initialLat ?? 0));
  let lon = $state(untrack(() => place?.lon ?? cluster?.lon ?? initialLon ?? 0));
  let notes = $state(untrack(() => place?.notes ?? ''));
  let open = $state(true);

  const baseCategories = [
    'home',
    'work',
    'gym',
    'food',
    'shopping',
    'social',
    'friend',
    'medical',
    'hotel',
    'transit',
    'other',
  ];

  const categoryOptions = $derived.by<SelectOption[]>(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const c of baseCategories) {
      if (c && !seen.has(c)) {
        seen.add(c);
        out.push(c);
      }
    }
    for (const p of $locationPlaces) {
      const c = p.category?.trim();
      if (c && !seen.has(c)) {
        seen.add(c);
        out.push(c);
      }
    }
    if (category && !seen.has(category)) {
      seen.add(category);
      out.push(category);
    }
    return out
      .sort((a, b) => a.localeCompare(b))
      .map((c) => ({ value: c, label: c[0].toUpperCase() + c.slice(1) }));
  });

  function handleSave() {
    return onSave({
      name: name.trim(),
      lat,
      lon,
      radius_meters: radius,
      category,
      notes: notes.trim(),
    });
  }

  function handleDismiss() {
    onDismiss?.({ lat, lon, radius_meters: radius });
  }

  let confirmDelete = $state(false);

  function handleDelete() {
    if (!place || !onDelete) return;
    confirmDelete = true;
  }

  function performDelete() {
    if (!place || !onDelete) return;
    confirmDelete = false;
    onDelete(place);
  }

  const title = $derived(editing ? 'Edit place' : manual ? 'New place' : 'Name this place');
</script>

<FormModal
  bind:open
  {title}
  width="320px"
  submitLabel={editing ? 'Update' : 'Save'}
  canSubmit={!!name.trim()}
  onSubmit={handleSave}
  {onCancel}
>
  {#if cluster && !editing}
    <div class="caption">{cluster.total_pings} pings recorded here</div>
  {/if}

  <div class="vstack">
    <Field label="Name">
      <Input {@attach focusOnMount} bind:value={name} placeholder="e.g. Office, Gym..." />
    </Field>

    <Field label="Category">
      <Select bind:value={category} options={categoryOptions} />
    </Field>

    <Field label="Radius ({radius}m)">
      <input
        class="radius"
        type="range"
        min="25"
        max="500"
        step="25"
        aria-label="Radius in metres"
        bind:value={radius}
      />
    </Field>

    {#if showCoords}
      <div class="coords hstack">
        <Field label="Lat">
          <Input type="number" step="0.00001" bind:value={lat} />
        </Field>
        <Field label="Lon">
          <Input type="number" step="0.00001" bind:value={lon} />
        </Field>
      </div>
    {/if}

    <Field label="Notes" optional>
      <TextArea bind:value={notes} rows={2} placeholder="Anything to remember about this place" />
    </Field>
  </div>

  {#snippet extraActions()}
    {#if editing && onDelete}
      <button class="delete-link" type="button" onclick={handleDelete}>Delete</button>
    {/if}
    {#if !editing && onDismiss}
      <Button variant="secondary" onclick={handleDismiss} title="Don't show this cluster again">
        Dismiss
      </Button>
    {/if}
  {/snippet}
</FormModal>

<ConfirmDialog
  bind:open={confirmDelete}
  title="Delete place"
  message={`Are you sure you want to delete "${place?.name ?? 'this place'}"? This cannot be undone.`}
  confirmLabel="Delete"
  onConfirm={performDelete}
/>

<style>
  /* Typography is the global .caption; the spacing stays at the call site. */
  .caption {
    margin-bottom: var(--space-2);
  }

  .radius {
    accent-color: var(--accent-amber);
  }

  .coords > :global(.field) {
    flex: 1;
  }

  .delete-link {
    margin-right: auto;
    background: none;
    border: none;
    color: var(--text-dim);
    font: inherit;
    font-size: var(--text-sm);
    cursor: pointer;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-pill);
    transition: color var(--transition-fast);
  }

  .delete-link:hover {
    color: var(--status-danger-fg);
  }
</style>
