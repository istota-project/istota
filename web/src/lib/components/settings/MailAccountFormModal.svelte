<script lang="ts">
  import { untrack } from 'svelte';
  import {
    AuthError,
    createMailAccount,
    updateMailAccount,
    type MailAccount,
    type MailAccountInput,
    type MailSecurity,
  } from '$lib/api';
  import {
    Checkbox,
    Field,
    FormGrid,
    FormModal,
    FormStack,
    Input,
    SecretField,
    Select,
  } from '$lib/components/ui';
  let {
    account = null,
    allowSend,
    onClose,
    onSaved,
    onError,
  }: {
    account?: MailAccount | null;
    /** `[mail_accounts] allow_send`: off, every account is read only. */
    allowSend: boolean;
    onClose: () => void;
    onSaved: () => void;
    onError: (e: unknown) => void;
  } = $props();
  const start = untrack(() => account);
  let open = $state(true);
  let busy = $state(false);
  let errors: Record<string, string> = $state({});

  const securityOptions = [
    { value: 'tls', label: 'TLS' },
    { value: 'starttls', label: 'STARTTLS' },
  ];
  // The usual port for each mode, so switching the mode moves a port that was
  // still on the other mode's default and leaves a typed one alone.
  const IMAP_PORTS: Record<MailSecurity, string> = { tls: '993', starttls: '143' };
  const SMTP_PORTS: Record<MailSecurity, string> = { tls: '465', starttls: '587' };

  let label = $state(start?.label ?? '');
  let slug = $state(start?.slug ?? '');
  let imapHost = $state(start?.imap_host ?? '');
  let imapSecurity: MailSecurity = $state(start?.imap_security ?? 'tls');
  let imapPort = $state(String(start?.imap_port ?? IMAP_PORTS.tls));
  let imapUsername = $state(start?.imap_username ?? '');
  let imapPassword = $state('');
  let defaultFolder = $state(start?.default_folder ?? 'INBOX');
  let enabled = $state(start?.enabled ?? true);
  let sending = $state(start?.permission === 'read_send');
  let smtpHost = $state(start?.smtp?.host ?? '');
  let smtpSecurity: MailSecurity = $state(start?.smtp?.security ?? 'starttls');
  let smtpPort = $state(String(start?.smtp?.port ?? SMTP_PORTS.starttls));
  let sameLogin = $state(start?.smtp?.same_login ?? true);
  let smtpUsername = $state(start?.smtp?.username ?? '');
  let smtpPassword = $state('');
  let fromAddress = $state(start?.from_address ?? '');
  let fromName = $state(start?.from_name ?? '');
  let sentFolder = $state(start?.sent_folder ?? '');

  // Every field the server can name in a refusal that this form draws. A
  // refusal naming anything else is a form-level error.
  const SHOWN = new Set([
    'label',
    'slug',
    'imap_host',
    'imap_port',
    'imap_security',
    'imap_username',
    'imap_password',
    'default_folder',
    'smtp.host',
    'smtp.port',
    'smtp.security',
    'smtp.username',
    'smtp_password',
    'from_address',
    'from_name',
    'sent_folder',
  ]);

  function isSecurity(value: string): value is MailSecurity {
    return value === 'tls' || value === 'starttls';
  }
  function setImapSecurity(mode: string) {
    if (!isSecurity(mode)) return;
    if (imapPort === IMAP_PORTS[imapSecurity]) imapPort = IMAP_PORTS[mode];
    imapSecurity = mode;
  }
  function setSmtpSecurity(mode: string) {
    if (!isSecurity(mode)) return;
    if (smtpPort === SMTP_PORTS[smtpSecurity]) smtpPort = SMTP_PORTS[mode];
    smtpSecurity = mode;
  }

  function port(text: string, field: string): number | null {
    const value = Number(text.trim());
    if (!Number.isInteger(value) || value < 1 || value > 65535) {
      errors[field] = 'Use a port between 1 and 65535';
      return null;
    }
    return value;
  }

  function body(): MailAccountInput | null {
    const imap = port(imapPort, 'imap_port');
    const smtp = sending ? port(smtpPort, 'smtp.port') : 0;
    if (imap === null || smtp === null) return null;
    const input: MailAccountInput = {
      label: label.trim(),
      slug: slug.trim(),
      permission: sending ? 'read_send' : 'read',
      imap_host: imapHost.trim(),
      imap_port: imap,
      imap_security: imapSecurity,
      imap_username: imapUsername.trim(),
      default_folder: defaultFolder.trim(),
    };
    if (start) input.enabled = enabled;
    if (imapPassword) input.imap_password = imapPassword;
    // Turning sending off keeps the SMTP settings, so turning it back on
    // finds them; only the permission changes.
    if (sending) {
      input.smtp = {
        host: smtpHost.trim(),
        port: smtp,
        security: smtpSecurity,
        same_login: sameLogin,
        username: sameLogin ? '' : smtpUsername.trim(),
      };
      input.from_address = fromAddress.trim();
      input.from_name = fromName.trim();
      input.sent_folder = sentFolder.trim();
      if (!sameLogin && smtpPassword) input.smtp_password = smtpPassword;
    }
    return input;
  }

  function clearSecrets() {
    imapPassword = '';
    smtpPassword = '';
  }
  function close() {
    clearSecrets();
    onClose();
  }

  async function save() {
    errors = {};
    const input = body();
    if (!input) return false;
    busy = true;
    try {
      if (start) await updateMailAccount(start.id, input);
      else await createMailAccount(input);
      clearSecrets();
      onSaved();
      onClose();
    } catch (e) {
      if (e instanceof AuthError) {
        clearSecrets();
        onError(e);
        onClose();
        return;
      }
      const error = e as Error & { field?: string | null };
      if (error.field && SHOWN.has(error.field)) {
        errors = { [error.field]: error.message };
        return false;
      }
      throw new Error(error.message || 'The mail account could not be saved.');
    } finally {
      busy = false;
    }
  }
