<script lang="ts">
  import {
    checkSetupConnection,
    getSetupModels,
    SetupApiError,
    type SetupCheckResult,
    type SetupModelDefaults,
  } from '$lib/api';
  import { AutocompleteInput, Button, Field, Input, OptionCards } from '$lib/components/ui';
  import { SecretField } from '$lib/components/settings';
  import SetupCheckResultLine from './SetupCheckResult.svelte';
  import SetupMore from './SetupMore.svelte';
  import type { SetupDraftStore } from './setupDraft.svelte';

  type Service = 'anthropic' | 'openrouter' | 'subscription';

  interface Props {
    draft: SetupDraftStore;
    errors: Record<string, string>;
    /** Each API-key service's address and default model, from `/state`. */
    models?: SetupModelDefaults;
    /** Set when a test failed, so the page offers "Continue without testing". */
    offerSkip?: boolean;
  }

  let { draft, errors, models, offerSkip = $bindable(false) }: Props = $props();

  const OPTIONS = [
    {
      value: 'anthropic',
      title: 'Anthropic API key',
      description: 'Pay Anthropic for what Istota uses. The quickest to set up.',
      badge: 'Recommended',
    },
    {
      value: 'openrouter',
      title: 'OpenRouter API key',
      description: 'Pay OpenRouter, and choose from models by several companies.',
    },
    {
      value: 'subscription',
      title: 'Claude subscription',
      description: 'Use your Claude Pro or Max plan. Takes longer to finish setting up.',
    },
  ];

  const KEY_PAGES: Record<'anthropic' | 'openrouter', string> = {
    anthropic: 'https://console.anthropic.com/settings/keys',
    openrouter: 'https://openrouter.ai/settings/keys',
  };

  function serviceOf(): Service {
    if (draft.text('brain_kind') === 'claude_code') return 'subscription';
    if (models && draft.text('native_base_url') === models.openrouter.base_url) return 'openrouter';
    return 'anthropic';
  }

  let service = $state<Service>(serviceOf());
  let testing = $state(false);
  let result = $state<SetupCheckResult | null>(null);
  let testErrors = $state<Record<string, string>>({});
  let suggestions = $state<string[]>([]);
  let suggestionsAsked = false;

  const shown = $derived({ ...errors, ...testErrors });

  function forget() {
    result = null;
    testErrors = {};
    offerSkip = false;
  }

  function loadSuggestions() {
    if (suggestionsAsked) return;
    suggestionsAsked = true;
    getSetupModels()
      .then((ids) => (suggestions = ids))
      .catch(() => (suggestions = []));
  }

  function choose(next: Service, previous: Service | null) {
    if (next === 'subscription') {
      draft.set('brain_kind', 'claude_code');
    } else {
      draft.set('brain_kind', 'native');
      draft.set('native_base_url', models?.[next].base_url ?? '');
      draft.set('native_model', models?.[next].model ?? '');
      // One service's key is no use to the other.
      if (previous && previous !== 'subscription' && previous !== next) {
        draft.clearSecret('native_api_key');
      }
      if (next === 'openrouter') loadSuggestions();
    }
    forget();
  }

  // Once, when the screen opens: an untouched screen starts on the first card.
  function open() {
    if (!draft.has('brain_kind')) choose('anthropic', null);
    else if (service === 'openrouter') loadSuggestions();
  }
  open();

  function edited(field: string, value: string) {
    if (field === 'native_api_key') draft.setSecret(field, value);
    else draft.set(field, value);
    forget();
  }

  /** Save what the screen holds and test it. True when it connected. */
  async function test(): Promise<boolean> {
    testing = true;
    forget();
    try {
      await draft.save();
      result = await checkSetupConnection('model');
    } catch (e) {
      if (e instanceof SetupApiError && e.status === 401) throw e;
      const fields: Record<string, string> = {};
      if (e instanceof SetupApiError) {
        for (const f of e.fields) if (f.field && !(f.field in fields)) fields[f.field] = f.message;
      }
      testErrors = fields;
      result = { ok: false, warning: false, message: e instanceof Error ? e.message : String(e) };
    } finally {
      testing = false;
    }
    const passed = !!result?.ok;
    offerSkip = !passed;
    return passed;
  }

  /** Before Next: an API key is tested unless its test already passed. */
  export async function beforeNext(): Promise<boolean> {
    if (service === 'subscription' || result?.ok) return true;
    return test();
  }
</script>

<div class="step">
  <p>Istota needs an AI service to think with. Choose how you will pay for it.</p>
  <OptionCards
    label="AI service"
    options={OPTIONS}
    value={service}
    onchange={(value) => {
      const previous = service;
      service = value as Service;
      choose(service, previous);
    }}
  >
    {#snippet selected(value)}
      {#if value === 'subscription'}
        <div class="fields">
          <p class="muted">
            Run <code>claude setup-token</code> on a computer with Claude Code installed, sign in, and
            paste the token it prints. It can't be tested here: finishing setup takes several minutes
            longer while Istota installs Claude Code, and the token is first used after that.
          </p>
          <SecretField
            label="Claude Code token"
            configured={draft.isSet('claude_code_oauth_token')}
            value={draft.text('claude_code_oauth_token')}
            error={shown.claude_code_oauth_token}
            onValueChange={(v) => draft.setSecret('claude_code_oauth_token', v)}
          />
        </div>
      {:else}
        {@const keyService = value as 'anthropic' | 'openrouter'}
        <div class="fields">
          <SecretField
            label={keyService === 'anthropic' ? 'Anthropic API key' : 'OpenRouter API key'}
            configured={draft.isSet('native_api_key')}
            value={draft.text('native_api_key')}
            error={shown.native_api_key}
            onValueChange={(v) => edited('native_api_key', v)}
          />
          <p class="muted">
            <a href={KEY_PAGES[keyService]} target="_blank" rel="noopener noreferrer"
              >Get a key from {keyService === 'anthropic' ? 'Anthropic' : 'OpenRouter'}</a
            >
          </p>
          <div class="test-row">
            <Button variant="secondary" loading={testing} loadingLabel="Testing…" onclick={test}
              >Test</Button
            >
          </div>
          <SetupCheckResultLine {result} />
          <SetupMore open={!!shown.native_model}>
            <Field
              label="Model"
              hint="The model Istota uses for most work. The Test button checks that this service has it."
              error={shown.native_model}
            >
              {#if keyService === 'openrouter'}
                <AutocompleteInput
                  value={draft.text('native_model')}
                  options={suggestions}
                  monospace
                  invalid={!!shown.native_model}
                  onValueChange={(v) => edited('native_model', v)}
                />
              {:else}
                <Input
                  value={draft.text('native_model')}
                  monospace
                  spellcheck={false}
                  invalid={!!shown.native_model}
                  oninput={(e: Event) =>
                    edited('native_model', (e.currentTarget as HTMLInputElement).value)}
                />
              {/if}
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

  .test-row {
    display: flex;
  }
</style>
