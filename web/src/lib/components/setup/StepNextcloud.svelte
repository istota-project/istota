<script lang="ts">
  import type { SetupCheckResult } from '$lib/api';
  import { Button, Field, Input, NoticeBanner, OptionCards } from '$lib/components/ui';
  import { SecretField } from '$lib/components/settings';
  import SetupCheckResultLine from './SetupCheckResult.svelte';
  import SetupMore from './SetupMore.svelte';
  import { deriveUsername, runSetupCheck, type SetupDraftStore } from './setupDraft.svelte';

  type Storage = 'local' | 'nextcloud';

  interface Props {
    draft: SetupDraftStore;
    errors: Record<string, string>;
    botName: string;
    /** Set when a test failed, so the page offers "Continue without testing". */
    offerSkip?: boolean;
  }

  let { draft, errors, botName, offerSkip = $bindable(false) }: Props = $props();

  const OPTIONS = [
    {
      value: 'local',
      title: 'Keep files on this server',
      description: 'Istota keeps its files here, and you chat with it in the web app.',
    },
    {
      value: 'nextcloud',
      title: 'Use my Nextcloud',
      description: 'Istota becomes a user on your Nextcloud, works in its files and chats in Talk.',
    },
  ];

  /** Scratch key for the Welcome screen's username while Nextcloud's replaces it. */
  const LOCAL_USERNAME = 'local_user_id';

  /**
   * A Nextcloud address as the server takes it: a scheme, and nothing after
   * the host and any subdirectory Nextcloud is installed under, so a pasted
   * `…/index.php/apps/files` or a trailing slash is dropped.
   */
  function nextcloudAddress(raw: string): string {
    let url = raw.trim();
    if (!url) return '';
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) url = `https://${url}`;
    return url.replace(/\/index\.php(\/.*)?$/, '').replace(/[/?#]+$/, '');
  }

  function storageOf(): Storage {
    return draft.text('storage') === 'nextcloud' ? 'nextcloud' : 'local';
  }

  // The address under More, only when it differs from the one browsers use.
  function internalOf(): string {
    const browsers = draft.text('nextcloud_public_url');
    const server = draft.text('nextcloud_url');
    return browsers && server !== browsers ? server : '';
  }

  // Talk was found by an earlier test if it is on in the draft.
  function talkFound(): boolean {
    return draft.flag('talk_enabled', false);
  }

  let storage = $state<Storage>(storageOf());
  let internal = $state(internalOf());
  let talkAvailable = $state(talkFound());
  let testing = $state(false);
  let result = $state<SetupCheckResult | null>(null);
  let testErrors = $state<Record<string, string>>({});

  const shown = $derived({ ...errors, ...testErrors });
  const address = $derived(draft.text('nextcloud_public_url') || draft.text('nextcloud_url'));

  function forget() {
    result = null;
    testErrors = {};
    offerSkip = false;
  }

  function choose(next: Storage) {
    draft.set('storage', next);
    if (next === 'nextcloud') {
      // With Nextcloud, the username is the user's Nextcloud id, which the
      // email cannot suggest: the field starts empty rather than wrong.
      draft.scratch[LOCAL_USERNAME] = draft.text('user_id');
      draft.set('user_id', '');
      draft.setDefault('nextcloud_username', 'istota');
      if (!talkAvailable) draft.set('talk_enabled', false);
    } else {
      draft.set(
        'user_id',
        draft.scratch[LOCAL_USERNAME] || deriveUsername(draft.text('user_email')),
      );
    }
    forget();
  }

  // Once, when the screen opens: an untouched screen keeps files here.
  function open() {
    if (!draft.has('storage')) draft.set('storage', 'local');
  }
  open();

  function edited(field: string, value: string) {
    if (field === 'nextcloud_app_password') draft.setSecret(field, value);
    else draft.set(field, value);
    forget();
  }

  function setAddress(value: string) {
    draft.set('nextcloud_public_url', value);
    if (!internal) draft.set('nextcloud_url', value);
    forget();
  }

  function setInternal(value: string) {
    internal = value;
    draft.set('nextcloud_url', value || address);
    forget();
  }

  function tidy() {
    if (storage !== 'nextcloud') return;
    const browsers = nextcloudAddress(address);
    draft.set('nextcloud_public_url', browsers);
    internal = nextcloudAddress(internal);
    draft.set('nextcloud_url', internal || browsers);
  }

  export function precheck(): Record<string, string> | null {
    tidy();
    return null;
  }

  /** Save what the screen holds and test it. True when every step passed. */
  async function test(): Promise<boolean> {
    testing = true;
    forget();
    tidy();
    try {
      ({ result, fields: testErrors } = await runSetupCheck(draft, 'nextcloud'));
      if (result.lines) {
        const found = !!result.talk_available;
        // Talk is offered, checked, once the check finds it; without it chat
        // is in the web app only.
        if (found !== talkAvailable || !found) draft.set('talk_enabled', found);
        talkAvailable = found;
        await draft.save();
      }
    } finally {
      testing = false;
    }
    const passed = !!result?.ok;
    offerSkip = !passed;
    return passed;
  }

  /** Before Next: Nextcloud is tested unless its test already passed. */
  export async function beforeNext(): Promise<boolean> {
    if (storage === 'local' || result?.ok) return true;
    return test();
  }
</script>

<div class="step">
  <p>
    {botName} can keep its files on this server, or work in your Nextcloud. This is optional.
  </p>
  <OptionCards
    label="Where Istota keeps its files"
    options={OPTIONS}
    value={storage}
    onchange={(value) => {
      storage = value as Storage;
      choose(storage);
    }}
  >
    {#snippet selected(value)}
      {#if value === 'nextcloud'}
        <div class="fields">
          <NoticeBanner variant="info" title="Before you continue" collapsed={false}>
            {#snippet children()}
              <ol class="checklist">
                <li>
                  Sign in to Nextcloud as an administrator and open <strong>Users</strong>. Create a
                  new user for your assistant, for example <code>istota</code>, with the display
                  name “{botName}”. Don't make it an administrator.
                </li>
                <li>
                  If you want to chat with your assistant in Nextcloud, check that the
                  <strong>Talk</strong> app is installed (Apps → Talk).
                </li>
                <li>
                  Sign in to Nextcloud as the new user (a private browser window is easiest). Open
                  <strong>Personal settings → Security</strong>, and under “Devices &amp; sessions”
                  create an app password named “Istota”. Copy it.
                </li>
                <li>Enter the details below.</li>
              </ol>
            {/snippet}
          </NoticeBanner>
          <Field
            label="Nextcloud address"
            error={shown.nextcloud_public_url ?? (internal ? undefined : shown.nextcloud_url)}
          >
            <Input
              type="url"
              value={address}
              placeholder="https://cloud.example.com"
              monospace
              autocapitalize="none"
              spellcheck={false}
              invalid={!!(shown.nextcloud_public_url ?? (!internal && shown.nextcloud_url))}
              oninput={(e: Event) => setAddress((e.currentTarget as HTMLInputElement).value)}
            />
          </Field>
          <Field label="The assistant's Nextcloud username" error={shown.nextcloud_username}>
            <Input
              value={draft.text('nextcloud_username')}
              monospace
              autocapitalize="none"
              spellcheck={false}
              invalid={!!shown.nextcloud_username}
              oninput={(e: Event) =>
                edited('nextcloud_username', (e.currentTarget as HTMLInputElement).value)}
            />
          </Field>
          <SecretField
            label="The assistant's app password"
            configured={draft.isSet('nextcloud_app_password')}
            value={draft.text('nextcloud_app_password')}
            error={shown.nextcloud_app_password}
            onValueChange={(v) => edited('nextcloud_app_password', v)}
          />
          <Field
            label="Your own Nextcloud username"
            hint="Istota shares its folder with this account and invites it to chats."
            error={shown.user_id}
          >
            <Input
              value={draft.text('user_id')}
              monospace
              autocapitalize="none"
              spellcheck={false}
              invalid={!!shown.user_id}
              oninput={(e: Event) => edited('user_id', (e.currentTarget as HTMLInputElement).value)}
            />
          </Field>
          <div class="test-row">
            <Button variant="secondary" loading={testing} loadingLabel="Testing…" onclick={test}
              >Test</Button
            >
          </div>
          <SetupCheckResultLine {result} />
          {#if talkAvailable}
            <Field label="Chat with {botName} in Nextcloud Talk" checkbox>
              <input
                type="checkbox"
                checked={draft.flag('talk_enabled', true)}
                onchange={(e: Event) =>
                  draft.set('talk_enabled', (e.currentTarget as HTMLInputElement).checked)}
              />
            </Field>
          {/if}
          <SetupMore open={!!(internal && shown.nextcloud_url)}>
            <Field
              label="Address this server uses to reach Nextcloud"
              hint="Only when this server reaches Nextcloud at another name than your browser does, such as an internal one."
              error={internal ? shown.nextcloud_url : undefined}
            >
              <Input
                type="url"
                value={internal}
                monospace
                autocapitalize="none"
                spellcheck={false}
                invalid={!!(internal && shown.nextcloud_url)}
                oninput={(e: Event) => setInternal((e.currentTarget as HTMLInputElement).value)}
              />
            </Field>
          </SetupMore>
        </div>
      {/if}
    {/snippet}
  </OptionCards>
</div>

<style>
  .step,
  .fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  p {
    margin: 0;
  }

  .checklist {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin: 0;
    padding-left: var(--space-6);
  }

  .test-row {
    display: flex;
  }
</style>
