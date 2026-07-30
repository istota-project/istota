<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { getModuleServices, monarchLogin, type ServiceCard as ServiceCardData } from '$lib/api';
  import {
    getBusinessSettings,
    createEntity,
    updateEntity,
    deleteEntity,
    createService,
    updateService,
    deleteService,
    ApiError,
    type EntityRow,
    type ServiceRow,
    type EntityInput,
    type ServiceInput,
    type BusinessDefaults,
  } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import { ServiceCard, SettingsLayout, SettingsCard } from '$lib/components/settings';
  import {
    Button,
    ConfirmDialog,
    HintPopover,
    KebabMenu,
    NoticeBanner,
    type KebabItem,
  } from '$lib/components/ui';
  import EntityForm from '$lib/components/money/EntityForm.svelte';
  import ServiceForm from '$lib/components/money/ServiceForm.svelte';

  let loading = $state(true);
  let error = $state('');

  let moduleServices: ServiceCardData[] = $state([]);
  let moduleEnabled = $state(true);

  let entities: EntityRow[] = $state([]);
  let services: ServiceRow[] = $state([]);
  let defaults: BusinessDefaults | null = $state(null);
  let businessError = $state('');

  // Programmatic-login form state. Plain bindings — values are POSTed to
  // /money/monarch/login and never persisted in the browser beyond the
  // in-memory component state.
  let loginEmail = $state('');
  let loginPassword = $state('');
  let loginMfa = $state('');
  let loginBusy = $state(false);
  let loginMessage = $state('');
  let loginErrorKind = $state<
    '' | 'auth' | 'mfa' | 'cloudflare' | 'captcha' | 'other' | 'challenge'
  >('');
  // A pending one-time-code step. Non-empty means the password was *accepted*
  // and Monarch is waiting on a code, which is a different thing from a failed
  // login and has to look different.
  let loginChallenge = $state<'' | 'email_otp' | 'mfa'>('');
  let loginCode = $state('');

  async function loadServices() {
    const mod = await getModuleServices('money');
    moduleServices = mod.services;
    moduleEnabled = mod.module_enabled;
  }

  async function loadBusiness() {
    try {
      const resp = await getBusinessSettings();
      entities = resp.entities;
      services = resp.services;
      defaults = resp.defaults;
      businessError = '';
    } catch (e) {
      businessError = e instanceof Error ? e.message : 'Failed to load business settings';
    }
  }

  async function refresh() {
    loading = true;
    error = '';
    try {
      await Promise.all([loadServices(), loadBusiness()]);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load settings';
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  $effect(() => {
    $selectedLedger;
    void loadBusiness();
  });

  function resetLogin() {
    loginChallenge = '';
    loginCode = '';
    loginPassword = '';
    loginMfa = '';
    loginMessage = '';
    loginErrorKind = '';
  }

  async function submitLogin() {
    if (!loginEmail || !loginPassword) return;
    if (loginChallenge && !loginCode) return;
    loginBusy = true;
    loginMessage = '';
    loginErrorKind = '';
    // On the code step the field carries whichever challenge is live; before
    // it, the optional MFA box lets someone with an authenticator skip a round
    // trip. Sending both would be wrong — they are different credentials.
    const codes =
      loginChallenge === 'email_otp'
        ? { emailOtp: loginCode }
        : loginChallenge === 'mfa'
          ? { mfaTotp: loginCode }
          : { mfaTotp: loginMfa };
    try {
      const result = await monarchLogin(loginEmail, loginPassword, codes);
      if (result.status === 'ok') {
        resetLogin();
        loginMessage = 'Logged in — session_id and csrftoken saved.';
        loginErrorKind = '';
        await loadServices();
        return;
      }
      if (result.status === 'challenge') {
        // Re-issuing the same challenge means the code we just sent was
        // refused. Say so, rather than silently re-rendering an identical
        // form that looks like nothing happened.
        const retry = loginChallenge === result.kind;
        loginChallenge = result.kind;
        loginCode = '';
        loginErrorKind = retry ? 'auth' : 'challenge';
        loginMessage = retry
          ? result.kind === 'email_otp'
            ? 'That code was not accepted. Codes expire — check for a newer email and try again.'
            : 'That code was not accepted. Wait for your authenticator to show a new one.'
          : result.kind === 'email_otp'
            ? `Email and password accepted. Monarch emailed a 6-digit code to ${loginEmail} — enter it below to finish.`
            : 'Email and password accepted. Enter the 6-digit code from your authenticator app to finish.';
        return;
      }
      // A real failure: drop the code step, since the password itself is now
      // in question and re-sending a code against it would only waste one.
      loginChallenge = '';
      loginCode = '';
      loginErrorKind = result.kind === 'blocked' ? 'other' : result.kind;
      loginMessage = result.message;
    } catch (e) {
      loginChallenge = '';
      loginCode = '';
      loginErrorKind = 'other';
      loginMessage = e instanceof Error ? e.message : 'Login failed';
    } finally {
      loginBusy = false;
    }
  }

  function formatRate(rate: number): string {
    return rate.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function typeLabel(t: string): string {
    const labels: Record<string, string> = {
      hours: 'per hour',
      days: 'per day',
      flat: 'flat rate',
      other: 'variable',
    };
    return labels[t] || t;
  }

  // --- Entity + service editing ---
  //
  // A refused delete (409) gets its own banner rather than being folded into
  // the card's error: the server's reason names records the user has to go
  // look at — the clients still pointing at an entity, or the work entries
  // still naming a service.
  let entityFormOpen = $state(false);
  let editingEntity: EntityRow | null = $state(null);
  let entityFormError = $state('');
  let entitySaving = $state(false);
  let entityNotice = $state('');

  let serviceFormOpen = $state(false);
  let editingService: ServiceRow | null = $state(null);
  let serviceFormError = $state('');
  let serviceSaving = $state(false);
  let serviceNotice = $state('');

  let confirmOpen = $state(false);
  let pendingDelete: { kind: 'entity' | 'service'; key: string; label: string } | null =
    $state(null);

  function openEntityForm(entity: EntityRow | null) {
    editingEntity = entity;
    entityFormError = '';
    entityFormOpen = true;
  }

  function openServiceForm(service: ServiceRow | null) {
    editingService = service;
    serviceFormError = '';
    serviceFormOpen = true;
  }

  async function saveEntity(key: string, data: EntityInput) {
    entitySaving = true;
    entityFormError = '';
    try {
      if (editingEntity) await updateEntity(key, data);
      else await createEntity(key, data);
      entityFormOpen = false;
      editingEntity = null;
      await loadBusiness();
    } catch (e) {
      entityFormError = e instanceof Error ? e.message : 'Failed to save entity';
    } finally {
      entitySaving = false;
    }
  }

  async function saveService(key: string, data: ServiceInput) {
    serviceSaving = true;
    serviceFormError = '';
    try {
      if (editingService) await updateService(key, data);
      else await createService(key, data);
      serviceFormOpen = false;
      editingService = null;
      await loadBusiness();
    } catch (e) {
      serviceFormError = e instanceof Error ? e.message : 'Failed to save service';
    } finally {
      serviceSaving = false;
    }
  }

  function askDelete(kind: 'entity' | 'service', key: string, label: string) {
    pendingDelete = { kind, key, label };
    confirmOpen = true;
  }

  async function handleDelete() {
    const target = pendingDelete;
    confirmOpen = false;
    pendingDelete = null;
    if (!target) return;

    entityNotice = '';
    serviceNotice = '';
    try {
      if (target.kind === 'entity') await deleteEntity(target.key);
      else await deleteService(target.key);
      await loadBusiness();
    } catch (e) {
      const msg = e instanceof Error ? e.message : `Failed to delete ${target.kind}`;
      const refused = e instanceof ApiError && e.status === 409;
      if (target.kind === 'entity') entityNotice = msg;
      else serviceNotice = msg;
      if (!refused) businessError = msg;
    }
  }

  const deleteMessage = $derived.by(() => {
    if (!pendingDelete) return '';
    if (pendingDelete.kind === 'entity') {
      return (
        `Are you sure you want to delete ${pendingDelete.label}? ` +
        'Clients that bill under it, and the default entity, are protected — ' +
        'the delete is refused rather than silently rebilling under another entity.'
      );
    }
    return (
      `Are you sure you want to delete ${pendingDelete.label}? ` +
      'A service any work entry still names cannot be deleted, because it would ' +
      'unbill that work and shrink the totals of invoices that already went out.'
    );
  });

  function entityMenu(entity: EntityRow): KebabItem[] {
    return [
      { label: 'Edit', onSelect: () => openEntityForm(entity) },
      {
        label: 'Delete',
        onSelect: () => askDelete('entity', entity.key, entity.name || entity.key),
        danger: true,
      },
    ];
  }

  function serviceMenu(svc: ServiceRow): KebabItem[] {
    return [
      { label: 'Edit', onSelect: () => openServiceForm(svc) },
      {
        label: 'Delete',
        onSelect: () => askDelete('service', svc.key, svc.display_name || svc.key),
        danger: true,
      },
    ];
  }
</script>

<SettingsLayout
  title="Money settings"
  description="Monarch credentials and business configuration. Secrets are encrypted at rest and never sent back to the browser."
  {loading}
  {error}
>
  {#if !moduleEnabled}
    <div class="banner info">
      Money module is disabled. Enable it in
      <a href="{base}/settings">Settings → Preferences</a> to manage Monarch credentials and invoicing.
    </div>
  {:else}
    {#each moduleServices as svc (svc.service)}
      {#if svc.service === 'monarch'}
        <div class="monarch-help">
          <h3>Connect to Monarch Money</h3>
          <p>
            Monarch's API requires browser session cookies. Pick the method that works for your
            account:
          </p>

          <details open>
            <summary>
              <span class="summary-label">
                Option A — Login with email and password
                <HintPopover
                  label="About logging in with email and password"
                  text="We sign in to Monarch on your behalf and store the session cookies it returns (session_id and csrftoken). Your password is used once and is never written to disk. If Monarch doesn't recognise this device it emails you a 6-digit code — enter it when asked. If Cloudflare blocks the request from this server, fall back to Option B."
                />
              </span>
            </summary>
            <form
              class="login-form"
              onsubmit={(e) => {
                e.preventDefault();
                void submitLogin();
              }}
            >
              <label>
                <span>Email</span>
                <input
                  type="email"
                  bind:value={loginEmail}
                  autocomplete="off"
                  disabled={loginBusy}
                  required
                />
              </label>
              <label>
                <span>Password</span>
                <input
                  type="password"
                  bind:value={loginPassword}
                  autocomplete="off"
                  disabled={loginBusy}
                  required
                />
              </label>
              {#if loginChallenge}
                <label>
                  <span>
                    {loginChallenge === 'email_otp' ? 'Emailed code' : 'Authenticator code'}
                  </span>
                  <!-- svelte-ignore a11y_autofocus -->
                  <input
                    type="text"
                    inputmode="numeric"
                    pattern="[0-9]*"
                    maxlength="6"
                    bind:value={loginCode}
                    autocomplete="one-time-code"
                    disabled={loginBusy}
                    placeholder="6-digit code"
                    autofocus
                  />
                </label>
              {:else}
                <label>
                  <span>MFA code <small>(if MFA enabled)</small></span>
                  <input
                    type="text"
                    inputmode="numeric"
                    pattern="[0-9]*"
                    bind:value={loginMfa}
                    autocomplete="off"
                    disabled={loginBusy}
                    placeholder="6-digit code"
                  />
                </label>
              {/if}
              <div class="login-actions">
                <Button
                  variant="primary"
                  size="sm"
                  disabled={loginBusy ||
                    !loginEmail ||
                    !loginPassword ||
                    (!!loginChallenge && !loginCode)}
                  type="submit"
                >
                  {loginBusy
                    ? loginChallenge
                      ? 'Verifying…'
                      : 'Logging in…'
                    : loginChallenge
                      ? 'Verify code'
                      : 'Login & save cookies'}
                </Button>
                {#if loginChallenge}
                  <Button variant="ghost" size="sm" disabled={loginBusy} onclick={resetLogin}>
                    Start over
                  </Button>
                {/if}
              </div>
              {#if loginMessage}
                <div class="login-status" data-kind={loginErrorKind || 'ok'}>
                  {loginMessage}
                </div>
              {/if}
            </form>
          </details>

          <details>
            <summary>Option B — Paste cookies from your browser</summary>
            <p class="hint">
              Use this when programmatic login is blocked by Cloudflare (common on cloud-hosted
              Istota deploys).
            </p>
            <ol>
              <li>
                Open <a href="https://app.monarch.com" target="_blank" rel="noopener noreferrer"
                  >app.monarch.com</a
                > in a logged-in browser tab.
              </li>
              <li>
                Open DevTools (Cmd/Ctrl+Option+I) → <strong>Application</strong> →
                <strong>Cookies</strong>
                → <code>https://api.monarch.com</code>.
              </li>
              <li>Copy the value of <code>session_id</code> into the field below.</li>
              <li>Copy the value of <code>csrftoken</code> into the field below.</li>
              <li>Click <strong>Save</strong>.</li>
            </ol>
          </details>

          <p class="legacy-note">
            Cookies are the only credential we store. They last months on a trusted-device login.
          </p>
        </div>
      {/if}
      <ServiceCard service={svc} onChanged={loadServices} />
    {/each}

    <SettingsCard title="Business defaults">
      {#if businessError}
        <div class="banner error">{businessError}</div>
      {:else if !defaults}
        <p class="empty">No invoicing configuration found.</p>
      {:else}
        <dl class="kv">
          <dt>Currency</dt>
          <dd>{defaults.currency}</dd>
          <dt>Default entity</dt>
          <dd>{defaults.default_entity}</dd>
          <dt>A/R account</dt>
          <dd><code>{defaults.default_ar_account}</code></dd>
          <dt>Bank account</dt>
          <dd><code>{defaults.default_bank_account}</code></dd>
          <dt>Invoice output</dt>
          <dd><code>{defaults.invoice_output}</code></dd>
          <dt>Next invoice #</dt>
          <dd>{defaults.next_invoice_number}</dd>
          {#if defaults.days_until_overdue > 0}
            <dt>Days until overdue</dt>
            <dd>{defaults.days_until_overdue}</dd>
          {/if}
          {#if defaults.notifications}
            <dt>Notifications</dt>
            <dd>{defaults.notifications}</dd>
          {/if}
        </dl>
      {/if}
    </SettingsCard>

    <!-- Outside the `{#if defaults}` guard on purpose: a user with no
         invoicing configuration has to be able to create the first entity
         and the first service from here. -->
    <SettingsCard title="Entities ({entities.length})">
      {#snippet actions()}
        <Button variant="primary" size="sm" onclick={() => openEntityForm(null)}>Add entity</Button>
      {/snippet}
      {#if entityNotice}
        <NoticeBanner title={entityNotice} variant="warn" />
      {/if}
      {#if entities.length === 0}
        <p class="empty">No entities yet — add the one that bills your clients.</p>
      {:else}
        <div class="entity-grid card-grid">
          {#each entities as entity (entity.key)}
            <div class="entity">
              <div class="entity-head">
                <span>{entity.name}</span>
                <span class="entity-key">
                  <code>{entity.key}</code>
                  <KebabMenu items={entityMenu(entity)} ariaLabel="Entity actions" />
                </span>
              </div>
              <dl class="kv compact">
                {#if entity.email}
                  <dt>Email</dt>
                  <dd>{entity.email}</dd>
                {/if}
                {#if entity.address}
                  <dt>Address</dt>
                  <dd class="pre">{entity.address}</dd>
                {/if}
                {#if entity.currency}
                  <dt>Currency</dt>
                  <dd>{entity.currency}</dd>
                {/if}
                {#if entity.ar_account}
                  <dt>A/R</dt>
                  <dd><code>{entity.ar_account}</code></dd>
                {/if}
                {#if entity.bank_account}
                  <dt>Bank</dt>
                  <dd><code>{entity.bank_account}</code></dd>
                {/if}
                {#if entity.payment_instructions}
                  <dt>Payment</dt>
                  <dd class="pre">{entity.payment_instructions}</dd>
                {/if}
                {#if entity.logo}
                  <dt>Logo</dt>
                  <dd><code>{entity.logo}</code></dd>
                {/if}
              </dl>
            </div>
          {/each}
        </div>
      {/if}
    </SettingsCard>

    <SettingsCard title="Services ({services.length})">
      {#snippet actions()}
        <Button variant="primary" size="sm" onclick={() => openServiceForm(null)}>
          Add service
        </Button>
      {/snippet}
      {#if serviceNotice}
        <NoticeBanner title={serviceNotice} variant="warn" />
      {/if}
      {#if services.length === 0}
        <p class="empty">No services yet — add what you bill for.</p>
      {:else}
        <div class="table-scroll">
          <table class="grid">
            <thead>
              <tr>
                <th>Service</th>
                <th>Type</th>
                <th class="num">Rate</th>
                <th>Income account</th>
                <th class="actions" aria-label="Actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each services as svc (svc.key)}
                <tr>
                  <td>
                    {svc.display_name}
                    <span class="muted"> <code>{svc.key}</code></span>
                  </td>
                  <td class="muted">{typeLabel(svc.type)}</td>
                  <td class="num">
                    {svc.type === 'other' ? '—' : `$${formatRate(svc.rate)}`}
                  </td>
                  <td class="muted"><code>{svc.income_account || '—'}</code></td>
                  <td class="actions">
                    <KebabMenu items={serviceMenu(svc)} ariaLabel="Service actions" />
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </SettingsCard>
  {/if}
</SettingsLayout>

{#if entityFormOpen}
  <EntityForm
    entity={editingEntity}
    onSave={saveEntity}
    onCancel={() => {
      entityFormOpen = false;
      editingEntity = null;
    }}
    error={entityFormError}
    saving={entitySaving}
  />
{/if}

{#if serviceFormOpen}
  <ServiceForm
    service={editingService}
    onSave={saveService}
    onCancel={() => {
      serviceFormOpen = false;
      editingService = null;
    }}
    error={serviceFormError}
    saving={serviceSaving}
  />
{/if}

<ConfirmDialog
  bind:open={confirmOpen}
  title={pendingDelete?.kind === 'entity' ? 'Delete entity' : 'Delete service'}
  message={deleteMessage}
  confirmLabel="Delete"
  onConfirm={handleDelete}
  onCancel={() => (pendingDelete = null)}
/>

<style>
  /* Shared .settings/.card/.field/.grid/.banner primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only money-specific
	   styling (kv, entity grid, numeric column tweaks) stays. */

  .kv {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.25rem 0.75rem;
    margin: 0;
    font-size: var(--text-sm);
  }

  .kv.compact {
    gap: 0.15rem 0.6rem;
    font-size: var(--text-xs);
  }

  .kv dt {
    color: var(--text-dim);
  }

  .kv dd {
    margin: 0;
    color: var(--text-secondary);
    word-break: break-word;
  }

  .kv dd.pre {
    white-space: pre-line;
  }

  .entity-grid {
    --card-min: 220px;
    --card-gap: 0.6rem;
  }

  .entity {
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: 0.5rem 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .entity-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.5rem;
    font-weight: 600;
    color: var(--text-primary);
    font-size: var(--text-sm);
  }

  .entity-key {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    font-weight: 400;
    color: var(--text-dim);
    font-size: var(--text-xs);
  }

  /* Money's services table sizes by content; shared .settings .grid uses
	   fixed layout, so opt back to auto here. */
  .grid {
    table-layout: auto;
  }

  .monarch-help {
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: 0.75rem 1rem;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  .monarch-help h3 {
    margin: 0 0 0.4rem;
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  .monarch-help p,
  .monarch-help ol {
    margin: 0.25rem 0;
  }

  .monarch-help ol {
    padding-left: 1.25rem;
  }

  .monarch-help li {
    margin: 0.1rem 0;
  }

  .monarch-help code {
    background: var(--surface-raised);
    padding: 0 0.25rem;
    border-radius: 0.2rem;
    font-size: 0.92em;
  }

  .monarch-help .legacy-note {
    margin-top: 0.5rem;
    color: var(--text-dim);
    font-size: var(--text-xs);
  }

  .monarch-help details {
    margin: 0.4rem 0;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: 0.5rem 0.75rem;
    background: var(--surface-raised);
  }

  .monarch-help summary {
    cursor: pointer;
    font-weight: 600;
    color: var(--text-primary);
  }

  /* The hint trigger rides inside the <summary> so it sits beside the label
     rather than below the disclosure. `display: inline-flex` on a wrapper —
     not on the summary itself, which would drop the disclosure triangle in
     some engines — keeps the "?" aligned to the text baseline box. */
  .monarch-help .summary-label {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }

  .monarch-help .hint {
    color: var(--text-dim);
    font-size: var(--text-xs);
    margin: 0.3rem 0;
  }

  .login-form {
    display: grid;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .login-form label {
    display: grid;
    grid-template-columns: 120px 1fr;
    gap: 0.5rem;
    align-items: center;
    font-size: var(--text-sm);
  }

  .login-form label span {
    color: var(--text-dim);
  }

  .login-form input {
    font: inherit;
    padding: 0.35rem 0.5rem;
    border-radius: var(--radius-sm, 0.3rem);
    border: 1px solid var(--border-default);
    background: var(--surface-base);
    color: var(--text-primary);
  }

  .login-actions {
    display: flex;
    justify-content: flex-end;
  }

  .login-status {
    font-size: var(--text-xs);
    padding: 0.35rem 0.5rem;
    border-radius: var(--radius-sm, 0.3rem);
    border: 1px solid var(--border-default);
  }

  .login-status[data-kind='ok'] {
    color: var(--status-success-fg);
    border-color: var(--status-success-bg);
  }

  .login-status[data-kind='auth'],
  .login-status[data-kind='other'] {
    color: var(--text-secondary);
    border-color: var(--border-default);
  }

  .login-status[data-kind='mfa'],
  .login-status[data-kind='cloudflare'] {
    color: var(--text-secondary);
    background: var(--surface-base);
  }

  /* A pending code step is progress, not a problem — it reads as info so a
     user isn't told their correct password failed. */
  .login-status[data-kind='challenge'] {
    color: var(--status-info-fg);
    border-color: var(--status-info-bg);
  }

  .grid th.num,
  .grid td.num {
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .grid th.actions,
  .grid td.actions {
    width: 1.5rem;
    text-align: right;
  }
</style>
