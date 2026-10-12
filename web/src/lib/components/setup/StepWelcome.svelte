<script lang="ts">
  import { AutocompleteInput, Field, Input } from '$lib/components/ui';
  import SetupMore from './SetupMore.svelte';
  import { DEFAULT_BOT_NAME, deriveUsername, type SetupDraftStore } from './setupDraft.svelte';

  interface Props {
    draft: SetupDraftStore;
    /** The server's refusals, by field. */
    errors: Record<string, string>;
    defaultBotName?: string;
  }

  let { draft, errors, defaultBotName = DEFAULT_BOT_NAME }: Props = $props();

  function browserTimeZone(): string {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    } catch {
      return 'UTC';
    }
  }

  function timeZones(): string[] {
    try {
      return Intl.supportedValuesOf('timeZone');
    } catch {
      return [];
    }
  }

  const zones = timeZones();

  // Once, when the screen opens: an answer already given is kept.
  function fillDefaults() {
    draft.setDefault('bot_name', defaultBotName);
    draft.setDefault('timezone', browserTimeZone());
  }
  fillDefaults();

  // With Nextcloud the username is the Nextcloud screen's, not the email's.
  const fromNextcloud = $derived(draft.text('storage') === 'nextcloud');

  // The username follows the email until somebody types one of their own:
  // it is still the email's suggestion as long as it equals it.
  function setEmail(value: string) {
    const suggested = deriveUsername(draft.text('user_email'));
    const username = draft.text('user_id');
    draft.set('user_email', value);
    if (fromNextcloud) return;
    if (!username || username === suggested) draft.set('user_id', deriveUsername(value));
  }

  /** Before Next: a username even when nobody opened "More". */
  export function precheck(): Record<string, string> | null {
    if (!fromNextcloud && !draft.text('user_id')) {
      draft.set('user_id', deriveUsername(draft.text('user_email')));
    }
    return null;
  }
</script>

<div class="step">
  <p>
    Answer a few questions and your assistant will be ready to use. You can change any of this
    later.
  </p>
  <Field label="What should your assistant be called?" error={errors.bot_name}>
    <Input
      value={draft.text('bot_name')}
      invalid={!!errors.bot_name}
      oninput={(e: Event) => draft.set('bot_name', (e.currentTarget as HTMLInputElement).value)}
    />
  </Field>
  <Field label="Your name" error={errors.display_name}>
    <Input
      value={draft.text('display_name')}
      autocomplete="name"
      invalid={!!errors.display_name}
      oninput={(e: Event) => draft.set('display_name', (e.currentTarget as HTMLInputElement).value)}
    />
  </Field>
  <Field label="Your email" hint="You will sign in with this address." error={errors.user_email}>
    <Input
      type="email"
      value={draft.text('user_email')}
      autocomplete="email"
      invalid={!!errors.user_email}
      oninput={(e: Event) => setEmail((e.currentTarget as HTMLInputElement).value)}
    />
  </Field>
  <Field label="Time zone" error={errors.timezone}>
    <AutocompleteInput
      value={draft.text('timezone')}
      options={zones}
      invalid={!!errors.timezone}
      onValueChange={(value) => draft.set('timezone', value)}
    />
  </Field>
  <SetupMore open={!!errors.user_id}>
    {#if fromNextcloud}
      <p class="caption">Your username is your Nextcloud username, set on the Nextcloud screen.</p>
    {:else}
      <Field
        label="Username"
        hint="Istota's name for your account, made from your email. Lowercase letters, digits, dots, hyphens and underscores."
        error={errors.user_id}
      >
        <Input
          value={draft.text('user_id')}
          monospace
          autocapitalize="none"
          spellcheck={false}
          invalid={!!errors.user_id}
          oninput={(e: Event) => draft.set('user_id', (e.currentTarget as HTMLInputElement).value)}
        />
      </Field>
    {/if}
  </SetupMore>
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
</style>
