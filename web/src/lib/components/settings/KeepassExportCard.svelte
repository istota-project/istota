<script lang="ts">
  import { onDestroy } from 'svelte';
  import { exportKeepass, type StepUpProof } from '$lib/api';
  import { downloadBlob, generateKeyfile } from '$lib/keepass';
  import { formatDate } from '@minot/ui/dateFormat';
  import { Button, Field, Modal, Card, Checkbox, CopyButton } from '@minot/ui';
  import StepUpDialog from './StepUpDialog.svelte';

  let { onExported }: { onExported?: () => void } = $props();
  let useKeyfile = $state(false);
  let keyfile: string | null = $state(null);
  let confirming = $state(false);
  let password = $state('');
  let busy = $state(false);
  let error = $state('');
  let active = true;
  let pending: AbortController | null = null;

  function clear() {
    pending?.abort();
    pending = null;
    keyfile = null;
    password = '';
    confirming = false;
    busy = false;
  }
  onDestroy(() => {
    active = false;
    clear();
  });

  async function start() {
    busy = true;
    error = '';
    try {
      keyfile = useKeyfile ? await generateKeyfile() : null;
      if (!active) {
        clear();
        return;
      }
      if (keyfile) {
        const date = formatDate(new Date().toISOString(), {
          locale: 'en-CA',
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
        });
        downloadBlob(
          new Blob([keyfile], { type: 'application/xml' }),
          `istota-export-${date}.keyx`,
        );
      }
      confirming = true;
    } catch (e) {
      error = (e as Error).message;
      clear();
    } finally {
      busy = false;
    }
  }

  async function download(proof: StepUpProof) {
    const controller = new AbortController();
    pending = controller;
    const result = await exportKeepass(keyfile, proof, controller.signal);
    if (!active || controller.signal.aborted) {
      result.password = result.file = '';
      return;
    }
    password = result.password;
    result.password = '';
    keyfile = null;
    const bytes = Uint8Array.from(atob(result.file), (char) => char.charCodeAt(0));
    result.file = '';
    downloadBlob(new Blob([bytes], { type: 'application/octet-stream' }), result.filename);
  }
</script>

<Card
  title="Export credentials"
  description="Download a fresh KeePass file containing your credentials, OTP seeds and recovery codes."
>
  <Field
    label="Also require a key file"
    checkbox
    hint="Adds a separate file that must be present to unlock the export, along with its password."
  >
    <Checkbox bind:checked={useKeyfile} disabled={busy || confirming || !!password} />
  </Field>
  {#if useKeyfile}<p class="description">
      Save the key file when it downloads. You will need both it and the export password to open the
      KeePass file.
    </p>{/if}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  <div class="hstack wrap">
    <Button
      variant="primary"
      size="sm"
      loading={busy}
      disabled={confirming || !!password}
      onclick={start}>Export credentials</Button
    >
  </div>
</Card>

{#if confirming}
  <StepUpDialog
    action="export"
    onConfirm={download}
    onCancel={clear}
    onComplete={() => {
      confirming = false;
      onExported?.();
    }}
  />
{:else if password}
  <Modal open={true} title="Save your export password" dismissible={false}>
    <p>
      Save this password now, for example in KeePassXC or on paper. Istota does not keep it and
      cannot show it again.
    </p>
    <p class="export-password">{password}</p>
    {#snippet footer()}
      <!-- A refusal is reported by copyText; the password stays on screen to copy by hand. -->
      <CopyButton
        variant="button"
        size="md"
        label="Copy password"
        copiedLabel="Password copied"
        text={password}
      />
      <Button variant="primary" onclick={clear}>I saved it</Button>
    {/snippet}
  </Modal>
{/if}

<style>
  .export-password {
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
    user-select: all;
  }
</style>
