<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AuthError,
    getCredentialGrants,
    saveCredentialGrant,
    revokeCredentialGrant,
    deleteCredential,
    grantExistingCredentials,
    showRecoveryCodes,
    getDeletedCredentials,
    type CredentialHistory,
    type StepUpProof,
    type CredentialGrant,
    type CredentialGrantsSettings,
    type CredentialSummary,
  } from '$lib/api';
  import {
    Alert,
    Badge,
    Button,
    Chip,
    ConfirmDialog,
    CountPill,
    EmptyState,
    KebabMenu,
    Modal,
    Card,
    CopyButton,
  } from '$lib/components/ui';
  import type { KebabItem } from '$lib/components/ui/KebabMenu.svelte';
  import { createConfirm } from '$lib/ui/confirm.svelte';
  import CredentialAccessFields from './CredentialAccessFields.svelte';
  import CredentialFormModal from './CredentialFormModal.svelte';
  import StepUpDialog from './StepUpDialog.svelte';
  import CredentialHistoryDialog from './CredentialHistoryDialog.svelte';

  let { onSignedOut = () => {} }: { onSignedOut?: () => void } = $props();
  let data: CredentialGrantsSettings | null = $state(null);
  let error = $state('');
  let busy = $state(false);
  let editing = $state('');
  let editorOpen = $state(false);
  let scope = $state<'all' | 'rooms'>('all');
  let rooms: string[] = $state([]);
  let scheduled = $state(false);
  let allowHttp = $state(false);
  let omittedRooms = $state(false);
  let confirmExisting = $state(false);
  const confirmRevoke = createConfirm<string>();
  const confirmDelete = createConfirm<CredentialSummary>();
  // Mounted per open, so the form's fields start empty every time.
  let form: { mode: 'add' | 'edit'; credential: CredentialSummary | null } | null = $state(null);
  let historyName: string | null = $state(null);
  let deleted: CredentialHistory[] = $state([]);
  let deletedOpen = $state(false);
  let reveal: { name: string; codes: string[]; spent: number[] } | null = $state(null);
  let revealName: string | null = $state(null);
  const recoveryText = $derived.by(() => {
    const current = reveal;
    if (!current) return '';
    return current.codes
      .map((code, index) => `${current.spent.includes(index) ? '(used) ' : ''}${code}`)
      .join('\n');
  });

  function openReveal(name: string) {
    revealName = name;
  }
  async function showCodes(proof: StepUpProof) {
    const name = revealName;
    if (!name) return;
    const result = await showRecoveryCodes(name, proof);
    if (revealName === name) reveal = { name, codes: result.codes, spent: result.spent };
  }
  async function showDeleted() {
    try {
      deleted = await getDeletedCredentials();
      deletedOpen = true;
    } catch (e) {
      report(e);
    }
  }

  const SOURCE_LABEL: Record<string, string> = {
    local: 'Istota',
    config: 'Deployment',
    generated: 'Generated',
  };

  function report(e: unknown) {
    if (e instanceof AuthError) onSignedOut();
    else error = (e as Error).message || 'Credentials could not be loaded.';
  }
  async function refresh() {
    try {
      data = await getCredentialGrants();
    } catch (e) {
      report(e);
    }
  }
  onMount(refresh);
  function editAccess(name: string) {
    const grant = data?.credentials.find((c) => c.name === name)?.grant;
    editing = name;
    scope = grant?.scope_mode ?? 'all';
    const availableRooms = new Set(data?.rooms.map((room) => room.token));
    rooms = (grant?.rooms ?? []).filter((room) => availableRooms.has(room));
    omittedRooms = rooms.length !== (grant?.rooms.length ?? 0);
    scheduled = grant?.allow_scheduled ?? false;
    allowHttp = grant?.allow_http ?? false;
    error = '';
    editorOpen = true;
  }
  async function mutate(action: () => Promise<unknown>) {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await action();
      editorOpen = false;
      confirmExisting = false;
      await refresh();
    } catch (e) {
      report(e);
    } finally {
      busy = false;
    }
  }
  function save() {
    return mutate(() =>
      saveCredentialGrant(editing, {
        scope_mode: scope,
        rooms: scope === 'rooms' ? rooms : [],
        allow_scheduled: scheduled,
        allow_http: allowHttp,
      }),
    );
  }
  function revoke() {
    const name = confirmRevoke.target;
    if (name) return mutate(() => revokeCredentialGrant(name));
  }

  function remove() {
    const name = confirmDelete.target?.name;
    if (name) return mutate(() => deleteCredential(name));
  }

  /** Room scope, scheduled access and the HTTP override. */
  function grantSummary(grant: CredentialGrant): string {
    const n = grant.rooms.length;
    const where = grant.scope_mode === 'all' ? 'All rooms' : `${n} room${n === 1 ? '' : 's'}`;
    const parts = [where];
    if (grant.allow_scheduled) parts.push('scheduled');
    if (grant.allow_http) parts.push('HTTP allowed');
    return parts.join(' · ');
  }

  /** What to do about a missing site, which depends on where the credential is edited. */
  function noSiteLine(c: CredentialSummary): string {
    if (c.source === 'local') return 'No site. Edit it to add one.';
    if (c.source === 'generated') return 'No site';
    return 'No site';
  }

  function menu(c: CredentialSummary): KebabItem[] {
    const items: KebabItem[] = [];
    if (c.source === 'local')
      items.push({
        label: 'Edit',
        disabled: busy,
        onSelect: () => (form = { mode: 'edit', credential: c }),
      });
    // Disabled rather than absent without a site: access needs a host to bind
    // to, and the row says how to give it one.
    items.push({
      label: 'Edit access',
      disabled: busy || !c.hosts.length,
      onSelect: () => editAccess(c.name),
    });
    if (c.grant)
      items.push({
        label: 'Revoke access',
        danger: true,
        disabled: busy,
        onSelect: () => confirmRevoke.ask(c.name),
      });
    if (c.source !== 'config')
      items.push({ label: 'History', onSelect: () => (historyName = c.name) });
    if (c.source === 'generated' && c.recovery)
      items.push({
        label: 'Show recovery codes',
        disabled: busy,
        onSelect: () => openReveal(c.name),
      });
    if (c.source === 'local' || c.source === 'generated')
      items.push({
        label:
          c.source === 'local'
            ? 'Delete'
            : c.source === 'generated'
              ? 'Retire'
              : 'Remove stored copy',
        danger: true,
        disabled: busy,
        onSelect: () => confirmDelete.ask(c),
      });
    return items;
  }
