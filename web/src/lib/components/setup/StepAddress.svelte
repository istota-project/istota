<script lang="ts">
  import type { SetupCheckResult } from '$lib/api';
  import { Button, Field, Input, NoticeBanner, OptionCards, Select } from '$lib/components/ui';
  import SetupCheckResultLine from './SetupCheckResult.svelte';
  import { runSetupCheck, type SetupDraftStore } from './setupDraft.svelte';

  type Choice = 'local' | 'direct' | 'proxied';

  interface Props {
    draft: SetupDraftStore;
    errors: Record<string, string>;
    /** The address the browser used to open this page (`Host`, from `/state`). */
    host?: string;
  }

  let { draft, errors, host = '' }: Props = $props();

  /** What "Only on this computer" serves on: the local stack's published port. */
  const LOCAL_ADDRESS = 'localhost:8080';
  const PROXY_CERTIFICATES = [
    { value: 'self-signed', label: 'Self-signed, made here' },
    { value: 'files', label: 'Certificate files I provide' },
    { value: 'none', label: 'None (plain HTTP)' },
  ];

  const OPTIONS = [
    {
      value: 'local',
      title: 'Only on this computer',
      description: 'Open Istota in a browser on this machine, or through a tunnel you set up.',
    },
    {
      value: 'direct',
      title: 'On the internet, at my own web address',
      description: 'Reach Istota from anywhere at a name like istota.example.com.',
    },
    {
      value: 'proxied',
      title: 'Behind a proxy I run',
      description: 'Another server of yours already answers for your domain and passes visits on.',
    },
  ];

  /** The host without its port, `[…]` kept around an IPv6 address. */
  function bareHost(value: string): string {
    if (value.startsWith('[')) return value.slice(0, value.indexOf(']') + 1) || value;
    return value.split(':')[0];
  }

  const browserHost = $derived(bareHost(host));
  const openedHere = $derived(
    ['localhost', '127.0.0.1', '[::1]'].includes(browserHost.toLowerCase()),
  );

  /** A web address as the server takes it: no scheme, no path. */
  function webName(raw: string): string {
    return raw
      .trim()
      .replace(/^[a-z][a-z0-9+.-]*:\/\//i, '')
      .split('/')[0];
  }

  function choiceOf(): Choice {
    const saved = draft.text('ingress');
    if (saved === 'local' || saved === 'direct' || saved === 'proxied') return saved;
    return openedHere ? 'local' : 'direct';
  }

  let choice = $state<Choice>(choiceOf());
  let testing = $state(false);
  let result = $state<SetupCheckResult | null>(null);
  let testErrors = $state<Record<string, string>>({});

  const shown = $derived({ ...errors, ...testErrors });

  function forget() {
    result = null;
    testErrors = {};
  }

  function choose(next: Choice) {
    draft.set('ingress', next);
    if (next === 'local') {
      draft.set('hostname', LOCAL_ADDRESS);
    } else if (draft.text('hostname') === LOCAL_ADDRESS) {
      draft.set('hostname', '');
    }
    if (next === 'direct') {
      draft.set('tls_cert_source', 'acme');
    } else if (next === 'proxied') {
      if (!PROXY_CERTIFICATES.some((c) => c.value === draft.text('tls_cert_source'))) {
        draft.set('tls_cert_source', 'self-signed');
      }
      if (!draft.text('listen_port')) draft.set('listen_port', 8080);
    }
    forget();
  }

  // Once, when the screen opens: an untouched screen starts on its default.
  function open() {
    if (!draft.has('ingress')) choose(choice);
  }
  open();

  function edited(field: string, value: string | number) {
    draft.set(field, value);
    forget();
  }

  function tidy() {
    if (choice !== 'local') draft.set('hostname', webName(draft.text('hostname')));
  }

  /** Before Next: the address as the server takes it, and a port that is a number. */
  export function precheck(): Record<string, string> | null {
    tidy();
    if (choice === 'proxied' && !/^\d+$/.test(draft.text('listen_port'))) {
      return { listen_port: 'Enter a port number.' };
    }
    return null;
  }

  async function test() {
    testing = true;
    forget();
    tidy();
    try {
      ({ result, fields: testErrors } = await runSetupCheck(draft, 'address'));
    } finally {
      testing = false;
    }
  }
</script>

<div class="step">
  <p>Choose how you and anyone you invite will open Istota once it is set up.</p>
  <OptionCards
    label="How people reach Istota"
    options={OPTIONS}
    value={choice}
    onchange={(value) => {
      choice = value as Choice;
      choose(choice);
    }}
  >
    {#snippet selected(value)}
      <div class="fields">
        {#if value === 'local'}
          {#if !openedHere}
            <NoticeBanner
              variant="warn"
              title="You opened this page from another computer. With this choice you won't be able to reach Istota the same way after setup."
            />
          {/if}
          <p class="muted">
            Istota will answer at <code>http://{LOCAL_ADDRESS}</code> on this machine.
          </p>
        {:else}
          {#if value === 'proxied'}
            <p class="muted">
              This is for people who already run a reverse proxy in front of this server. Istota
              listens on a private address and accepts connections from your proxy only.
            </p>
          {/if}
          <Field
            label="Web address"
            hint="The name people type to reach Istota, without https://."
            error={shown.hostname}
          >
            <Input
              value={draft.text('hostname')}
              placeholder="istota.example.com"
              monospace
              autocapitalize="none"
              spellcheck={false}
              invalid={!!shown.hostname}
              oninput={(e: Event) =>
                edited('hostname', (e.currentTarget as HTMLInputElement).value)}
            />
          </Field>
          {#if value === 'direct'}
            <NoticeBanner
              variant="info"
              title="Point this name at this server's address, {browserHost}, with your domain provider before you finish."
            />
            <div class="test-row">
              <Button variant="secondary" loading={testing} loadingLabel="Testing…" onclick={test}
                >Test</Button
              >
            </div>
            <SetupCheckResultLine {result} />
          {:else}
            <Field
              label="Upstream proxy address"
              hint="The address your proxy connects from, or a network in CIDR form such as 10.0.0.0/24."
              error={shown.upstream_proxy}
            >
              <Input
                value={draft.text('upstream_proxy')}
                monospace
                spellcheck={false}
                invalid={!!shown.upstream_proxy}
                oninput={(e: Event) =>
                  edited('upstream_proxy', (e.currentTarget as HTMLInputElement).value)}
              />
            </Field>
            <Field
              label="Listen address"
              hint="One private address of this server for the proxy to connect to."
              error={shown.listen_addr}
            >
              <Input
                value={draft.text('listen_addr')}
                monospace
                spellcheck={false}
                invalid={!!shown.listen_addr}
                oninput={(e: Event) =>
                  edited('listen_addr', (e.currentTarget as HTMLInputElement).value)}
              />
            </Field>
            <Field label="Listen port" error={shown.listen_port}>
              <Input
                type="number"
                min="1"
                max="65535"
                value={draft.text('listen_port')}
                invalid={!!shown.listen_port}
                oninput={(e: Event) => {
                  const raw = (e.currentTarget as HTMLInputElement).value;
                  edited('listen_port', /^\d+$/.test(raw) ? Number(raw) : raw);
                }}
              />
            </Field>
            <Field
              label="Certificate between the proxy and Istota"
              error={shown.tls_cert_source}
              labelled={false}
            >
              <Select
                value={draft.text('tls_cert_source') || 'self-signed'}
                options={PROXY_CERTIFICATES}
                ariaLabel="Certificate between the proxy and Istota"
                fullWidth
                onValueChange={(v) => edited('tls_cert_source', v)}
              />
            </Field>
          {/if}
        {/if}
      </div>
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
