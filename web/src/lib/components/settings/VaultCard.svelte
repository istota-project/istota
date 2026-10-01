<script lang="ts">
  import { onMount } from 'svelte';
  // The tree's own date rendering. A local `toLocaleString` here was a second
  // copy of it, and `dateFormat.test.ts`'s drift guard is what said so — by
  // name, in the default suite. `formatRelative` rather than `formatDateTime`:
  // this stamp answers "is it keeping up", which a relative reading says
  // directly, and it falls back to an absolute date past its own threshold for
  // the vault nobody has edited in a month.
  import { formatRelative } from '$lib/dateFormat';
  import { copyText } from '$lib/clipboard';
  import { getVaultStatus, selectVaultFile, setVaultPassphrase, type VaultStatus } from '$lib/api';
  import { Button, ConfirmDialog, Field, Select, type SelectOption } from '$lib/components/ui';
  import { notifySuccess } from '$lib/stores/notices';
  import SettingsCard from './SettingsCard.svelte';
  import SecretField from './SecretField.svelte';

  // null = this user has no KeePassXC sync, which is the default for everyone,
  // and also what an unreachable endpoint resolves to. The status line renders
  // for neither.
  let vault: VaultStatus | null = $state(null);
  // The whole response, including the form's half — which is present for a user
  // with no vault, where `vault` above is deliberately null.
  let vaultForm: VaultStatus | null = $state(null);

  // The failing sentence, or empty when the vault is working. Computed by the
  // server: the precedence between a live finding and a recorded one is a rule,
  // and restating it here would be a second copy of it.
  // `.by` rather than the expression form: a bare `$derived(vault?.problem)` is
  // narrowed by control-flow analysis to the `null` the state was initialised
  // with, since every assignment to `vault` is further down the file. The
  // closure defers the read and keeps the declared type.
  let vaultProblem = $derived.by(() => vault?.problem ?? '');

  // The setup panel. Closed by default, and opened on the first load when the
  // sync has a problem, since fixing it is what the panel is for. Later
  // refreshes leave it as the user set it.
  let panelOpen = $state(false);
  let panelSeeded = false;

  async function refreshVault() {
    try {
      const status = await getVaultStatus();
      vault = status && status.configured ? status : null;
      vaultForm = status ?? null;
    } catch {
      vault = null;
      vaultForm = null;
    }
    if (!panelSeeded && vaultForm) {
      panelOpen = !!vault?.problem;
      panelSeeded = true;
    }
  }

  // Emptied on every successful save and on every navigation away: it is a
  // credential, and leaving it in a reactive variable keeps it in the page for
  // as long as the tab is open.
  let mintedPassphrase = $state('');
  let vaultBusy = $state(false);
  let vaultError = $state('');
  let passphraseInput = $state('');

  let vaultEditable = $derived.by(() => vaultForm?.editable ?? false);
  let vaultConfigured = $derived.by(() => vaultForm?.configured ?? false);
  let vaultHasPassphrase = $derived.by(() => vaultForm?.passphrase_present ?? false);
  let vaultDir = $derived.by(() => vaultForm?.vault_dir ?? '');
  let vaultFiles = $derived.by(() => vaultForm?.files ?? []);
  let vaultFile = $derived.by(() => vaultForm?.vault_file ?? '');

  /** The file the status line names: the chosen one, else the configured path's last part. */
  let syncedFile = $derived.by(() => {
    if (vault?.vault_file) return vault.vault_file;
    const path = vault?.path ?? '';
    return path.split('/').pop() || path;
  });

  let nameConflicts = $derived.by(() => vault?.name_conflicts ?? 0);
  // One string rather than template text, so a prettier line break cannot land
  // inside the sentence.
  let nameConflictLine = $derived.by(() => {
    const n = nameConflicts;
    const which = n === 1 ? '1 entry in the file was' : `${n} entries in the file were`;
    const [it, ones] = n === 1 ? ['it', 'the one'] : ['them', 'the ones'];
    return `${which} skipped because a credential with the same name was added in Istota. Rename ${it} in KeePassXC, or delete ${ones} added in Istota; skipped entries are synced the next time the file changes.`;
  });

  /**
   * The dropdown's options, plus one for "nothing chosen".
   *
   * The empty entry is what the user picks to undo a choice: with several files
   * and none stored the server asks the question again, which is the state the
   * card is for. It is absent when there is only one file, where there is no
   * question to ask and no choice to undo.
   */
  let vaultFileOptions: SelectOption[] = $derived.by(() => {
    const options = vaultFiles.map((name) => ({ value: name, label: name }));
    return vaultFiles.length > 1 ? [{ value: '', label: 'Choose a file…' }, ...options] : options;
  });

  /**
   * Store the choice and re-read the status.
   *
   * A filename rather than a path, validated by the server against the listing
   * it just produced — so there is no rule for this side to restate and no
   * refusal it could anticipate. What it does anticipate is the *save* failing,
   * which is reported in the card rather than swallowed.
   */
  async function chooseVaultFile(name: string) {
    if (name === vaultFile) return;
    vaultBusy = true;
    vaultError = '';
    try {
      await selectVaultFile(name);
      await refreshVault();
      notifySuccess(name ? `Reading ${name}` : 'Vault file choice cleared');
    } catch (e) {
      vaultError = (e as Error).message || 'Could not save the vault file';
      // The dropdown is bound to the server's answer, so a refused choice has
      // to be put back rather than left showing a selection nothing stored.
      await refreshVault();
    } finally {
      vaultBusy = false;
    }
  }

  let confirmingVaultReplace = $state(false);

  /**
   * Generate, or ask first when there is one to destroy.
   *
   * The server refuses a generate over an existing passphrase without
   * `replace`, on the same reasoning `istota secret ensure --generate` refuses
   * one without `--force`: minting a second value destroys the only copy this
   * deployment has of the one the KDBX is already encrypted under, and the
   * vault then fails to open until the user re-keys the file by hand. So this
   * is the page's one irreversible action, and it takes a `ConfirmDialog` like
   * every other.
   */
  function generateVaultPassphrase() {
    if (vaultHasPassphrase) {
      confirmingVaultReplace = true;
      return;
    }
    return saveVaultPassphrase(true);
  }

  function replaceVaultPassphrase() {
    return saveVaultPassphrase(true, true);
  }

  function saveTypedVaultPassphrase() {
    return saveVaultPassphrase(false);
  }

  async function saveVaultPassphrase(generate: boolean, replace = false) {
    vaultBusy = true;
    vaultError = '';
    mintedPassphrase = '';
    try {
      const resp = await setVaultPassphrase(
        generate ? { generate: true, replace } : { passphrase: passphraseInput },
      );
      // Shown once, here, because nothing reads it back — the user needs it to
      // open their own KDBX. The typed path returns an empty string, since the
      // client already has that value.
      mintedPassphrase = resp.generated ?? '';
      passphraseInput = '';
      await refreshVault();
    } catch (e) {
      vaultError = (e as Error).message || 'Could not store the passphrase';
    } finally {
      vaultBusy = false;
    }
  }

  // Its own request, and a failure renders nothing rather than an error. The
  // sync is an optional per-user feature nobody has by default, so a
  // deployment where this endpoint is unreachable must not be a settings page
  // that fails to load.
  onMount(refreshVault);
