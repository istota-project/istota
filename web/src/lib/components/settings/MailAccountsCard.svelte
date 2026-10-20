<script lang="ts">
  import {
    AuthError,
    deleteMailAccount,
    type MailAccount,
    type MailAccountsSettings,
    type MailAccountTestResult,
  } from '$lib/api';
  import {
    Alert,
    Badge,
    Button,
    Card,
    ConfirmDialog,
    EmptyState,
    KebabMenu,
    Spinner,
    type BadgeVariant,
    type KebabItem,
  } from '$lib/components/ui';
  import { notifyError } from '$lib/stores/notices';
  import { createConfirm } from '$lib/ui/confirm.svelte';
  import { waitForMailAccountTest } from '$lib/mailAccountTest';
  import MailAccountFormModal from './MailAccountFormModal.svelte';
  let {
    settings,
    loadError = '',
    onReload,
    onSaved,
    onError,
  }: {
    /** Null until loaded, and after a load that failed. */
    settings: MailAccountsSettings | null;
    loadError?: string;
    onReload: () => void;
    onSaved: () => void;
    onError: (e: unknown) => void;
  } = $props();

  type TestState =
    | { state: 'running' }
    | { state: 'answered'; result: MailAccountTestResult }
    | { state: 'unanswered' };

  let form: { account: MailAccount | null } | null = $state(null);
  const removing = createConfirm<MailAccount>();
  let tests: Record<number, TestState> = $state({});
  const writable = $derived(!!settings && !settings.refusal);
  const atLimit = $derived(!!settings && settings.accounts.length >= settings.max_per_user);

  function status(account: MailAccount): { label: string; variant: BadgeVariant } {
    if (!account.enabled) return { label: 'Disabled', variant: 'neutral' };
    if (!account.imap_password_set) return { label: 'Password not set', variant: 'warn' };
    if (account.permission === 'read_send' && settings?.allow_send)
      return { label: 'Read and send', variant: 'success' };
    return { label: 'Read only', variant: 'info' };
  }

  function name(account: MailAccount): string {
    return account.label || account.slug;
  }

  function heldLine(count: number): string {
    return count === 1 ? '1 held message' : `${count} held messages`;
  }

  async function test(account: MailAccount) {
    if (!writable || tests[account.id]?.state === 'running') return;
    tests[account.id] = { state: 'running' };
    try {
      const result = await waitForMailAccountTest(account.id);
      tests[account.id] = result ? { state: 'answered', result } : { state: 'unanswered' };
    } catch (e) {
      delete tests[account.id];
      if (e instanceof AuthError) onError(e);
      else notifyError((e as Error).message || 'The test could not be started.');
    }
  }

  // A refusal rejects into the dialog, which stays open to say so; an expired
  // session goes to onError, the one place that ends it.
  async function remove(account: MailAccount) {
    try {
      await deleteMailAccount(account.id);
      delete tests[account.id];
      onSaved();
    } catch (e) {
      if (e instanceof AuthError) {
        onError(e);
        return;
      }
      throw e;
    }
  }

  function menu(account: MailAccount): KebabItem[] {
    return [
      {
        label: 'Edit',
        disabled: !writable,
        onSelect: () => {
          form = { account };
        },
      },
      {
        label: 'Test connection',
        disabled: !writable || tests[account.id]?.state === 'running',
        onSelect: () => void test(account),
      },
      {
        label: 'Delete',
        danger: true,
        disabled: !writable,
        onSelect: () => removing.ask(account),
      },
    ];
  }

  const removeMessage = $derived.by(() => {
    const target = removing.target;
    if (!target) return '';
    const base = `Are you sure you want to delete ${name(target)}? Istota forgets its settings and stored passwords.`;
    if (!target.pending_drafts) return base;
    const count = target.pending_drafts;
    const held = count === 1 ? '1 message' : `${count} messages`;
    return `${base} ${held} waiting for your approval to send from this account will be discarded.`;
  });
</script>

<Card
  title="Mail accounts"
  description="Mailboxes Istota can read when you ask, and send from when you allow it."
>
  {#snippet actions()}
    {#if settings}<Button
        variant="primary"
        size="sm"
        disabled={!writable || atLimit}
        onclick={() => {
          form = { account: null };
        }}>Add account</Button
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
    {#if settings.refusal}<Alert appearance="inline" variant="info">{settings.refusal}</Alert>{/if}
    {#if settings.accounts.length === 0}
      <EmptyState size="sm">No mail accounts connected.</EmptyState>
    {:else}
      <ul class="divided-list">
        {#each settings.accounts as account (account.id)}
          {@const badge = status(account)}
          {@const run = tests[account.id]}
          <li class="mail-account">
            <div class="mail-account-row">
              <div class="mail-account-main">
                <span>{name(account)}</span>
                <span class="caption"
                  ><span class="mono">{account.slug}</span> · {account.imap_username} at {account.imap_host}{#if account.pending_drafts}
                    · {heldLine(account.pending_drafts)}{/if}</span
                >
              </div>
              <Badge size="sm" variant={badge.variant}>{badge.label}</Badge>
              <KebabMenu items={menu(account)} ariaLabel="Actions for {name(account)}" />
            </div>
            {#if run?.state === 'running'}
              <p class="caption hstack"><Spinner size="sm" /> Testing the connection…</p>
            {:else if run?.state === 'unanswered'}
              <Alert appearance="inline" variant="warn" role="status"
                >The test has not been answered after two minutes. The scheduler may not be running.</Alert
              >
            {:else if run?.state === 'answered'}
              <Alert
                appearance="inline"
                variant={run.result.ok ? 'success' : 'danger'}
                role="status"
              >
                {run.result.message}
                {#if run.result.lines.length}
                  <ul class="list-reset">
                    {#each run.result.lines as line, i (i)}
                      <li>{line.ok ? 'Passed' : 'Failed'}: {line.message}</li>
                    {/each}
                  </ul>
                {/if}
              </Alert>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if atLimit}
      <p class="caption">
        This server allows {settings.max_per_user} mail accounts per person.
      </p>
    {/if}
    {#if !settings.allow_send}
      <p class="caption">
        Sending from connected accounts is turned off on this server. Every account is read only.
      </p>
    {/if}
  {/if}
</Card>
{#if form && settings}<MailAccountFormModal
    account={form.account}
    allowSend={settings.allow_send}
    onClose={() => {
      form = null;
    }}
    {onSaved}
    {onError}
  />{/if}
<ConfirmDialog
  bind:open={removing.open}
  title="Delete mail account"
  message={removeMessage}
  confirmLabel="Delete"
  busyLabel="Deleting…"
  onConfirm={() => removing.target && remove(removing.target)}
/>

<style>
  .mail-account {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .mail-account-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .mail-account-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