</script>

<Card
  title="Credentials"
  description="A task can send a credential only to its site, and only from rooms you allow."
>
  {#snippet status()}
    <CountPill count={data?.credentials.length ?? 0} tone="muted" />
  {/snippet}
  {#snippet actions()}
    {#if data?.grant_existing_available}
      <Button variant="pill" size="sm" onclick={() => (confirmExisting = true)} disabled={busy}>
        Allow all existing
      </Button>
    {/if}
    <Button
      variant="primary"
      size="sm"
      onclick={() => (form = { mode: 'add', credential: null })}
      disabled={!data || !data.can_add || busy}
    >
      Add credential
    </Button>
  {/snippet}
  {#if data && !data.can_add && data.add_blocked_reason}
    <p class="caption add-blocked" data-testid="add-blocked">{data.add_blocked_reason}</p>
  {/if}
  {#if error && !editorOpen}<Alert appearance="inline" variant="danger" role="alert">{error}</Alert
    >{/if}
  {#if data}
    {#if !data.broker_enabled}
      <Alert appearance="inline" variant="info">
        Access settings are saved but not enforced until your administrator turns on the credential
        broker.
      </Alert>
    {/if}
    {#if !data.sandboxed}
      <Alert appearance="inline" variant="info">
        Tasks on this deployment run without a sandbox, so credential values are not contained.
      </Alert>
    {/if}
    {#if data.credentials.length === 0}
      <EmptyState>No credentials yet.</EmptyState>
    {:else}
      <ul class="divided-list">
        {#each data.credentials as credential (credential.name)}
          <li class="cred-row" data-testid="credential-{credential.name}">
            <code class="cred-name code-chip">{credential.name}</code>
            <div class="cred-main">
              {#if credential.hosts.length}
                <span class="cred-hosts">{credential.hosts.join(', ')}</span>
              {:else}
                <span class="cred-warn">{noSiteLine(credential)}</span>
              {/if}
              {#if credential.grant}
                <span class="caption">{grantSummary(credential.grant)}</span>
              {:else}
                <span class="caption cred-warn">No access yet</span>
              {/if}
            </div>
            <div class="cred-badges">
              {#if credential.otp}<Badge size="sm">2FA</Badge>{/if}
              {#if credential.recovery}<Badge size="sm">Recovery codes</Badge>{/if}
              <span class="cred-source cred-source-{credential.source}">
                <Badge size="sm">{SOURCE_LABEL[credential.source] ?? credential.source}</Badge>
              </span>
              {#if credential.hosts.some( (host) => host.startsWith('http://') ) && !credential.grant?.allow_http}
                <Badge size="sm" variant="warn">HTTPS required</Badge>
              {/if}
              {#if credential.revealable}<Badge size="sm" variant="info">Readable by tasks</Badge
                >{/if}
            </div>
            <KebabMenu items={menu(credential)} ariaLabel="Actions for {credential.name}" />
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
  <div class="hstack wrap">
    <Button variant="secondary" size="sm" onclick={showDeleted}>Recently deleted credentials</Button
    >
  </div>
</Card>

{#if form}
  <CredentialFormModal
    mode={form.mode}
    credential={form.credential}
    rooms={data?.rooms ?? []}
    {onSignedOut}
    onClose={() => (form = null)}
    onSaved={refresh}
  />
{/if}

<Modal bind:open={editorOpen} title="Access for {editing}">
  <CredentialAccessFields
    rooms={data?.rooms ?? []}
    bind:scope
    bind:selected={rooms}
    bind:scheduled
    bind:allowHttp
    {omittedRooms}
    error={editorOpen ? error : ''}
  />
  {#snippet footer()}
    <Button variant="secondary" onclick={() => (editorOpen = false)} disabled={busy}>Cancel</Button>
    <Button variant="primary" onclick={save} loading={busy}>Save access</Button>
  {/snippet}
</Modal>
{#if revealName}
  <StepUpDialog
    action="recovery_reveal"
    name={revealName}
    onConfirm={showCodes}
    onComplete={() => (revealName = null)}
    onCancel={() => (revealName = null)}
  />
{/if}
<Modal
  open={reveal !== null && revealName === null}
  title="Recovery codes for {reveal?.name ?? ''}"
  onOpenChange={(open) => {
    if (!open) reveal = null;
  }}
>
  {#if reveal}
    <p class="caption reveal-note">
      Keep these codes somewhere only you can reach, and close this when you are done.
    </p>
    <pre class="recovery-codes" data-testid="recovery-codes">{recoveryText}</pre>
  {/if}
  {#snippet footer()}
    <CopyButton
      variant="button"
      size="md"
      text={recoveryText}
      copiedLabel="Recovery codes"
      disabled={!reveal}
    />
    <Button variant="primary" onclick={() => (reveal = null)}>Done</Button>
  {/snippet}
</Modal>
<Modal
  open={deletedOpen}
  title="Recently deleted credentials"
  onOpenChange={(open) => (deletedOpen = open)}
>
  {#if deleted.length}
    {#each deleted as row (row.id)}
      <p>
        <Button
          variant="secondary"
          onclick={() => {
            deletedOpen = false;
            historyName = row.name;
          }}>{row.name}</Button
        >
      </p>
    {/each}
  {:else}<p>No deleted credentials in history.</p>{/if}
</Modal>
{#if historyName}
  <CredentialHistoryDialog
    name={historyName}
    onClose={() => (historyName = null)}
    onChanged={refresh}
  />
{/if}
{#if confirmDelete.target}
  {@const target = confirmDelete.target}
  <ConfirmDialog
    bind:open={confirmDelete.open}
    title={target.source === 'local'
      ? 'Delete credential'
      : target.source === 'generated'
        ? 'Retire credential'
        : 'Remove stored copy'}
    message={target.source === 'local'
      ? `Delete ${target.name}? Tasks lose it now. You can restore it from history until its saved versions expire.`
      : target.source === 'generated'
        ? `Retire ${target.name}? Tasks lose it now, exported copies stay as they are. You can restore it from history until its saved versions expire. If the account still needs this password or its two-factor code, change them on the site first.`
        : `Remove the stored copy of ${target.name}? Tasks lose it now. You can restore it from history until its saved versions expire.`}
    confirmLabel={target.source === 'local'
      ? 'Delete'
      : target.source === 'generated'
        ? 'Retire'
        : 'Remove'}
    busyLabel={target.source === 'local'
      ? 'Deleting…'
      : target.source === 'generated'
        ? 'Retiring…'
        : 'Removing…'}
    onConfirm={remove}
  />
{/if}
<ConfirmDialog
  bind:open={confirmExisting}
  title="Allow all existing credentials"
  message="Allow all currently bound credentials in every room, including scheduled tasks? Credentials added later remain ungranted."
  confirmLabel="Allow all existing"
  busyLabel="Allowing…"
  onConfirm={() => mutate(grantExistingCredentials)}
  confirmVariant="primary"
/>
<ConfirmDialog
  bind:open={confirmRevoke.open}
  title="Revoke access"
  message="Are you sure you want to revoke access to {confirmRevoke.target}? No task can use it until you allow it again."
  confirmLabel="Revoke"
  busyLabel="Revoking…"
  confirmVariant="danger"
  onConfirm={revoke}
/>

<style>
  .cred-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .cred-name {
    color: var(--text-primary);
    flex: 0 1 9rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .cred-main {
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-width: 0;
  }

  .cred-hosts {
    font-size: var(--text-sm);
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }

  .cred-main > .cred-warn:first-child {
    font-size: var(--text-sm);
  }

  .cred-warn {
    color: var(--status-warn-fg);
  }

  .cred-badges {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--space-1);
  }

  /* A categorical hue per source, reaching the child Badge through its
     documented --badge-bg / --badge-fg hook. Istota takes the bot's identity
     accent, as the admin badge does. design-lint-allow-begin: categorical
     custom properties a child component reads. */
  .cred-source {
    display: inline-flex;
  }

  .cred-source-local {
    --badge-bg: color-mix(in srgb, var(--accent-amber) 18%, transparent);
    --badge-fg: var(--accent-amber);
  }

  .cred-source-config {
    --badge-bg: var(--status-partial-bg);
    --badge-fg: var(--status-partial-fg);
  }

  .cred-source-generated {
    --badge-bg: var(--status-info-bg);
    --badge-fg: var(--status-info-fg);
  }
  /* design-lint-allow-end */

  .add-blocked {
    margin: 0 0 var(--space-2);
  }

  .reveal-note {
    margin: 0 0 var(--space-2);
  }

  .recovery-codes {
    font-family: var(--font-mono);
    font-size: var(--text-sm);
    background: var(--surface-raised);
    border-radius: var(--radius-sm);
    padding: var(--space-2);
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    user-select: all;
  }

  /* On a phone the name and the menu share the first line and the rest wraps
     under them, so a long host list is not squeezed into a sliver beside the
     badges. */
  @media (max-width: 640px) {
    .cred-row {
      flex-wrap: wrap;
    }
    .cred-name {
      flex: 1 1 auto;
    }
    .cred-row :global(.ui-kebab-trigger) {
      order: 1;
    }
    .cred-main {
      order: 2;
      flex-basis: 100%;
    }
    .cred-badges {
      order: 3;
      justify-content: flex-start;
    }
  }
</style>
