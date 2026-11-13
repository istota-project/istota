<script lang="ts">
  import { onDestroy } from 'svelte';
  import {
    ConfigChangeError,
    exportConfigBackup,
    getAdminConfig,
    type ConfigBackupExport,
    type StepUpProof,
  } from '$lib/api';
  import { downloadBlob } from '$lib/download';
  import { generateKeyfile } from '$lib/keepass';
  import { StepUpDialog } from '$lib/components/settings';
  import { Alert, Button, Checkbox, CopyButton, Field, Modal } from '@minot/ui';
  import { backupStamp, fromBase64, plainBackup } from './backupFile';

  interface Props {
    onClose: () => void;
    /** The form holds edits, which a backup of the saved file does not include. */
    unsaved?: boolean;
    /** A change request is open; the server refuses an encrypted backup then. */
    requestOpen?: boolean;
    /** The view the page holds says the server can write the encrypted file. */
    encryptedAvailable?: boolean;
    /** The view the page holds carries the server's size limit. */
    limitsAvailable?: boolean;
  }

  let {
    onClose,
    unsaved = false,
    requestOpen = false,
    encryptedAvailable = false,
    limitsAvailable = false,
  }: Props = $props();

  const RUNBOOK_URL = 'https://istota.cynium.com/docs/deployment/running-the-stack/';
  const SCOPE =
    'Configuration and installation credentials only; keep database/workspace backups and the ' +
    'original master key separately.';

  let stage = $state<'options' | 'step-up' | 'password'>('options');
  let includeSecrets = $state(false);
  let useKeyfile = $state(false);
  let busy = $state(false);
  let refusal = $state('');
  let password = $state('');
  let summary = $state<ConfigBackupExport['summary'] | null>(null);
  // Plain variables, not state: neither is drawn, and neither should outlive the dialog.
  let keyfile: string | null = null;
  let active = true;
  let pending: AbortController | null = null;

  let encryptedRefusal = $derived(
    !encryptedAvailable
      ? 'This server cannot write an encrypted backup: the vault extra is not installed.'
      : requestOpen
        ? 'A configuration change is waiting for the host. Back up with secrets once it has finished.'
        : null,
  );
  let canDownload = $derived(includeSecrets ? !encryptedRefusal : limitsAvailable);

  /** Everything secret-bearing the dialog holds, dropped. */
  function clear() {
    pending?.abort();
    pending = null;
    keyfile = null;
    password = '';
    summary = null;
  }

  function close() {
    clear();
    onClose();
  }

  onDestroy(() => {
    active = false;
    clear();
  });

  async function downloadPlain() {
    // A fresh read: the page's copy may be older than the saved file.
    const view = await getAdminConfig();
    if (!active) return;
    const built = plainBackup(view);
    if (!built.ok) {
      refusal = built.message;
      return;
    }
    downloadBlob(new Blob([built.bytes], { type: 'application/toml' }), built.filename);
    close();
  }

  async function startEncrypted() {
    if (useKeyfile && keyfile === null) {
      const generated = await generateKeyfile();
      if (!active) return;
      keyfile = generated;
      downloadBlob(
        new Blob([generated], { type: 'application/xml' }),
        `istota-config-${backupStamp()}.keyx`,
      );
    } else if (!useKeyfile) {
      keyfile = null;
    }
    stage = 'step-up';
  }

  async function start() {
    busy = true;
    refusal = '';
    try {
      if (includeSecrets) await startEncrypted();
      else await downloadPlain();
    } catch (e) {
      refusal = e instanceof Error ? e.message : 'The backup could not be made.';
    } finally {
      busy = false;
    }
  }

  async function download(proof: StepUpProof) {
    const controller = new AbortController();
    pending = controller;
    let result: ConfigBackupExport;
    try {
      result = await exportConfigBackup(
        keyfile === null ? null : btoa(keyfile),
        proof,
        controller.signal,
      );
    } catch (e) {
      if (!active || controller.signal.aborted) return;
      // A refused code or password stays in the step-up dialog, which reads `reason`.
      if (e instanceof ConfigChangeError && (e.field === 'code' || e.field === 'password')) throw e;
      // Anything else goes back to the options, kept, for a deliberate retry.
      stage = 'options';
      refusal = e instanceof Error ? e.message : 'The backup could not be made.';
      return;
    } finally {
      if (pending === controller) pending = null;
    }
    if (!active || controller.signal.aborted) {
      result.password = result.file = '';
      return;
    }
    password = result.password;
    summary = result.summary;
    keyfile = null;
    const bytes = fromBase64(result.file);
    result.password = result.file = '';
    downloadBlob(new Blob([bytes], { type: 'application/octet-stream' }), result.filename);
    stage = 'password';
  }
