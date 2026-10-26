<script lang="ts">
  import {
    AuthError,
    acceptSharedFolder,
    declineSharedFolder,
    removeSharedFolder,
    type SharedFolder,
    type SharedFolderOffer,
    type SharedFolderSettings,
  } from '$lib/api';
  import {
    Alert,
    Badge,
    Button,
    Checkbox,
    ConfirmDialog,
    CopyField,
    EmptyState,
    FormModal,
    KebabMenu,
    Timestamp,
  } from '@minot/ui';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { accessLabel, folderName, shareCaption, sharedFrom } from './folderShares';

  // Shares and offers over Open Cloud Mesh, on the Settings card and the Files
  // page's index. Every name and person here is the sender's words, drawn as text.
  let {
    ocm,
    onSaved,
    onError,
    open = null,
  }: {
    ocm: SharedFolderSettings;
    onSaved: () => void;
    onError: (e: unknown) => void;
    /** Where an accepted share opens, when this list can open one; null keeps names as text. */
    open?: {
      href: (share: SharedFolder) => string;
      onOpen: (event: MouseEvent, share: SharedFolder) => void;
    } | null;
  } = $props();

  const folderShares = $derived(ocm.shares ?? []);
  let accepting = $state<SharedFolderOffer | null>(null);
  let acceptOpen = $state(false);
  let allowScheduled = $state(false);
  const declining = createConfirm<SharedFolderOffer>();
  const removingFolder = createConfirm<SharedFolder>();

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
</script>

{#if ocm.federated_id}
  <p class="caption">
    Share a folder or a file with this address from your Nextcloud, or any server with federated
    sharing. Istota cannot read it until you accept it.
  </p>
  <CopyField label="Federated sharing address" value={ocm.federated_id} monospace />
{:else}
  <Alert appearance="inline" variant="info"
    >This server no longer receives folder shares, so Istota does not hear when one of these folders
    is unshared. Remove a share here once you stop using it.</Alert
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
      {@const from = sharedFrom(offer)}
      <li class="shared-row">
        <div class="shared-row-main">
          <span>{folderName(offer)}</span>
          <span class="caption"
            >from {from ? `${from} · ` : ''}{offer.host} · offered
            <Timestamp value={offer.created_at} format="relative" empty="just now" /></span
          >
        </div>
        <Badge size="sm" variant="info">{offer.reoffer ? 'Offered again' : 'Waiting'}</Badge>
        <div class="row-actions">
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
      <li class="shared-row">
        <div class="shared-row-main">
          {#if open}
            <a
              class="share-link"
              href={open.href(share)}
              onclick={(event) => open!.onOpen(event, share)}>{folderName(share)}</a
            >
          {:else}
            <span>{folderName(share)}</span>
          {/if}
          <span class="caption"
            >{shareCaption(share)}{#if share.accepted_at}
              · accepted <Timestamp value={share.accepted_at} format="relative" />{/if}</span
          >
          <span class="caption mono">share:{share.id}</span>
        </div>
        <Badge size="sm" variant={share.access === 'write' ? 'info' : 'neutral'}
          >{accessLabel(share.access)}</Badge
        >
        {#if share.state === 'error'}<Badge size="sm" variant="warn">Not reachable</Badge>{/if}
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

<style>
  .shared-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .row-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .shared-row-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .share-link {
    color: var(--text-primary);
    text-decoration: none;
  }
  .share-link:hover {
    text-decoration: underline;
  }
</style>
