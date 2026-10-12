<script lang="ts">
  import type { SetupCheckResult } from '$lib/api';
  import { Button, Field, Input } from '$lib/components/ui';
  import { SecretField } from '$lib/components/settings';
  import ConnectionCard from './ConnectionCard.svelte';
  import SetupCheckResultLine from './SetupCheckResult.svelte';
  import SetupMore from './SetupMore.svelte';
  import { runSetupCheck, type SetupDraftStore } from './setupDraft.svelte';

  type Kind = 'email' | 'caldav';

  interface Props {
    draft: SetupDraftStore;
    errors: Record<string, string>;
    botName: string;
    /** Set when a test failed, so the page offers "Continue without testing". */
    offerSkip?: boolean;
  }

  let { draft, errors, botName, offerSkip = $bindable(false) }: Props = $props();

  const SECRETS = new Set(['imap_password', 'caldav_password']);

  // With Nextcloud the calendar comes from there, so there is nothing to add.
  const nextcloud = $derived(draft.text('storage') === 'nextcloud');

  function emailAdded(): boolean {
    return draft.flag('email_enabled', false);
  }
  function calendarAdded(): boolean {
    return !!draft.text('caldav_url');
  }

  let email = $state(emailAdded());
  let calendar = $state(calendarAdded());
  let testing = $state<Kind | null>(null);
  let results = $state<Record<Kind, SetupCheckResult | null>>({ email: null, caldav: null });
  let testErrors = $state<Record<string, string>>({});

  const shown = $derived({ ...errors, ...testErrors });

  function forget(kind: Kind) {
    results[kind] = null;
    testErrors = {};
    offerSkip = false;
  }

  function edited(kind: Kind, field: string, value: string) {
    if (SECRETS.has(field)) draft.setSecret(field, value);
    else draft.set(field, value);
    forget(kind);
  }

  function addEmail() {
    email = true;
    draft.set('email_enabled', true);
  }
  function removeEmail() {
    email = false;
    draft.set('email_enabled', false);
    forget('email');
  }
  function addCalendar() {
    calendar = true;
  }
  function removeCalendar() {
    calendar = false;
    // The address is what turns a calendar on; the rest is left unwritten.
    draft.set('caldav_url', '');
    forget('caldav');
  }

  /** Save what the screen holds and test one connection. True when it worked. */
  async function test(kind: Kind): Promise<boolean> {
    testing = kind;
    forget(kind);
    try {
      const { result, fields } = await runSetupCheck(draft, kind);
      results[kind] = result;
      testErrors = { ...testErrors, ...fields };
    } finally {
      testing = null;
    }
    const passed = !!results[kind]?.ok;
    if (!passed) offerSkip = true;
    return passed;
  }

  /** Before Next: every added connection is tested unless its test passed. */
  export async function beforeNext(): Promise<boolean> {
    const wanted: Kind[] = [];
    if (email) wanted.push('email');
    if (calendar && !nextcloud && draft.text('caldav_url')) wanted.push('caldav');
    let passed = true;
    for (const kind of wanted) {
      if (results[kind]?.ok) continue;
      if (!(await test(kind))) passed = false;
    }
    return passed;
  }

  const input = (e: Event) => (e.currentTarget as HTMLInputElement).value;
</script>

<div class="step">
  <p>Both are optional.</p>

  <ConnectionCard
    title="Connect an email account"
    description="Give {botName} an email address of its own, so you can write to it and it can send mail."
    added={email}
    onAdd={addEmail}
    onRemove={removeEmail}
  >
    <Field
      label="Email address"
      hint="The account {botName} reads and sends mail as. A new address just for {botName} works best."
      error={shown.imap_user}
    >
      <Input
        type="email"
        value={draft.text('imap_user')}
        placeholder="istota@example.com"
        autocapitalize="none"
        spellcheck={false}
        invalid={!!shown.imap_user}
        oninput={(e: Event) => edited('email', 'imap_user', input(e))}
      />
    </Field>
    <SecretField
      label="Email password"
      configured={draft.isSet('imap_password')}
      value={draft.text('imap_password')}
      error={shown.imap_password}
      onValueChange={(v) => edited('email', 'imap_password', v)}
    />
    <Field label="Incoming mail server" error={shown.imap_host}>
      <Input
        value={draft.text('imap_host')}
        placeholder="imap.example.com"
        monospace
        autocapitalize="none"
        spellcheck={false}
        invalid={!!shown.imap_host}
        oninput={(e: Event) => edited('email', 'imap_host', input(e))}
      />
    </Field>
    <div class="test-row">
      <Button
        variant="secondary"
        loading={testing === 'email'}
        loadingLabel="Testing…"
        disabled={testing !== null}
        onclick={() => test('email')}>Test</Button
      >
    </div>
    <SetupCheckResultLine result={results.email} />
    <SetupMore open={!!(shown.smtp_host || shown.bot_email)}>
      <Field
        label="Outgoing mail server"
        hint="Only when it differs from the incoming one."
        error={shown.smtp_host}
      >
        <Input
          value={draft.text('smtp_host')}
          placeholder={draft.text('imap_host') || 'smtp.example.com'}
          monospace
          autocapitalize="none"
          spellcheck={false}
          invalid={!!shown.smtp_host}
          oninput={(e: Event) => edited('email', 'smtp_host', input(e))}
        />
      </Field>
      <Field
        label="Address {botName} sends from"
        hint="Only when it differs from the email address above."
        error={shown.bot_email}
      >
        <Input
          type="email"
          value={draft.text('bot_email')}
          placeholder={draft.text('imap_user')}
          autocapitalize="none"
          spellcheck={false}
          invalid={!!shown.bot_email}
          oninput={(e: Event) => edited('email', 'bot_email', input(e))}
        />
      </Field>
    </SetupMore>
  </ConnectionCard>

  {#if !nextcloud}
    <ConnectionCard
      title="Connect a calendar"
      description="Let {botName} see your calendar and add to it."
      added={calendar}
      onAdd={addCalendar}
      onRemove={removeCalendar}
    >
      <Field
        label="Calendar address"
        hint="Your calendar provider lists it as the CalDAV address."
        error={shown.caldav_url}
      >
        <Input
          type="url"
          value={draft.text('caldav_url')}
          placeholder="https://calendar.example.com/dav/"
          monospace
          autocapitalize="none"
          spellcheck={false}
          invalid={!!shown.caldav_url}
          oninput={(e: Event) => edited('caldav', 'caldav_url', input(e))}
        />
      </Field>
      <Field label="Calendar username" error={shown.caldav_username}>
        <Input
          value={draft.text('caldav_username')}
          autocapitalize="none"
          spellcheck={false}
          invalid={!!shown.caldav_username}
          oninput={(e: Event) => edited('caldav', 'caldav_username', input(e))}
        />
      </Field>
      <SecretField
        label="Calendar password"
        configured={draft.isSet('caldav_password')}
        value={draft.text('caldav_password')}
        error={shown.caldav_password}
        onValueChange={(v) => edited('caldav', 'caldav_password', v)}
      />
      <div class="test-row">
        <Button
          variant="secondary"
          loading={testing === 'caldav'}
          loadingLabel="Testing…"
          disabled={testing !== null}
          onclick={() => test('caldav')}>Test</Button
        >
      </div>
      <SetupCheckResultLine result={results.caldav} />
    </ConnectionCard>
  {/if}

  <p class="caption">You can add or change these later in Admin → Configuration.</p>
</div>

<style>
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  p {
    margin: 0;
  }

  .test-row {
    display: flex;
  }
</style>
