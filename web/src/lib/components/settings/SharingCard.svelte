<script lang="ts">
  import {
    AuthError,
    addSharedFeed,
    removeSharedFeed,
    type SharedFeed,
    type SharingSettings,
  } from '$lib/api';
  import {
    Alert,
    Badge,
    Button,
    Card,
    ConfirmDialog,
    EmptyState,
    Field,
    FormModal,
    Input,
    KebabMenu,
    Timestamp,
    type KebabItem,
  } from '@minot/ui';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';

  let {
    settings,
    loadError = '',
    onReload,
    onSaved,
    onError,
  }: {
    /** Null until loaded, and after a load that failed. */
    settings: SharingSettings | null;
    loadError?: string;
    onReload: () => void;
    onSaved: () => void;
    onError: (e: unknown) => void;
  } = $props();

  // Fixed text per reason, beside what the server says, so a refusal reads the
  // same whichever answered.
  const REASONS: Record<string, string> = {
    not_fetched: 'Not read yet',
    not_a_calendar: 'No longer returns a calendar',
    too_large: 'Too large to read',
    host_refused: 'Host not allowed',
    redirect_refused: 'Too many redirects',
    connection_failed: 'Host not reachable',
    timeout: 'Host did not answer',
    secret_unreadable: 'Address could not be read',
  };

  let adding = $state(false);
  let url = $state('');
  const removing = createConfirm<SharedFeed>();
  const atLimit = $derived(!!settings && settings.feeds.length >= settings.max_feeds);

  function name(feed: SharedFeed): string {
    return feed.name || feed.host;
  }

  function reason(code: string | null): string {
    if (!code) return 'Not refreshing';
    if (code.startsWith('http_')) return `The calendar answered with an error (${code.slice(5)})`;
    return REASONS[code] ?? 'Not refreshing';
  }

  function openAdd() {
    url = '';
    adding = true;
  }

  async function add() {
    try {
      await addSharedFeed(url.trim());
    } catch (e) {
      if (e instanceof AuthError) {
        adding = false;
        onError(e);
        return;
      }
      throw e;
    }
    onSaved();
  }

  // A refusal rejects into the dialog, which stays open to say so; an expired
  // session goes to onError, the one place that ends it.
  async function remove(feed: SharedFeed) {
    try {
      await removeSharedFeed(feed.id);
      onSaved();
    } catch (e) {
      if (e instanceof AuthError) {
        onError(e);
        return;
      }
      throw e;
    }
  }

  function menu(feed: SharedFeed): KebabItem[] {
    return [{ label: 'Remove', danger: true, onSelect: () => removing.ask(feed) }];
  }
</script>

<Card
  title="Shared with Istota"
  description="Calendars you subscribed Istota to by their address. Istota can read them and cannot change them."
>
  {#snippet actions()}
    {#if settings}<Button
        variant="primary"
        size="sm"
        disabled={!settings.can_add || atLimit}
        onclick={openAdd}>Add calendar</Button
      >{/if}
  {/snippet}
  {#if !settings}
    {#if loadError}
      <Alert appearance="inline" variant="danger" role="alert"
        >{loadError}
        {#snippet actions()}<Button variant="secondary" size="sm" onclick={onReload}>Retry</Button
          >{/snippet}
      </Alert>
    {:else}
      <p class="caption">Loading…</p>
    {/if}
  {:else}
    {#if !settings.can_add}
      <Alert appearance="inline" variant="info"
        >This server cannot store calendar addresses until its credential store is set up.</Alert
      >
    {/if}
    {#if settings.feeds.length === 0}
      <EmptyState size="sm">No calendars shared with Istota.</EmptyState>
    {:else}
      <ul class="divided-list">
        {#each settings.feeds as feed (feed.id)}
          <li class="shared-feed">
            <div class="shared-feed-main">
              <span>{name(feed)}</span>
              <span class="caption"
                ><span class="mono">feed:{feed.id}</span> · {feed.host} · read
                <Timestamp value={feed.last_ok_at} format="relative" empty="never" /></span
              >
              {#if feed.state === 'error'}
                <span class="caption">{reason(feed.last_reason)}. Istota keeps trying.</span>
              {/if}
            </div>
            <Badge size="sm" variant={feed.state === 'error' ? 'warn' : 'success'}
              >{feed.state === 'error' ? 'Not refreshing' : 'Active'}</Badge
            >
            <KebabMenu items={menu(feed)} ariaLabel="Actions for {name(feed)}" />
          </li>
        {/each}
      </ul>
    {/if}
    {#if atLimit}
      <p class="caption">This server allows {settings.max_feeds} shared calendars per person.</p>
    {/if}
  {/if}
</Card>

<FormModal
  bind:open={adding}
  title="Add a calendar"
  submitLabel="Add calendar"
  busyLabel="Checking…"
  canSubmit={url.trim().length > 0}
  onSubmit={add}
  onCancel={() => (adding = false)}
>
  <Field
    label="Calendar address"
    description="The secret iCal or ICS address from your calendar's sharing settings. Istota checks it once now, then reads it every few minutes. It is stored encrypted and is not shown again."
  >
    <Input
      type="url"
      bind:value={url}
      monospace
      autocomplete="off"
      placeholder="https://… or webcal://…"
    />
  </Field>
</FormModal>

<ConfirmDialog
  bind:open={removing.open}
  title="Remove calendar"
  message={removing.target
    ? `Are you sure you want to remove ${name(removing.target)}? Istota forgets its address and stops reading it.`
    : ''}
  confirmLabel="Remove"
  confirmVariant="danger"
  busyLabel="Removing…"
  onConfirm={() => removing.target && remove(removing.target)}
/>

<style>
  .shared-feed {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .shared-feed-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
