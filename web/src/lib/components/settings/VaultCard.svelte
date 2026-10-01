<script lang="ts">
  import { onMount } from 'svelte';
  // The tree's own date rendering. A local `toLocaleString` here was a second
  // copy of it, and `dateFormat.test.ts`'s drift guard is what said so — by
  // name, in the default suite. `formatRelative` rather than `formatDateTime`:
  // this stamp answers "is it keeping up", which a relative reading says
  // directly, and it falls back to an absolute date past its own threshold for
  // the vault nobody has edited in a month.
  import { formatRelative } from '$lib/dateFormat';
  import { getVaultStatus, selectVaultFile, setVaultPassphrase, type VaultStatus } from '$lib/api';
  import { Button, ConfirmDialog, Field, Select, type SelectOption } from '$lib/components/ui';
  import { notifySuccess } from '$lib/stores/notices';
  import SettingsCard from './SettingsCard.svelte';
  import SecretField from './SecretField.svelte';

  // null = this user has no credential vault, which is the default for
  // everyone, and also what an unreachable endpoint resolves to. Both render
  // nothing: a heading that always says something is a heading every user has
  // to read past.
  let vault: VaultStatus | null = $state(null);
  // The whole response, including the form's half — which is present for a user
  // with no vault, where `vault` above is deliberately null.
  let vaultForm: VaultStatus | null = $state(null);

  // The failing sentence, or empty when the vault is working. Computed by the
  // server: the precedence between a live finding and a recorded one is a rule,
  // and restating it here was a second copy of it — one that compared against
  // the literal `'ok'`, hardcoding a Python constant in TypeScript with nothing
  // holding the two in step.
  // `.by` rather than the expression form: a bare `$derived(vault?.problem)` is
  // narrowed by control-flow analysis to the `null` the state was initialised
  // with, since every assignment to `vault` is further down the file. The
  // closure defers the read and keeps the declared type.
  let vaultProblem = $derived.by(() => vault?.problem ?? '');

  // The count and its noun as one string rather than two nodes. Prettier is
  // free to reflow the markup, and a line break landing between `{count}` and
  // the word after it puts a newline inside the sentence — invisible on screen
  // and the reason a test asserting on the rendered text saw `2\n credentials`.
  let vaultSharedCount = $derived.by(() => {
    const n = vault?.entry_count ?? 0;
    return `${n} shared credential${n === 1 ? '' : 's'}`;
  });

  async function refreshVault() {
    try {
      const status = await getVaultStatus();
      // Two pieces of state off one response, and the split is the same one the
      // server makes. `vault` is the *status line*, which renders only for a
      // configured vault — a heading that always says something is a heading
      // every user reads past for the life of the deployment. `vaultForm` is
      // the *form*, which renders whenever this surface may write, including
      // for the user who has no vault at all: that user is the one the form
      // exists for.
      vault = status && status.configured ? status : null;
      vaultForm = status ?? null;
    } catch {
      vault = null;
      vaultForm = null;
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
  // Off `vault` rather than `vaultForm`: the names ride on the configured half
  // of the payload, which is also the half the status line renders from.
  let vaultDir = $derived.by(() => vaultForm?.vault_dir ?? '');
  let vaultFiles = $derived.by(() => vaultForm?.files ?? []);
  let vaultFile = $derived.by(() => vaultForm?.vault_file ?? '');

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
  // vault is an optional per-user feature nobody has by default, so a
  // deployment where this endpoint is unreachable must not be a settings page
  // that fails to load — which is the same as the ordinary unconfigured case.
  onMount(refreshVault);
</script>

<!--
  Renders for a user with *no* vault, which is the state it exists to move
  them out of. What it deliberately does not offer is an absolute-path field:
  an absolute path is checked against the trees a sandbox binds rather than
  against this user's own directory, which is the right question for a path
  an operator wrote into config.toml and not a line a user may put themselves
  on the far side of.
-->
{#if vaultForm}
  <SettingsCard
    title="Credential vault"
    description="Credentials you want your own tasks to be able to use — a token for a service istota has no integration with, a device password, anything a script needs. Keep them in a KeePassXC file: istota reads it and never writes to it, so it stays yours to edit on any device."
  >
    {#snippet status()}
      <!--
        The server's own verdict, not a proxy for it. `source` says where
        the *selection* came from and is empty for a folder vault, which is
        now the ordinary way to have one — read as the pill it said "Not
        set up" over a vault that was working.
      -->
      <span class="status-pill status-{vaultConfigured ? 'configured' : 'missing'}">
        {vaultConfigured ? 'Configured' : 'Not set up'}
      </span>
    {/snippet}

    <div class="vault-form" data-testid="vault-form">
      <!--
        What the vault currently holds, and where it is read from.

        This used to sit on the Connected services heading, and the comment
        that put it there gave the reason: it was the referent that each
        vault-managed service card's disabled-field sentence pointed at, so
        it had to be visible from all of them. The vault owns no service
        card's fields any more and disables none of them, so that reason
        went with them and the line was left describing the vault from
        outside the only card about the vault.
      -->
      {#if vault}
        <!--
          A `div`, not a `p`. This began as one sentence of prose and is
          now several statements plus a disclosure holding a list, and a
          `ul` or a `p` inside a `p` is closed by the parser before it —
          which SSR then reports as a hydration mismatch rather than as
          the markup error it is. `.hint` is typography only, so it
          carries over unchanged.
        -->
        <div class="hint vault" data-testid="vault-status">
          <!--
            What the vault is the authority for is its own namespace of
            shared credentials, not a list of connected services. It used to
            name the services it overwrote; it overwrites none of them now,
            so that sentence was false on every card that rendered it and its
            empty-list fallback ("no services are assigned to it yet") was
            false on the rest.
          -->
          <p class="vault-line">
            {#if (vault.entry_count ?? 0) > 0}
              istota holds {vaultSharedCount} from this file, and the file is the authority for all of
              them — removing an entry removes the credential.
            {:else}
              Nothing has been shared from this file yet.
            {/if}
          </p>
          <p class="vault-line">
            Istota created {vault.generated_count ?? 0} credential{(vault.generated_count ?? 0) ===
            1
              ? ''
              : 's'} in <code>generated/</code>.
          </p>
          <!--
            The scope notice. A file with no top-level `istota` group is read
            in full, which is how it is meant to work for a file put in the
            vault folder for istota and is not what somebody who copied their
            everyday password database in wants. It is a notice rather than a
            refusal, so it says what happened and what to do, and it renders
            only when a cycle has actually read the file that way.
          -->
          {#if vault.unscoped}
            <p class="vault-line vault-problem" data-testid="vault-unscoped">
              This file has no top-level <code>istota</code> group, so all
              {vault.entry_count ?? 0} credential{(vault.entry_count ?? 0) === 1 ? '' : 's'} in it are
              shared with your tasks. If that was not what you meant, move the file out or put what you
              meant to share under a top-level group named <code>istota</code>.
            </p>
          {/if}
          {#if vaultProblem}
            <p class="vault-line vault-problem">Not working: {vaultProblem}</p>
          {:else if vault.last_success_at}
            <!--
              "applied", not "synced". Istota re-reads the file only when its
              bytes change, so a vault nobody has edited for three weeks
              reports a three-week-old timestamp and is working perfectly —
              calling that "last synced" reads as staleness and sends a user
              looking for a fault that is not there.
            -->
            <p class="vault-line">
              Istota last applied it {formatRelative(vault.last_success_at)}, and re-reads it
              whenever the file changes.
            </p>
          {:else}
            <p class="vault-line">Nothing has been applied from it yet.</p>
          {/if}
        </div>
      {/if}
      <!--
        Only the *file* half is withheld when something outranks it. The
        passphrase is the user's own either way — it is a credential Istota
        holds to open their file, not a setting an operator made — and
        withholding it left a user whose vault came from the form this
        replaced with no way to store one at all.
      -->
      <!--
        Precedence rather than cause. The server answers `editable: false`
        for a configured `vault_path` and also on its two fail-closed arms
        — no config loaded, and a lookup that raised — where naming the
        deployment's configuration would send the user to an administrator
        for a line that does not exist. Saying which file is live, and that
        this page cannot change it, is true in all three.
      -->
      {#if !vaultEditable}
        <p class="caption" data-testid="vault-not-selectable">
          Your credential vault's file is set outside this page, so it is not selectable here. Ask
          your administrator if it needs to change.
        </p>
      {/if}
      <!--
          The hint is what stops Generate reading as a write to a file the
          card has just called read-only. Istota reads the *file*; the
          master password is a credential Istota has to *hold* in order to
          open it, which is a different thing.

          Behind the "?" rather than inline, matching how Preferences
          carries the same kind of explanation. It is the one place on this
          card where that is the right slot: the field's own label already
          says what to type, and this is the background behind it.
        -->
      <SecretField
        label="Master password"
        hint="The password your KeePassXC file is encrypted with. Istota stores it so it can open the file — it is never written back to the file, and cannot be shown to you again. If you have not made the file yet, generate one here and use it as the master password when you create it."
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
        <Button variant="ghost" size="sm" onclick={generateVaultPassphrase} disabled={vaultBusy}>
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
        <p class="vault-minted" data-testid="vault-minted">
          <strong>Copy this now — it will not be shown again:</strong>
          <code>{mintedPassphrase}</code>
        </p>
      {/if}

      <!--
          The file half, and it is a folder plus a name rather than a path.
          There is nothing to type: the user drops their KeePassXC file into
          the folder named below and the server offers what it found. With
          one file there is no question to ask, which is why the dropdown is
          absent for it and a line of prose says which file is being read.

          Withheld when a configured path outranks it, which the sentence
          above has already explained — offering a choice the server would
          refuse is a control that does nothing.

          `warning`, not `hint`: a hint renders behind a hover "?" and is
          discoverable rather than seen, and web/AGENTS.md's rule is that
          nothing the user has to act on goes there. Putting the file
          somewhere is the action.
        -->
      {#if !vaultEditable}
        <!-- nothing: the sentence at the top of the card says why -->
      {:else if vaultFiles.length === 0}
        <p class="caption vault-folder" data-testid="vault-folder">
          {#if vaultDir}
            Put your KeePassXC file in <code>{vaultDir}</code> and it will show up here.
          {:else}
            Istota cannot reach your files on this deployment, so the vault file is an administrator
            setting.
          {/if}
        </p>
      {:else if vaultFiles.length === 1}
        <p class="caption vault-folder" data-testid="vault-folder">
          Reading <code>{vaultFiles[0]}</code> from <code>{vaultDir}</code>.
        </p>
      {:else}
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
      {#if vaultError}
        <p class="banner error" data-testid="vault-error">{vaultError}</p>
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
  /* A second paragraph under the same heading, separated from the first rather
     than styled apart from it: it is the same kind of statement about the same
     card list. `.hint` carries the size and colour. */
  .vault {
    margin-top: var(--space-2);
  }

  .vault code {
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    font-size: 0.9em;
    color: var(--text-muted);
    /* A resolved filesystem path has no break opportunities of its own, so on a
       phone it would otherwise push the whole heading block sideways. */
    overflow-wrap: anywhere;
  }

  /* Three separate claims, not one paragraph: what is shared, whether the
     whole file is shared, and when it last applied. Run together they read as
     a wall with a coloured clause in the middle of it, which is what the
     scope notice looked like. One block each, and the gap is what separates
     the notice from the prose around it. */
  .vault-line {
    margin: 0;
  }

  .vault-line + .vault-line {
    margin-top: var(--space-2);
  }

  /* The one part of this block that is not neutral prose. Colour alone would
     not carry it — the sentence says what happened in words. */
  .vault-problem {
    color: var(--status-warn-fg);
  }

  .vault-form :global(.micro-label) {
    margin: 0;
  }

  .vault-form {
    margin-top: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  /* A folder path has no break opportunities of its own, so on a phone it
     would push the card sideways. Same rule the heading's `.vault code` uses. */
  .vault-folder code {
    overflow-wrap: anywhere;
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
    padding: var(--space-2);
    border: 1px solid var(--status-warn-fg);
    border-radius: var(--radius-sm);
    background: var(--surface-raised);
  }

  .vault-minted code {
    display: block;
    margin-top: var(--space-1);
    /* No break opportunities of its own, and it must be selectable whole. */
    overflow-wrap: anywhere;
    user-select: all;
  }
</style>
