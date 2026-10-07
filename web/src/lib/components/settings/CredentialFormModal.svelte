<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import {
    AuthError,
    CredentialWriteError,
    createCredential,
    updateLocalCredential,
    type CredentialSummary,
  } from '$lib/api';
  import { Button, Field, Input, Modal } from '$lib/components/ui';
  import { notifySuccess } from '$lib/stores/notices';
  import CredentialAccessFields from './CredentialAccessFields.svelte';
  import SecretField from './SecretField.svelte';

  /**
   * Add a credential, or edit one added in Istota.
   *
   * Mounted for one open and unmounted on close, so every field starts from
   * the props and the value never outlives the dialog. The value is held in
   * this component's own state and nowhere else: not a store, not storage,
   * not a URL and not a notice.
   */
  interface Props {
    mode: 'add' | 'edit';
    /** The credential being edited. Ignored on add. */
    credential?: CredentialSummary | null;
    rooms: { token: string; name: string }[];
    onClose: () => void;
    onSaved: (name: string) => void;
    onSignedOut?: () => void;
  }

  let {
    mode,
    credential = null,
    rooms,
    onClose,
    onSaved,
    onSignedOut = () => {},
  }: Props = $props();

  // Read once: the form is mounted per open, and a list refresh behind it must
  // not rewrite what the user is typing.
  const start = untrack(() => (mode === 'edit' ? credential : null));
  const editing = start !== null;
  const initialUrl = start?.url ?? '';
  // From the server, which splits the bound hosts with the parser that bound
  // them; a string comparison here would read `host:443` as an extra host and
  // keep it bound after the site changed.
  const initialExtraHosts = start?.extra_hosts ?? '';

  let open = $state(true);
  let busy = $state(false);
  let banner = $state('');
  let errors: Record<string, string> = $state({});

  let name = $state(editing ? (start?.name ?? '') : '');
  let value = $state('');
  let username = $state('');
  let removeUsername = $state(false);
  let otp = $state('');
  let removeOtp = $state(false);
  let site = $state(initialUrl);
  let extraHosts = $state(initialExtraHosts);
  let headers = $state(editing ? (start?.headers ?? []).join(', ') : '');
  let revealable = $state(editing ? (start?.revealable ?? false) : false);
  let moreOpen = $state(false);

  let scope = $state<'all' | 'rooms'>('all');
  let selectedRooms: string[] = $state([]);
  let scheduled = $state(false);
  let allowHttp = $state(false);

  const usernameSet = editing && (start?.username_set ?? false);
  let hasSite = $derived(site.trim() !== '');
  let shownFields = $derived(
    new Set(
      ['name', 'value', 'username', 'otp', 'url', 'extra_hosts', 'headers'].concat(
        !editing && hasSite ? ['access'] : [],
      ),
    ),
  );

  function clearSecret() {
    value = '';
    username = '';
    otp = '';
  }

  onDestroy(clearSecret);

  function cancel() {
    // Escape and the overlay bypass the disabled fieldset; while a save runs,
    // its result decides whether the dialog closes.
    if (busy) {
      open = true;
      return;
    }
    clearSecret();
    open = false;
    onClose();
  }

  async function save() {
    if (busy) return;
    busy = true;
    banner = '';
    errors = {};
    try {
      if (editing) {
        await updateLocalCredential(name, {
          value: value === '' ? null : value,
          username: removeUsername ? '' : username === '' ? null : username,
          ...(removeOtp ? { otp: '' } : otp !== '' ? { otp } : {}),
          url: site,
          extra_hosts: extraHosts,
          headers,
          revealable,
        });
      } else {
        await createCredential({
          name,
          value,
          username,
          ...(otp !== '' ? { otp } : {}),
          url: site,
          extra_hosts: extraHosts,
          headers,
          revealable,
          ...(hasSite
            ? {
                access: {
                  scope_mode: scope,
                  rooms: scope === 'rooms' ? selectedRooms : [],
                  allow_scheduled: scheduled,
                  allow_http: allowHttp,
                },
              }
            : {}),
        });
      }
      const saved = name;
      clearSecret();
      open = false;
      notifySuccess(editing ? `Saved ${saved}` : `Added ${saved}`);
      onSaved(saved);
      onClose();
    } catch (e) {
      if (e instanceof AuthError) {
        clearSecret();
        onSignedOut();
        return;
      }
      if (e instanceof CredentialWriteError && e.field && shownFields.has(e.field)) {
        errors = { [e.field]: e.message };
        if (e.field === 'extra_hosts' || e.field === 'headers') moreOpen = true;
      } else {
        banner = (e as Error).message || 'The credential could not be saved.';
      }
    } finally {
      busy = false;
    }
  }