</script>

<Modal
  open={stage === 'options'}
  title="Back up configuration"
  width="560px"
  {busy}
  onOpenChange={(next) => {
    if (!next && stage === 'options') close();
  }}
>
  {#if unsaved}
    <Alert appearance="inline" variant="warn">
      Unsaved changes are not included. The backup is the configuration as it is saved.
    </Alert>
  {/if}
  <p>
    The backup is the saved config.toml with every credential replaced by
    <code>"&lt;unchanged&gt;"</code>. It does not hold database-backed settings (user profiles,
    briefings, scheduled jobs), user vaults, the admin list, the credential-store master key or
    other runtime keys, or settings supplied only through the environment.
  </p>

  <Field
    label="Include installation secrets"
    checkbox
    description="One encrypted KeePass file holding the configuration and the installation's credentials, opened by a password generated for it."
    warning={encryptedRefusal ?? undefined}
  >
    <Checkbox bind:checked={includeSecrets} disabled={busy || !!encryptedRefusal} />
  </Field>
  {#if includeSecrets}
    <Field
      label="Also require a key file"
      checkbox
      hint="Adds a separate file that must be present to unlock the backup, along with its password."
    >
      <Checkbox bind:checked={useKeyfile} disabled={busy} />
    </Field>
    {#if useKeyfile}
      <p class="description">
        Save the key file when it downloads, somewhere other than this installation. You will need
        both it and the backup password to open the KeePass file.
      </p>
    {/if}
  {:else if !limitsAvailable}
    <p class="caption">
      This server did not say how large a backup may be. Reload the page to download the
      configuration.
    </p>
  {/if}

  <p class="scope">
    {SCOPE}
    <a href={RUNBOOK_URL} target="_blank" rel="noopener noreferrer">Recovering an installation</a>
  </p>

  {#if refusal}
    <Alert appearance="inline" variant="danger" role="alert">{refusal}</Alert>
  {/if}

  {#snippet footer()}
    <Button variant="secondary" onclick={close} disabled={busy}>Cancel</Button>
    <Button
      variant="primary"
      onclick={start}
      loading={busy}
      loadingLabel="Preparing…"
      disabled={!canDownload}>{includeSecrets ? 'Continue' : 'Download'}</Button
    >
  {/snippet}
</Modal>

{#if stage === 'step-up'}
  <StepUpDialog action="admin_config_backup" onConfirm={download} onCancel={close} />
{:else if stage === 'password'}
  <Modal open={true} title="Save your backup password" dismissible={false} width="560px">
    <p>
      Save this password now, somewhere that survives the loss of this installation. Istota does not
      keep it and cannot show it again.
    </p>
    <p class="backup-password">{password}</p>
    {#if summary}
      <p>
        The backup holds {summary.file_credentials}
        {summary.file_credentials === 1 ? 'deployment credential' : 'deployment credentials'} and
        {summary.inline_credentials}
        {summary.inline_credentials === 1
          ? 'credential held in config.toml'
          : 'credentials held in config.toml'}.
      </p>
      {#if !summary.secrets_dir_available}
        <p>
          This installation has no deployment secret files, so only credentials saved in config.toml
          are included.
        </p>
      {/if}
      {#if summary.omissions.length}
        <h3 class="micro-label">Not in the backup</h3>
        <ul class="omissions">
          {#each summary.omissions as omission, i (i)}
            <li><code>{omission.name}</code>: {omission.reason}</li>
          {/each}
        </ul>
      {/if}
    {/if}
    <p class="scope">
      {SCOPE}
      <a href={RUNBOOK_URL} target="_blank" rel="noopener noreferrer">Recovering an installation</a>
    </p>
    {#snippet footer()}
      <!-- A refusal is reported by copyText; the password stays on screen to copy by hand. -->
      <CopyButton
        variant="button"
        size="md"
        label="Copy password"
        copiedLabel="Password copied"
        text={password}
      />
      <Button variant="primary" onclick={close}>I saved it</Button>
    {/snippet}
  </Modal>
{/if}

<style>
  .backup-password {
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
    user-select: all;
  }

  .scope {
    font-weight: var(--weight-semibold);
  }

  .omissions {
    margin: 0;
    padding-left: var(--space-4);
    font-size: var(--text-sm);
  }
</style>