</script>

<FormModal
  bind:open
  title={start ? 'Edit mail account' : 'Add mail account'}
  submitLabel={start ? 'Save' : 'Add account'}
  onSubmit={save}
  onCancel={close}
>
  <FormStack disabled={busy}>
    <Field label="Label" optional error={errors.label}
      ><Input bind:value={label} autocomplete="off" placeholder="Work" /></Field
    >
    <Field
      label="Short name"
      error={errors.slug}
      description="What you call it in chat, as in “check my work mail”. Lowercase letters, digits, - and _."
      ><Input bind:value={slug} autocomplete="off" monospace placeholder="work" /></Field
    >
    {#if start}
      <Checkbox
        bind:checked={enabled}
        label="Enabled"
        description="A disabled account keeps its settings but cannot be read or sent from."
      />
    {/if}

    <FormStack legend="Incoming mail (IMAP)">
      <Field
        label="Server"
        error={errors.imap_host}
        description="Microsoft 365 and Outlook.com mailboxes cannot be connected here: they no longer accept a password over IMAP."
        ><Input
          bind:value={imapHost}
          autocomplete="off"
          monospace
          placeholder="imap.example.com"
        /></Field
      >
      <FormGrid>
        <Field label="Security" error={errors.imap_security}
          ><Select
            value={imapSecurity}
            options={securityOptions}
            onValueChange={setImapSecurity}
            fullWidth
          /></Field
        >
        <Field label="Port" error={errors.imap_port}
          ><Input bind:value={imapPort} inputmode="numeric" invalid={!!errors.imap_port} /></Field
        >
      </FormGrid>
      <Field label="Username" error={errors.imap_username}
        ><Input bind:value={imapUsername} autocomplete="off" /></Field
      >
      <SecretField
        label="Password"
        configured={!!start?.imap_password_set}
        value={imapPassword}
        required={!start}
        error={errors.imap_password}
        onValueChange={(next) => (imapPassword = next)}
      />
      <Field label="Folder to read" error={errors.default_folder}
        ><Input bind:value={defaultFolder} autocomplete="off" /></Field
      >
    </FormStack>

    <Checkbox
      bind:checked={sending}
      label="Allow sending"
      description={allowSend
        ? 'Istota may send, reply and mark messages from this account. Sends still go through your approval setting.'
        : 'Sending from connected accounts is turned off on this server, so the account is read only for now.'}
    />

    {#if sending}
      <FormStack legend="Outgoing mail (SMTP)">
        <Field label="Server" error={errors['smtp.host']}
          ><Input
            bind:value={smtpHost}
            autocomplete="off"
            monospace
            placeholder="smtp.example.com"
          /></Field
        >
        <FormGrid>
          <Field label="Security" error={errors['smtp.security']}
            ><Select
              value={smtpSecurity}
              options={securityOptions}
              onValueChange={setSmtpSecurity}
              fullWidth
            /></Field
          >
          <Field label="Port" error={errors['smtp.port']}
            ><Input
              bind:value={smtpPort}
              inputmode="numeric"
              invalid={!!errors['smtp.port']}
            /></Field
          >
        </FormGrid>
        <Checkbox bind:checked={sameLogin} label="Sign in with the IMAP username and password" />
        {#if !sameLogin}
          <Field label="SMTP username" error={errors['smtp.username']}
            ><Input bind:value={smtpUsername} autocomplete="off" /></Field
          >
          <SecretField
            label="SMTP password"
            configured={!!start?.smtp_password_set}
            value={smtpPassword}
            error={errors.smtp_password}
            onValueChange={(next) => (smtpPassword = next)}
          />
        {/if}
        <Field label="From address" error={errors.from_address}
          ><Input
            bind:value={fromAddress}
            type="email"
            autocomplete="off"
            placeholder="you@example.com"
          /></Field
        >
        <Field label="From name" optional error={errors.from_name}
          ><Input bind:value={fromName} autocomplete="off" /></Field
        >
        <Field
          label="Save sent mail to"
          optional
          error={errors.sent_folder}
          description="Leave blank when the server keeps a copy itself, as Gmail does."
          ><Input bind:value={sentFolder} autocomplete="off" placeholder="Sent" /></Field
        >
      </FormStack>
    {/if}
  </FormStack>
</FormModal>
