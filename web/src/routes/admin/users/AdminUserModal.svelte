<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AuthError,
    AdminUserWriteError,
    getAdminUser,
    updateAdminUser,
    resetAdminUserWhatsApp,
    setAdminUserIdentity,
    type AdminUserDetail,
    type AdminUserProfile,
  } from '$lib/api';
  import {
    Badge,
    Button,
    Chip,
    ConfirmDialog,
    Field,
    Input,
    Modal,
    Select,
    type SelectOption,
  } from '$lib/components/ui';
  import { formatRelative } from '$lib/dateFormat';
  import { changedProfileFields } from '$lib/profilePatch';
  import { parseListInput, profileListString } from '$lib/settings/listInput';
  import { ADMIN_MANAGED_BADGE, adminManagedHint } from '$lib/settings/managed';
  import { timezoneOptions } from '$lib/settings/timezones';
  import { signInStateLabel } from '$lib/admin/signInState';
  import { notifySuccess } from '$lib/stores/notices';

  /**
   * One user's settings, for an admin. Mounted for one open and unmounted on
   * close, like `CredentialFormModal`, so every field starts from the server.
   *
   * Two saves on purpose: the footer Save sends the changed fields to the
   * PATCH, and the Login section has its own button, because attaching or
   * changing a login email signs the user out and can send mail — side
   * effects a field save must never carry.
   */
  interface Props {
    userId: string;
    onClose: () => void;
    /** Something the users table shows may have changed. */
    onChanged: () => void;
    onSignedOut: () => void;
  }

  let { userId, onClose, onChanged, onSignedOut }: Props = $props();

  type Form = AdminUserProfile & { whatsapp_number: string };

  // Same rule the server applies; the server still decides.
  const E164 = /^\+[1-9][0-9]{7,14}$/;
  const POLICY_RANK: Record<string, number> = { off: 0, untrusted: 1, all: 2 };
  // `''` is bits-ui's "nothing picked" value, so the default gets a key of its own.
  const DEFAULT_POLICY = 'default';
  const WORKER_FIELDS = [
    ['max_foreground_workers', 'Foreground workers'],
    ['max_background_workers', 'Background workers'],
  ] as const;
  const zoneOptions = timezoneOptions();

  let open = $state(true);
  let detail = $state<AdminUserDetail | null>(null);
  let loadError = $state('');
  let form = $state<Form | null>(null);
  let snapshot = $state('');
  let busy = $state(false);
  let banner = $state('');
  let errors: Record<string, string> = $state({});
  let confirmDiscard = $state(false);
  let confirmReset = $state(false);

  let loginEmail = $state('');
  let loginInvite = $state(false);
  let addOverride = $state<boolean | null>(null);
  let loginBusy = $state(false);
  let loginError = $state('');
  let loginNote = $state('');

  function formFrom(d: AdminUserDetail): Form {
    return { ...d.profile, whatsapp_number: d.whatsapp.number };
  }

  /** Take a fresh server answer without losing edits the admin has not saved:
   *  a login save or a WhatsApp reset changes the stored record under an open
   *  form. */
  function adopt(next: AdminUserDetail, keepEdits: boolean) {
    const pending = keepEdits && form ? changedProfileFields(form, snapshot) : {};
    const stored = formFrom(next);
    detail = next;
    snapshot = JSON.stringify(stored);
    form = { ...stored, ...pending };
  }

  function resetLogin(d: AdminUserDetail) {
    loginEmail = d.identity?.email ?? '';
    loginInvite = !d.identity;
    addOverride = null;
  }

  function failed(e: unknown, fallback: string): string {
    if (e instanceof AuthError) {
      onSignedOut();
      return '';
    }
    return e instanceof Error && e.message ? e.message : fallback;
  }

  onMount(async () => {
    try {
      const d = await getAdminUser(userId);
      adopt(d, false);
      resetLogin(d);
    } catch (e) {
      loadError = failed(e, 'The user could not be loaded.');
    }
  });

  const patch = $derived(form ? changedProfileFields(form, snapshot) : {});
  const dirty = $derived(Object.keys(patch).length > 0);
  const storedLogin = $derived(detail?.identity?.email ?? '');
  const typedLogin = $derived(loginEmail.trim());
  const loginDirty = $derived(typedLogin !== '' && typedLogin.toLowerCase() !== storedLogin);
  const changingLogin = $derived(!!detail?.identity && loginDirty);
  const alreadyListed = $derived(
    !!detail?.profile.email_addresses.some((a) => a.toLowerCase() === typedLogin.toLowerCase()),
  );
  const addToAddresses = $derived(addOverride ?? !alreadyListed);

  const smsInvalid = $derived(!!form?.sms_phone_number && !E164.test(form.sms_phone_number));
  const whatsappInvalid = $derived(!!form?.whatsapp_number && !E164.test(form.whatsapp_number));
  const showSms = $derived(
    !!detail && (detail.options.sms_enabled || detail.profile.sms_phone_number !== ''),
  );
  const showWhatsApp = $derived(
    !!detail &&
      (detail.options.whatsapp_enabled ||
        detail.whatsapp.number !== '' ||
        detail.whatsapp.status !== 'unbound'),
  );
  const whatsappHasIdentity = $derived(
    detail?.whatsapp.status === 'enrolled' || detail?.whatsapp.status === 'opted_out',
  );

  const locked = (field: string) => !!detail?.managed.includes(field);
  const badge = (field: string) => (locked(field) ? ADMIN_MANAGED_BADGE : undefined);
  const hint = (field: string, otherwise?: string) =>
    locked(field) ? adminManagedHint(userId, field) : otherwise;

  const policyOptions = $derived.by((): SelectOption[] => {
    if (!detail) return [];
    const floor = detail.options.outbound_approval_floor;
    const stored = detail.profile.outbound_approval;
    const labels: Record<string, string> = {
      '': `Deployment default (${floor})`,
      off: 'Off: send without approval',
      untrusted: 'Approve mail to untrusted recipients',
      all: 'Approve all outgoing mail',
    };
    return detail.options.outbound_approval.map((value) => ({
      value: value || DEFAULT_POLICY,
      label: labels[value] ?? value,
      // Below the floor is refused, unless it is what is already stored.
      disabled: value !== '' && value !== stored && POLICY_RANK[value] < POLICY_RANK[floor],
    }));
  });

  const whatsappStatus = $derived.by(() => {
    const w = detail?.whatsapp;
    if (!w) return { label: '', variant: 'neutral' as const };
    if (w.status === 'opted_out') return { label: 'Opted out', variant: 'warn' as const };
    if (w.status === 'awaiting_first_message')
      return { label: 'Waiting for first message', variant: 'info' as const };
    if (w.status === 'enrolled') {
      const provider = w.provider?.includes('baileys')
        ? ' · Baileys'
        : w.provider?.includes('cloud')
          ? ' · Cloud API'
          : '';
      return { label: `Enrolled${provider}`, variant: 'success' as const };
    }
    return { label: 'Unbound', variant: 'neutral' as const };
  });

  function toggle(list: string[], name: string): string[] {
    return list.includes(name) ? list.filter((n) => n !== name) : [...list, name];
  }

  function requestClose() {
    if (busy || loginBusy) {
      open = true;
      return;
    }
    if (dirty || loginDirty) {
      open = true;
      confirmDiscard = true;
      return;
    }
    open = false;
    onClose();
  }

  function discard() {
    confirmDiscard = false;
    open = false;
    onClose();
  }

  async function save() {
    if (!form || busy || !dirty || smsInvalid || whatsappInvalid) return;
    busy = true;
    banner = '';
    errors = {};
    const body = { ...patch };
    if (typeof body.sms_phone_number === 'string')
      body.sms_phone_number = body.sms_phone_number.trim();
    if (typeof body.whatsapp_number === 'string')
      body.whatsapp_number = body.whatsapp_number.trim();
    try {
      await updateAdminUser(userId, body);
      notifySuccess(`Saved settings for ${detail?.profile.display_name || userId}.`);
      onChanged();
      open = false;
      onClose();
    } catch (e) {
      if (e instanceof AdminUserWriteError && e.fields.length) {
        errors = Object.fromEntries(e.fields.map((f) => [f, e.message]));
      } else {
        banner = failed(e, 'The settings could not be saved.');
      }
    } finally {
      busy = false;
    }
  }

  async function refresh() {
    try {
      adopt(await getAdminUser(userId), true);
    } catch {
      // The error the caller is already showing is the one that matters.
    }
  }

  async function saveLogin() {
    if (!detail || loginBusy || !loginDirty) return;
    loginBusy = true;
    loginError = loginNote = '';
    try {
      const next = await setAdminUserIdentity(userId, {
        email: typedLogin,
        invite: loginInvite,
        add_to_addresses: addToAddresses,
      });
      adopt(next, true);
      resetLogin(next);
      loginNote =
        next.addresses_skipped === 'managed'
          ? 'Login email saved. It was not added to the email addresses, which are set by the deployment.'
          : 'Login email saved.';
      onChanged();
    } catch (e) {
      loginError = failed(e, 'The login email could not be saved.');
      // A failed invitation still saved the identity (the 502 says so).
      await refresh();
      onChanged();
    } finally {
      loginBusy = false;
    }
  }

  async function resetWhatsApp() {
    confirmReset = false;
    banner = '';
    try {
      adopt(await resetAdminUserWhatsApp(userId), true);
    } catch (e) {
      banner = failed(e, 'The WhatsApp identity could not be reset.');
    }
  }