</script>

<Modal
  bind:open
  title={editing ? `Edit ${name}` : 'Add credential'}
  width="480px"
  onOpenChange={(next) => {
    if (!next) cancel();
  }}
>
  <fieldset class="cred-form" disabled={busy}>
    <div class="cred-field">
      <Field label="Name" error={errors.name}>
        <Input
          bind:value={name}
          readonly={editing}
          monospace
          autocomplete="off"
          spellcheck="false"
          invalid={!!errors.name}
        />
      </Field>
      {#if !editing}
        <p class="caption">Lowercase letters, digits and underscores. Tasks use this name.</p>
      {/if}
    </div>
    <div class="cred-field">
      <SecretField
        label="Value"
        configured={editing}
        {value}
        error={errors.value}
        onValueChange={(next) => (value = next)}
      />
      {#if editing}<p class="caption">Leave empty to keep the current value.</p>{/if}
    </div>
    <div class="cred-field">
      <Field label="Username" error={errors.username}>
        <Input
          bind:value={username}
          autocomplete="off"
          disabled={removeUsername}
          invalid={!!errors.username}
        />
      </Field>
      {#if usernameSet}
        <p class="caption">Leave empty to keep it.</p>
        <Field label="Remove username" checkbox>
          <input type="checkbox" bind:checked={removeUsername} />
        </Field>
      {/if}
    </div>
    <div class="cred-field">
      <Field label="Two-factor (TOTP)" error={errors.otp}>
        <Input
          type="password"
          bind:value={otp}
          placeholder="otpauth://totp/... or base32 secret"
          autocomplete="off"
          spellcheck="false"
          disabled={removeOtp}
          invalid={!!errors.otp}
        />
      </Field>
      {#if editing && start?.otp_set}
        <p class="caption">Leave empty to keep two-factor.</p>
        <Field label="Remove two-factor" checkbox>
          <input type="checkbox" bind:checked={removeOtp} />
        </Field>
      {/if}
    </div>
    <div class="cred-field">
      <Field label="Site" error={errors.url}>
        <Input
          bind:value={site}
          placeholder="api.example.com"
          autocomplete="off"
          spellcheck="false"
          invalid={!!errors.url}
        />
      </Field>
      <p class="caption">The credential is only ever sent here.</p>
    </div>
    {#if !editing}
      <div class="cred-field" data-testid="credential-access">
        <p class="micro-label">Who may use it</p>
        {#if hasSite}
          <CredentialAccessFields
            {rooms}
            bind:scope
            bind:selected={selectedRooms}
            bind:scheduled
            bind:allowHttp
            error={errors.access}
          />
        {:else}
          <p class="caption">Add a site to choose who may use it.</p>
        {/if}
      </div>
    {/if}
    <details class="cred-more" bind:open={moreOpen}>
      <summary>More options</summary>
      <div class="cred-more-fields">
        <div class="cred-field">
          <Field label="Also used on" error={errors.extra_hosts}>
            <Input
              bind:value={extraHosts}
              placeholder="api2.example.com, example.org"
              autocomplete="off"
              spellcheck="false"
              invalid={!!errors.extra_hosts}
            />
          </Field>
          <p class="caption">Other hosts it may be sent to, separated by commas.</p>
        </div>
        <div class="cred-field">
          <Field label="Headers" error={errors.headers}>
            <Input
              bind:value={headers}
              autocomplete="off"
              spellcheck="false"
              invalid={!!errors.headers}
            />
          </Field>
          <p class="caption">
            Request headers it may be sent in, separated by commas. Leave empty for the usual
            authorization headers.
          </p>
        </div>
        <div class="cred-field">
          <Field label="Tasks may read the value" checkbox>
            <input type="checkbox" bind:checked={revealable} />
          </Field>
          <p class="caption">Otherwise a task can only have Istota send it.</p>
        </div>
      </div>
    </details>
    {#if banner}<p class="banner error" role="alert">{banner}</p>{/if}
  </fieldset>
  {#snippet footer()}
    <Button variant="ghost" onclick={cancel} disabled={busy}>Cancel</Button>
    <Button variant="primary" onclick={save} loading={busy}>
      {editing ? 'Save' : 'Add credential'}
    </Button>
  {/snippet}
</Modal>

<style>
  .cred-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .cred-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .cred-field .caption,
  .cred-field .micro-label {
    margin: 0;
  }

  .cred-more summary {
    cursor: pointer;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  .cred-more-fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-top: var(--space-3);
  }

  .cred-form .banner {
    margin: 0;
  }
</style>
