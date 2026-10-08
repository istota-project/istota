<script lang="ts">
  import { onDestroy } from 'svelte';
  import { exportKeepass, type StepUpProof } from '$lib/api';
  import { downloadBlob, generateKeyfile } from '$lib/keepass';
  import { Button, Modal } from '$lib/components/ui';
  import SettingsCard from './SettingsCard.svelte';
  import StepUpDialog from './StepUpDialog.svelte';

  let { onExported }: { onExported?: () => void } = $props();
  let useKeyfile = $state(false);
  let keyfile: string | null = $state(null);
  let confirming = $state(false);
  let password = $state('');
  let busy = $state(false);
  let error = $state('');
  let copied = $state(false);
  let active = true;
  let pending: AbortController | null = null;

  function clear() {
    pending?.abort();
    pending = null;
    keyfile = null;
    password = '';
    copied = false;
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
        const date = new Date().toLocaleDateString('en-CA');
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

  async function copy() {
    try {
      await navigator.clipboard.writeText(password);
      copied = true;
    } catch {
      error = 'Could not copy. Select the password and copy it yourself.';
    }
  }
</script>

<SettingsCard
  title="Export credentials"
  description="Download a fresh KeePass file containing your credentials, OTP seeds and recovery codes."
>
  <label class="checkbox-label"
    ><input type="checkbox" bind:checked={useKeyfile} disabled={busy || confirming || !!password} /> Also
    require a key file</label
  >
  {#if useKeyfile}<p class="hint">
      Save the key file when it downloads. You will need both it and the export password to open the
      KeePass file.
    </p>{/if}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  <Button variant="primary" loading={busy} disabled={confirming || !!password} onclick={start}
    >Export credentials</Button
  >
</SettingsCard>

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
      <Button variant="secondary" onclick={copy}>{copied ? 'Copied' : 'Copy password'}</Button>
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