</script>

<!--
  Renders for a user with *no* vault, which is the state the setup panel
  exists to move them out of. What it deliberately does not offer is an
  absolute-path field: an absolute path is checked against the trees a sandbox
  binds rather than against this user's own directory, which is the right
  question for a path an operator wrote into config.toml and not a line a user
  may put themselves on the far side of.
-->
{#if vaultForm}
  <SettingsCard title="KeePassXC sync">
    {#snippet status()}
      {#if vault}
        <!-- The server's own verdict, not a proxy for it. -->
        <span
          class="status-pill status-{vaultProblem ? 'partial' : 'configured'}"
          data-testid="vault-pill"
        >
          {vaultProblem ? 'Needs attention' : 'Working'}
        </span>
      {/if}
    {/snippet}
    {#snippet actions()}
      <Button variant="ghost" size="sm" onclick={() => (panelOpen = !panelOpen)}>
        {vaultConfigured ? 'Manage' : 'Set up'}
      </Button>
    {/snippet}

    <div class="vault-card" data-testid="vault-card">
      {#if vault}
        <p class="hint vault-intro">
          Istota syncs the entries in this file and adds the ones tasks create to its
          <code>generated</code> group.
        </p>
        <!--
          A `div`, not a `p`: it holds several statements, and a `p` inside a
          `p` is closed by the parser before it, which SSR then reports as a
          hydration mismatch rather than as the markup error it is.
        -->
        <div class="vault-status" data-testid="vault-status">
          {#if vaultProblem}
            <p class="caption vault-problem">Not working: {vaultProblem}</p>
          {:else if vault.last_success_at}
            <!--
              "updated" is when the file was last applied. Istota re-reads it
              only when its bytes change, so an old stamp on a file nobody has
              edited is a working sync, not a stale one.
            -->
            <p class="caption">
              Syncing <code>{syncedFile}</code> · updated {formatRelative(vault.last_success_at)}.
            </p>
          {:else}
            <p class="caption">Syncing <code>{syncedFile}</code> · nothing applied yet.</p>
          {/if}
          <!--
            A file with no top-level `istota` group is read in full, which is
            how it is meant to work for a file made for Istota and is not what
            somebody who copied their everyday password database in wants. It
            renders only once a cycle has read the file that way.
          -->
          {#if vault.unscoped}
            <p class="caption vault-problem" data-testid="vault-unscoped">
              This file has no top-level <code>istota</code> group, so every entry in it is shared
              with your tasks. To share only some, move them into a group named <code>istota</code>.
            </p>
          {/if}
          {#if nameConflicts > 0}
            <p class="caption vault-problem" data-testid="vault-conflicts">{nameConflictLine}</p>
          {/if}
        </div>
      {:else}
        <p class="hint vault-intro">
          Keep credentials in a KeePassXC file instead? Istota can sync them from a file in your
          files.
        </p>
      {/if}

      {#if panelOpen}
        <div class="vault-panel" id="vault-panel" data-testid="vault-panel">
          <section class="vault-step">
            <h3 class="micro-label">1. Master password</h3>
            <SecretField
              label="Master password"
              hint="The password your file is encrypted with. Istota keeps it to open the file and cannot show it again."
              configured={vaultHasPassphrase}
              value={passphraseInput}
              disabled={vaultBusy}
              onValueChange={(next) => (passphraseInput = next)}
            />
            <div class="vault-actions control-row">
              <Button
                variant="secondary"
                size="sm"
                onclick={saveTypedVaultPassphrase}
                loading={vaultBusy}
                disabled={!passphraseInput}
              >
                Save password
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onclick={generateVaultPassphrase}
                disabled={vaultBusy}
              >
                {vaultHasPassphrase ? 'Generate a new one' : 'Generate one for me'}
              </Button>
            </div>
            {#if mintedPassphrase}
              <!--
                The one place this value is ever rendered. There is no route
                that reads it back, so it is here or nowhere — which is why it
                is a bordered block rather than a line of prose, and why it is
                not a `notify()`: a transient banner that expires takes the
                only copy with it.
              -->
              <div class="vault-minted" data-testid="vault-minted">
                <p><strong>Copy this now — it will not be shown again:</strong></p>
                <code>{mintedPassphrase}</code>
                <Button
                  variant="secondary"
                  size="sm"
                  onclick={() => copyText(mintedPassphrase, { label: 'Password copied' })}
                >
                  Copy
                </Button>
              </div>
            {/if}
          </section>

          <section class="vault-step">
            <h3 class="micro-label">2. File</h3>
            <!--
              Precedence rather than cause. The server answers `editable: false`
              for a configured `vault_path` and also on its two fail-closed arms,
              and in all three the file is not this page's to choose.
            -->
            {#if !vaultEditable}
              <p class="caption" data-testid="vault-not-selectable">
                Your administrator sets this file.
              </p>
            {:else if vaultFiles.length === 0}
              <p class="caption vault-folder" data-testid="vault-folder">
                {#if vaultDir}
                  Put your KeePassXC file in <code>{vaultDir}</code> and it will show up here.
                {:else}
                  Istota cannot reach your files on this deployment, so the file is an administrator
                  setting.
                {/if}
              </p>
            {:else if vaultFiles.length === 1}
              <p class="caption vault-folder" data-testid="vault-folder">
                Reading <code>{vaultFiles[0]}</code> from <code>{vaultDir}</code>.
              </p>
            {:else}
              <!-- `warning`, not `hint`: choosing a file is the action. -->
              <Field
                label="Vault file"
                warning="There is more than one file in your vault folder, so Istota needs to know which one to read."
                wide
              >
                <Select
                  value={vaultFile}
                  options={vaultFileOptions}
                  disabled={vaultBusy}
                  fullWidth
                  ariaLabel="Vault file"
                  onValueChange={chooseVaultFile}
                />
              </Field>
              <p class="caption vault-folder" data-testid="vault-folder">
                From <code>{vaultDir}</code>
              </p>
            {/if}
          </section>
          {#if vaultError}
            <p class="banner error" data-testid="vault-error">{vaultError}</p>
          {/if}
        </div>
      {/if}
      <ConfirmDialog
        bind:open={confirmingVaultReplace}
        title="Replace the vault password"
        message="Are you sure? Your vault file is encrypted with the password Istota already has, and generating a new one does not re-encrypt it — the vault will stop opening until you set the new password on the file yourself in KeePassXC."
        confirmLabel="Replace it"
        confirmVariant="danger"
        onConfirm={replaceVaultPassphrase}
      />
    </div>
  </SettingsCard>
{/if}

<style>
  .vault-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .vault-card p {
    margin: 0;
  }

  .vault-card code {
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    font-size: 0.9em;
    color: var(--text-muted);
    /* A path or a file name has no break opportunities of its own, so on a
       phone it would otherwise push the card sideways. */
    overflow-wrap: anywhere;
  }

  .vault-status {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  /* Colour alone would not carry it — the sentence says what happened in words. */
  .vault-problem {
    color: var(--status-warn-fg);
  }

  .vault-panel {
    margin-top: var(--space-2);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .vault-step {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .vault-step .micro-label {
    margin: 0;
  }

  .vault-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  /* The minted passphrase. Deliberately loud: it is shown once and there is no
     route that shows it again, so a user who scrolls past it has lost it. */
  .vault-minted {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-2);
    border: 1px solid var(--status-warn-fg);
    border-radius: var(--radius-sm);
    background: var(--surface-raised);
  }

  .vault-minted code {
    display: block;
    /* No break opportunities of its own, and it must be selectable whole. */
    overflow-wrap: anywhere;
    user-select: all;
  }
</style>
