<script lang="ts">
  import { unlockSetup, SetupApiError } from '$lib/api';
  import { Button, Field, Input } from '$lib/components/ui';

  interface Props {
    /** Under the field: why the last code was refused, the address's included. */
    error?: string;
    onUnlocked: () => void;
  }

  let { error = $bindable(''), onUnlocked }: Props = $props();

  let code = $state('');
  let busy = $state(false);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    if (!code.trim()) {
      error = 'Enter your setup code.';
      return;
    }
    busy = true;
    try {
      await unlockSetup(code);
      error = '';
      onUnlocked();
    } catch (e) {
      error = e instanceof SetupApiError ? e.message : "Can't reach the setup service.";
    } finally {
      busy = false;
    }
  }
</script>

<form class="unlock" onsubmit={submit} novalidate>
  <p>
    Setup printed a code where it started: in the terminal where it was run, or on your server's
    console. Type it here to continue.
  </p>
  <Field label="Setup code" {error}>
    <Input
      bind:value={code}
      monospace
      autocomplete="one-time-code"
      autocapitalize="characters"
      spellcheck={false}
      placeholder="XXXXX-XXXXX-XXXXX-XXXXX"
      invalid={!!error}
    />
  </Field>
  <details class="where">
    <summary>Where do I find this?</summary>
    <p>
      On the server, run <code>sudo istota-stack setup-code</code> to print it again. If that says
      no setup is in progress, start one with <code>sudo istota-stack setup --web</code>, which
      prints a new code. A code works once: after it has opened setup in one browser, another
      browser needs a new one.
    </p>
  </details>
  <div class="form-actions">
    <Button variant="primary" type="submit" loading={busy} loadingLabel="Checking…">Continue</Button
    >
  </div>
</form>

<style>
  .unlock {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .where summary {
    width: fit-content;
    color: var(--link);
    cursor: pointer;
  }

  .where p {
    margin-top: var(--space-2);
    color: var(--text-muted);
  }
</style>
