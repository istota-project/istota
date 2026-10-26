<script lang="ts">
  import {
    AuthError,
    acceptSharedFolder,
    addSharedFeed,
    createCalendarFeedLink,
    declineSharedFolder,
    removeCalendarFeedLink,
    removeSharedFeed,
    removeSharedFolder,
    setCalendarDelivery,
    type CalendarDelivery,
    type SharedFeed,
    type SharedFolder,
    type SharedFolderOffer,
    type SharingSettings,
  } from '$lib/api';
  import {
    Alert,
    Badge,
    Button,
    Card,
    Checkbox,
    ConfirmDialog,
    CopyField,
    EmptyState,
    Field,
    FormModal,
    Input,
    KebabMenu,
    Select,
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

  // Folders shared over OCM. Accepting opens the share once on the server to
  // prove it; declining and removing tell the sender, so both ask first.
  const ocm = $derived(settings?.ocm ?? null);
  const folderShares = $derived(ocm?.shares ?? []);
  let accepting = $state<SharedFolderOffer | null>(null);
  let acceptOpen = $state(false);
  let allowScheduled = $state(false);
  const declining = createConfirm<SharedFolderOffer>();
  const removingFolder = createConfirm<SharedFolder>();

  function folderName(item: { name: string | null }): string {
    return item.name || 'A folder';
  }

  function openAccept(offer: SharedFolderOffer) {
    accepting = offer;
    allowScheduled = false;
    acceptOpen = true;
  }

  // A refusal rejects into the dialog, which stays open to say so; an expired
  // session goes to onError.
  async function folderAction(run: () => Promise<unknown>, close?: () => void) {
    try {
      await run();
    } catch (e) {
      if (e instanceof AuthError) {
        close?.();
        onError(e);
        return;
      }
      throw e;
    }
    onSaved();
  }

  async function acceptOffer() {
    const offer = accepting;
    if (!offer) return;
    await folderAction(
      () => acceptSharedFolder(offer.id, allowScheduled),
      () => (acceptOpen = false),
    );
  }

  async function declineOffer() {
    const offer = declining.target;
    if (!offer) return;
    await folderAction(
      () => declineSharedFolder(offer.id),
      () => declining.cancel(),
    );
  }

  async function removeFolder() {
    const share = removingFolder.target;
    if (!share) return;
    await folderAction(
      () => removeSharedFolder(share.id),
      () => removingFolder.cancel(),
    );
  }

  // Istota's own calendar. The feed address is shown once, when it is made;
  // the server keeps only its digest.
  const calendar = $derived(settings?.calendar ?? null);
  const DELIVERY_OPTIONS = [
    { value: 'invite', label: 'Invitation by email' },
    { value: 'feed', label: 'The calendar feed only' },
  ];
  let feedUrl = $state('');
  let linkBusy = $state(false);
  let resetting = $state(false);
  let unlinking = $state(false);
  let delivery = $state<CalendarDelivery>('invite');
  $effect(() => {
    if (calendar) delivery = calendar.delivery;
  });

  async function makeLink() {
    linkBusy = true;
    try {
      feedUrl = (await createCalendarFeedLink()).url;
      onSaved();
    } catch (e) {
      onError(e);
    } finally {
      linkBusy = false;
    }
  }

  async function removeLink() {
    try {
      await removeCalendarFeedLink();
      feedUrl = '';
      onSaved();
    } catch (e) {
      if (e instanceof AuthError) {
        onError(e);
        return;
      }
      throw e;
    }
  }

  async function chooseDelivery(value: string) {
    const previous = delivery;
    delivery = value as CalendarDelivery;
    try {
      await setCalendarDelivery(delivery);
      onSaved();
    } catch (e) {
      delivery = previous;
      onError(e);
    }
  }
</script>

<Card
  title="Shared with Istota"
  description={settings?.ocm
    ? 'Calendars you subscribed Istota to by their address, which it can read and not change, and folders you shared with it.'
    : 'Calendars you subscribed Istota to by their address. Istota can read them and cannot change them.'}
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
    {#if ocm}
      <h3>Shared folders</h3>
      {#if ocm.federated_id}
        <p class="caption">
          Share a folder with this address from your Nextcloud, or any server with federated
          sharing. Istota cannot read a folder until you accept it.
        </p>
        <CopyField label="Federated sharing address" value={ocm.federated_id} monospace />
      {:else}
        <Alert appearance="inline" variant="info"
          >This server no longer receives folder shares, so Istota does not hear when one of these
          folders is unshared. Remove a share here once you stop using it.</Alert
        >
      {/if}
      {#if ocm.accept_refusal && ocm.pending.length > 0}
        <Alert appearance="inline" variant="warn">{ocm.accept_refusal}</Alert>
      {/if}
      {#if ocm.pending.length === 0 && folderShares.length === 0}
        <EmptyState size="sm">No folder offers are waiting.</EmptyState>
      {/if}
      {#if ocm.pending.length > 0}
        <ul class="divided-list" aria-label="Folder offers">
          {#each ocm.pending as offer (offer.id)}
            <li class="shared-feed">
              <div class="shared-feed-main">
                <span>{folderName(offer)}</span>
                <span class="caption"
                  >from {offer.owner ? `${offer.owner} · ` : ''}{offer.host} · offered
                  <Timestamp value={offer.created_at} format="relative" empty="just now" /></span
                >
              </div>
              <Badge size="sm" variant="info">{offer.reoffer ? 'Offered again' : 'Waiting'}</Badge>
              <div class="feed-actions">
                <Button
                  variant="primary"
                  size="sm"
                  disabled={!!ocm.accept_refusal}
                  ariaLabel="Accept {folderName(offer)}"
                  onclick={() => openAccept(offer)}>Accept</Button
                >
                <Button
                  variant="secondary"
                  size="sm"
                  ariaLabel="Decline {folderName(offer)}"
                  onclick={() => declining.ask(offer)}>Decline</Button
                >
              </div>
            </li>
          {/each}
        </ul>
      {/if}
      {#if folderShares.length > 0}
        <ul class="divided-list" aria-label="Shared folders">
          {#each folderShares as share (share.id)}
            <li class="shared-feed">
              <div class="shared-feed-main">
                <span>{folderName(share)}</span>
                <span class="caption"
                  ><span class="mono">share:{share.id}</span> · {share.host} · {share.access ===
                  'write'
                    ? 'read and write'
                    : 'read only'}</span
                >
              </div>
              <Badge size="sm" variant={share.state === 'error' ? 'warn' : 'success'}
                >{share.state === 'error' ? 'Not reachable' : 'Active'}</Badge
              >
              <KebabMenu
                items={[
                  {
                    label: 'Remove',
                    danger: true,
                    onSelect: () => removingFolder.ask(share),
                  },
                ]}
                ariaLabel="Actions for {folderName(share)}"
              />
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
    {#if calendar}
      <h3>Istota's calendar</h3>
      <p class="caption">
        Events Istota puts on your calendar. Subscribe to the feed once in your calendar app and
        they appear there.
      </p>
      <Field label="Send new events by">
        <Select
          value={delivery}
          options={DELIVERY_OPTIONS}
          onValueChange={chooseDelivery}
          disabled={!calendar.email_enabled}
          fullWidth
        />
      </Field>
      {#if !calendar.email_enabled}
        <p class="caption">This server sends no email, so events reach you through the feed.</p>
      {:else if delivery === 'invite'}
        <p class="caption">
          The feed carries the same events. If you accept the invitations and also subscribe to the
          feed, each event shows twice.
        </p>
      {/if}
      {#if !calendar.can_link}
        <Alert appearance="inline" variant="info"
          >This server has no public name, so it has no feed address to give.</Alert
        >
      {:else}
        {#if feedUrl}
          <CopyField
            label="Calendar feed address"
            value={feedUrl}
            monospace
            description="Shown this once. Anyone with this address can read these events."
          />
        {:else if calendar.feed}
          <p class="caption">
            Feed address made <Timestamp value={calendar.feed.created_at} format="relative" />, last
            read <Timestamp value={calendar.feed.last_used_at} format="relative" empty="never" />.
          </p>
        {/if}
        {#if !calendar.reachable_elsewhere}
          <p class="caption">
            This server's address is local, so only calendar apps on this computer can read the
            feed.
          </p>
        {/if}
        <div class="feed-actions">
          {#if calendar.feed}
            <Button variant="secondary" size="sm" onclick={() => (resetting = true)}
              >Reset address</Button
            >
            <Button variant="danger" size="sm" onclick={() => (unlinking = true)}
              >Remove address</Button
            >
          {:else}
            <Button
              variant="secondary"
              size="sm"
              loading={linkBusy}
              loadingLabel="Making…"
              onclick={makeLink}>Get feed address</Button
            >
          {/if}
        </div>
      {/if}
    {/if}
  {/if}
</Card>

<ConfirmDialog
  bind:open={resetting}
  title="Reset feed address"
  message="Are you sure you want to reset the feed address? The current address stops working at once, and every calendar app subscribed to it needs the new one."
  confirmLabel="Reset"
  confirmVariant="danger"
  busyLabel="Resetting…"
  onConfirm={makeLink}
/>

<ConfirmDialog
  bind:open={unlinking}
  title="Remove feed address"
  message="Are you sure you want to remove the feed address? Calendar apps subscribed to it stop receiving Istota's events."
  confirmLabel="Remove"
  confirmVariant="danger"
  busyLabel="Removing…"
  onConfirm={removeLink}
/>

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

<FormModal
  bind:open={acceptOpen}
  title="Accept a shared folder"
  submitLabel="Accept"
  busyLabel="Opening…"
  onSubmit={acceptOffer}
  onCancel={() => (acceptOpen = false)}
>
  {#if accepting}
    <p>
      Istota opens {folderName(accepting)} from {accepting.host} once now to check the share. Then your
      tasks can read it, and change it if the share allows that.
    </p>
  {/if}
  <Checkbox
    bind:checked={allowScheduled}
    label="Let scheduled jobs use it"
    description="When this is off, only tasks you start can reach the folder; scheduled jobs and briefings cannot. You can change it later under Credentials."
  />
</FormModal>

<ConfirmDialog
  bind:open={declining.open}
  title="Decline folder"
  message={declining.target
    ? declining.target.reoffer
      ? `Are you sure you want to decline the new offer of ${folderName(declining.target)}? The folder stays shared as it is now.`
      : `Are you sure you want to decline ${folderName(declining.target)}? Istota tells ${declining.target.host}, and the owner has to share it again before Istota can reach it.`
    : ''}
  confirmLabel="Decline"
  confirmVariant="danger"
  busyLabel="Declining…"
  onConfirm={declineOffer}
/>

<ConfirmDialog
  bind:open={removingFolder.open}
  title="Remove folder"
  message={removingFolder.target
    ? `Are you sure you want to remove ${folderName(removingFolder.target)}? Istota forgets its link and tells ${removingFolder.target.host}, which ends the share.`
    : ''}
  confirmLabel="Remove"
  confirmVariant="danger"
  busyLabel="Removing…"
  onConfirm={removeFolder}
/>

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
  .feed-actions {
    display: flex;
    flex-wrap: wrap;
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