</script>

<Modal
  bind:open
  title={detail ? `Settings for ${detail.profile.display_name || userId}` : 'User settings'}
  width="560px"
  onOpenChange={(next) => {
    if (!next) requestClose();
  }}
>
  {#if loadError}
    <p class="banner error" role="alert">{loadError}</p>
  {:else if !detail || !form}
    <p class="caption">Loading…</p>
  {:else}
    <div class="editor">
      {#if banner}<p class="banner error" role="alert">{banner}</p>{/if}

      <fieldset class="editor-fields" disabled={busy}>
        <section class="editor-section" aria-labelledby="admin-user-identity">
          <h3 id="admin-user-identity" class="micro-label">Identity</h3>
          <Field label="User ID">
            <Input value={userId} readonly monospace />
          </Field>
          {#if detail.is_admin}
            <p class="admin-line">
              <Badge size="sm">Admin</Badge><span class="caption">Set in the admins file.</span>
            </p>
          {/if}
          <Field
            label="Display name"
            badge={badge('display_name')}
            hint={hint('display_name')}
            error={errors.display_name}
          >
            <Input
              bind:value={form.display_name}
              disabled={locked('display_name')}
              invalid={!!errors.display_name}
            />
          </Field>
          <Field
            label="Timezone (IANA)"
            labelled={false}
            badge={badge('timezone')}
            hint={hint('timezone', 'Used until the user sets their own.')}
            error={errors.timezone}
          >
            <Select
              value={form.timezone || 'UTC'}
              options={zoneOptions}
              ariaLabel="Timezone"
              fullWidth
              disabled={locked('timezone')}
              onValueChange={(v) => {
                if (form) form.timezone = v;
              }}
            />
          </Field>
        </section>

        <fieldset
          class="editor-section login-fields"
          disabled={loginBusy}
          aria-labelledby="admin-user-login"
        >
          <h3 id="admin-user-login" class="micro-label">Login</h3>
          <p class="login-state">
            {#if detail.identity}
              <span>{detail.identity.email}</span>
              <span class="caption"
                >{signInStateLabel(detail.identity, detail.identity.state)} · last login {detail
                  .identity.last_login_at
                  ? formatRelative(detail.identity.last_login_at)
                  : 'never'}</span
              >
            {:else}
              <span class="caption">{signInStateLabel(null, undefined)}: no login email.</span>
            {/if}
          </p>
          {#if detail.options.email_login_enabled}
            <Field
              label="Login email"
              warning={changingLogin
                ? 'Changing the login email signs this user out everywhere and voids outstanding links.'
                : undefined}
              error={loginError || undefined}
            >
              <Input
                type="email"
                bind:value={loginEmail}
                autocomplete="off"
                invalid={!!loginError}
              />
            </Field>
            <Field label="Also add to email addresses" checkbox>
              <input
                type="checkbox"
                checked={addToAddresses}
                disabled={alreadyListed}
                onchange={(e) => (addOverride = (e.currentTarget as HTMLInputElement).checked)}
              />
            </Field>
            <Field label="Send invitation" checkbox>
              <input type="checkbox" bind:checked={loginInvite} />
            </Field>
            {#if loginNote}<p class="banner success" role="status">{loginNote}</p>{/if}
            <div class="form-actions">
              <Button
                variant="secondary"
                size="sm"
                onclick={saveLogin}
                loading={loginBusy}
                disabled={!loginDirty}
                >{detail.identity ? 'Change login email' : 'Attach login email'}</Button
              >
            </div>
            <p class="caption">
              Invitations, password resets, signing out, disabling and removing the login are in the
              user’s row menu.
            </p>
          {:else}
            <p class="caption">Email sign-in is not enabled, so no login email can be attached.</p>
          {/if}
        </fieldset>

        <section class="editor-section" aria-labelledby="admin-user-email">
          <h3 id="admin-user-email" class="micro-label">Email</h3>
          <Field
            label="Email addresses (comma-separated)"
            badge={badge('email_addresses')}
            hint={hint('email_addresses', 'Inbound mail from these addresses is this user’s.')}
            error={errors.email_addresses}
          >
            <Input
              value={profileListString(form.email_addresses)}
              disabled={locked('email_addresses')}
              invalid={!!errors.email_addresses}
              oninput={(e) => {
                if (form)
                  form.email_addresses = parseListInput(
                    (e.currentTarget as HTMLInputElement).value,
                  );
              }}
            />
          </Field>
          <Field
            label="Trusted senders (fnmatch patterns, comma-separated)"
            badge={badge('trusted_email_senders')}
            hint={hint('trusted_email_senders')}
            error={errors.trusted_email_senders}
          >
            <Input
              value={profileListString(form.trusted_email_senders)}
              disabled={locked('trusted_email_senders')}
              invalid={!!errors.trusted_email_senders}
              oninput={(e) => {
                if (form)
                  form.trusted_email_senders = parseListInput(
                    (e.currentTarget as HTMLInputElement).value,
                  );
              }}
            />
          </Field>
          <Field
            label="Quiet senders (filed silently, comma-separated)"
            badge={badge('quiet_email_senders')}
            hint={hint('quiet_email_senders')}
            error={errors.quiet_email_senders}
          >
            <Input
              value={profileListString(form.quiet_email_senders)}
              disabled={locked('quiet_email_senders')}
              invalid={!!errors.quiet_email_senders}
              oninput={(e) => {
                if (form)
                  form.quiet_email_senders = parseListInput(
                    (e.currentTarget as HTMLInputElement).value,
                  );
              }}
            />
          </Field>
          <Field
            label="Outbound approval"
            labelled={false}
            badge={badge('outbound_approval')}
            hint={hint(
              'outbound_approval',
              `The deployment floor is ${detail.options.outbound_approval_floor}; looser values are not available.`,
            )}
            error={errors.outbound_approval}
          >
            <Select
              value={form.outbound_approval || DEFAULT_POLICY}
              options={policyOptions}
              ariaLabel="Outbound approval"
              fullWidth
              disabled={locked('outbound_approval')}
              onValueChange={(v) => {
                if (form)
                  form.outbound_approval = (
                    v === DEFAULT_POLICY ? '' : v
                  ) as AdminUserProfile['outbound_approval'];
              }}
            />
          </Field>
        </section>

        {#if showSms || showWhatsApp}
          <section class="editor-section" aria-labelledby="admin-user-phone">
            <h3 id="admin-user-phone" class="micro-label">Phone</h3>
            {#if showSms}
              <Field
                label="SMS number"
                badge={badge('sms_phone_number')}
                hint={hint(
                  'sms_phone_number',
                  'Full international form, for example +48123456789.',
                )}
                error={errors.sms_phone_number ??
                  (smsInvalid ? 'Use the full international form, starting with +.' : undefined)}
              >
                <Input
                  bind:value={form.sms_phone_number}
                  type="tel"
                  autocomplete="off"
                  placeholder="+48123456789"
                  disabled={locked('sms_phone_number')}
                  invalid={smsInvalid || !!errors.sms_phone_number}
                />
              </Field>
            {/if}
            {#if showWhatsApp}
              <Field
                label="WhatsApp number"
                labelled={false}
                badge={badge('whatsapp_number')}
                hint={hint('whatsapp_number', 'Full international form, for example +48123456789.')}
                warning={whatsappHasIdentity && form.whatsapp_number !== detail.whatsapp.number
                  ? 'Changing the number discards the current enrollment, the open service window and any opt-out.'
                  : undefined}
                error={errors.whatsapp_number ??
                  (whatsappInvalid
                    ? 'Use the full international form, starting with +.'
                    : undefined)}
              >
                <div class="inline-control">
                  <Input
                    bind:value={form.whatsapp_number}
                    type="tel"
                    aria-label="WhatsApp number"
                    autocomplete="off"
                    placeholder="+48123456789"
                    disabled={locked('whatsapp_number')}
                    invalid={whatsappInvalid || !!errors.whatsapp_number}
                  />
                  {#if showSms}
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={locked('whatsapp_number') || !form.sms_phone_number}
                      onclick={() => {
                        if (form) form.whatsapp_number = form.sms_phone_number;
                      }}>Same as SMS</Button
                    >
                  {/if}
                </div>
              </Field>
              <div class="whatsapp-status">
                <Badge size="sm" variant={whatsappStatus.variant}>{whatsappStatus.label}</Badge>
                {#if detail.whatsapp.identity}
                  <span class="caption mono">{detail.whatsapp.identity}</span>
                {/if}
                {#if detail.whatsapp.last_seen_at}
                  <span class="caption"
                    >Last seen {formatRelative(detail.whatsapp.last_seen_at)}</span
                  >
                {/if}
                {#if whatsappHasIdentity}
                  <Button variant="ghost" size="sm" onclick={() => (confirmReset = true)}
                    >Reset identity</Button
                  >
                {/if}
              </div>
            {/if}
          </section>
        {/if}

        <section class="editor-section" aria-labelledby="admin-user-access">
          <h3 id="admin-user-access" class="micro-label">Access and limits</h3>
          {#if detail.options.modules.length}
            <Field
              label="Disabled modules"
              labelled={false}
              badge={badge('disabled_modules')}
              hint={hint('disabled_modules', 'Modules are on by default. Pick one to turn it off.')}
              error={errors.disabled_modules}
            >
              <div class="chip-set">
                {#each detail.options.modules as name (name)}
                  {@const on = form.disabled_modules.includes(name)}
                  <Chip
                    checked={on}
                    aria-pressed={on}
                    disabled={locked('disabled_modules')}
                    onclick={() => {
                      if (form) form.disabled_modules = toggle(form.disabled_modules, name);
                    }}>{name}</Chip
                  >
                {/each}
              </div>
            </Field>
          {/if}
          {#if detail.options.skills.length}
            <Field
              label="Disabled skills"
              labelled={false}
              badge={badge('disabled_skills')}
              hint={hint('disabled_skills', 'Pick a skill to turn it off for this user.')}
              error={errors.disabled_skills}
            >
              <div class="chip-set">
                {#each detail.options.skills as name (name)}
                  {@const on = form.disabled_skills.includes(name)}
                  <Chip
                    checked={on}
                    aria-pressed={on}
                    disabled={locked('disabled_skills')}
                    onclick={() => {
                      if (form) form.disabled_skills = toggle(form.disabled_skills, name);
                    }}>{name}</Chip
                  >
                {/each}
              </div>
            </Field>
          {/if}
          {#each WORKER_FIELDS as [field, label] (field)}
            <Field
              {label}
              badge={badge(field)}
              hint={hint(field, 'Leave empty for the deployment default.')}
              error={errors[field]}
            >
              <Input
                type="number"
                min="0"
                step="1"
                placeholder="Deployment default"
                value={form[field] || ''}
                disabled={locked(field)}
                invalid={!!errors[field]}
                oninput={(e) => {
                  const raw = (e.currentTarget as HTMLInputElement).value;
                  if (form)
                    form[field] = raw === '' ? 0 : Math.max(0, Math.trunc(Number(raw)) || 0);
                }}
              />
            </Field>
          {/each}
          <Field
            label="Seed the default briefings"
            checkbox
            badge={badge('default_briefings')}
            hint={hint('default_briefings', 'Applies when the user’s briefings are first seeded.')}
          >
            <input
              type="checkbox"
              bind:checked={form.default_briefings}
              disabled={locked('default_briefings')}
            />
          </Field>
        </section>

        <section class="editor-section" aria-labelledby="admin-user-channels">
          <h3 id="admin-user-channels" class="micro-label">Channels</h3>
          <dl class="kv">
            <dt class="muted">Log channel</dt>
            <dd class:mono={!!detail.channels.log_channel}>
              {detail.channels.log_channel || 'Not provisioned'}
            </dd>
            <dt class="muted">Alerts channel</dt>
            <dd class:mono={!!detail.channels.alerts_channel}>
              {detail.channels.alerts_channel || 'Not provisioned'}
            </dd>
          </dl>
        </section>
      </fieldset>
    </div>
  {/if}

  {#snippet footer()}
    <Button variant="ghost" onclick={requestClose} disabled={busy}>Cancel</Button>
    <Button
      variant="primary"
      onclick={save}
      loading={busy}
      disabled={!dirty || smsInvalid || whatsappInvalid}>Save</Button
    >
  {/snippet}
</Modal>

<ConfirmDialog
  bind:open={confirmReset}
  title="Reset WhatsApp identity"
  message="Keeps the number. The next message from it enrolls again. Use this when the person moved the number to a new phone."
  confirmLabel="Reset identity"
  onConfirm={resetWhatsApp}
/>

<ConfirmDialog
  bind:open={confirmDiscard}
  title="Discard changes"
  message={`Are you sure? Your unsaved changes to ${detail?.profile.display_name || userId} will be lost.`}
  confirmLabel="Discard"
  onConfirm={discard}
/>

<style>
  .editor,
  .editor-fields,
  .login-fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .editor-section {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .editor-section + .editor-section {
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
  }

  .editor h3,
  .editor p {
    margin: 0;
  }

  .admin-line,
  .whatsapp-status,
  .inline-control,
  .chip-set {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .inline-control {
    flex-wrap: nowrap;
  }

  .login-state {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--text-sm);
  }

  .mono {
    font-family: var(--font-mono);
  }
</style>
