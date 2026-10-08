<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getCredentialBackup,
    setCredentialBackup,
    type CredentialBackupSettings,
    type StepUpProof,
  } from '$lib/api';
  import { Button, Field, Input } from '$lib/components/ui';
  import SettingsCard from './SettingsCard.svelte';
  import StepUpDialog from './StepUpDialog.svelte';

  let settings = $state<CredentialBackupSettings | null>(null);
  let recipient = $state('');
  let confirming = $state(false);
  let pending: string | null = $state(null);
  let error = $state('');
  let active = true;
  onMount(() => {
    getCredentialBackup()
      .then((value) => {
        if (active) settings = value;
      })
      .catch((e) => {
        if (active) error = (e as Error).message;
      });
    return () => {
      active = false;
      recipient = '';
      pending = null;
    };
  });
  function start(value: string | null) {
    pending = value;
    confirming = true;
  }
  async function save(proof: StepUpProof) {
    const result = await setCredentialBackup(pending, proof);
    if (active && settings) {
      settings = { ...settings, recipient_suffix: result.recipient_suffix };
      recipient = '';
      pending = null;
    }
  }
</script>

<SettingsCard
  title="Scheduled backup"
  description="Encrypted credential backups saved to your exports/credential-backups folder."
>
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if settings}
    {#if !settings.available}<p class="hint">Ask the operator to enable credential backups.</p>{/if}
    {#if settings.recipient_suffix}
      <p>Backup key ending {settings.recipient_suffix}.</p>
    {:else}
      <p class="hint">
        Add an age public key to turn on scheduled backups. The private key stays with you.
      </p>
    {/if}
    {#if settings.interval <= 0}<p class="hint">
        The operator has disabled scheduled backups.
      </p>{/if}
    {#if settings.last_run}
      <p class="hint">
        Last backup: {settings.last_run.at} · {settings.last_run.outcome}{settings.last_run.reason
          ? ` (${settings.last_run.reason})`
          : ''}
      </p>
    {/if}
    <Field
      label="Age public key"
      hint="Paste the public recipient key that begins with age1. Keep the matching private key somewhere safe; you need it to decrypt a backup."
    >
      <Input
        id="credential-backup-key"
        bind:value={recipient}
        placeholder="age1…"
        disabled={confirming || !settings.available}
      />
    </Field>
    <div class="row">
      <Button
        variant="primary"
        size="sm"
        disabled={!settings.available || !recipient.trim() || confirming}
        onclick={() => start(recipient.trim())}>Save backup key</Button
      >
      {#if settings.recipient_suffix}
        <Button variant="secondary" size="sm" disabled={confirming} onclick={() => start(null)}
          >Turn off backups</Button
        >
      {/if}
    </div>
  {/if}
</SettingsCard>
{#if confirming}
  <StepUpDialog
    action="backup_recipient"
    onConfirm={save}
    onCancel={() => {
      confirming = false;
      pending = null;
    }}
    onComplete={() => {
      confirming = false;
    }}
  />
{/if}
