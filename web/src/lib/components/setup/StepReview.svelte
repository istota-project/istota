<script lang="ts">
  import type { SetupModelDefaults } from '$lib/api';
  import { Button } from '$lib/components/ui';
  import {
    BROWSER_PROFILE,
    applyFeatureDefaults,
    moduleTitle,
    stepInfo,
    type SetupDraftStore,
    type SetupStepId,
  } from './setupDraft.svelte';

  interface Props {
    draft: SetupDraftStore;
    botName: string;
    models?: SetupModelDefaults;
    modules?: { field: string; module: string }[];
    /** Back to a screen, from its Edit link. */
    onEdit: (step: SetupStepId) => void;
  }

  let { draft, botName, models, modules = [], onEdit }: Props = $props();

  // A Finish reached without passing Features keeps its defaults.
  function open() {
    applyFeatureDefaults(draft, modules);
  }
  open();

  interface Row {
    label: string;
    value: string;
  }
  interface Group {
    step: SetupStepId;
    rows: Row[];
  }

  /** A credential appears as "set", never as its value; one typed and not
   *  yet saved counts too. */
  function secret(field: string): string {
    return draft.isSet(field) || draft.text(field) ? 'set' : 'not set';
  }

  function service(): string {
    if (draft.text('brain_kind') === 'claude_code') return 'Claude subscription';
    const url = draft.text('native_base_url');
    const openrouter = models ? url === models.openrouter.base_url : url.includes('openrouter.ai');
    return openrouter ? 'OpenRouter API key' : 'Anthropic API key';
  }

  const groups = $derived.by((): Group[] => {
    const nextcloud = draft.text('storage') === 'nextcloud';
    const ingress = draft.text('ingress') || 'local';
    const subscription = draft.text('brain_kind') === 'claude_code';
    const scheme = ingress === 'local' ? 'http' : 'https';
    const username: Row = { label: 'Your username', value: draft.text('user_id') };

    const welcome: Row[] = [
      { label: 'Assistant’s name', value: draft.text('bot_name') || botName },
      { label: 'Your name', value: draft.text('display_name') },
      { label: 'Your email', value: draft.text('user_email') },
      { label: 'Time zone', value: draft.text('timezone') },
    ];
    if (!nextcloud) welcome.push(username);

    const model: Row[] = [{ label: 'AI service', value: service() }];
    if (subscription) {
      model.push({ label: 'Claude Code token', value: secret('claude_code_oauth_token') });
    } else {
      model.push({ label: 'API key', value: secret('native_api_key') });
      model.push({ label: 'Model', value: draft.text('native_model') });
    }

    const address: Row[] = [
      {
        label: 'People reach it',
        value:
          ingress === 'direct'
            ? 'On the internet, at its own web address'
            : ingress === 'proxied'
              ? 'Behind your proxy'
              : 'Only on this computer',
      },
      { label: 'Web address', value: `${scheme}://${draft.text('hostname')}` },
    ];
    if (ingress === 'proxied') {
      address.push({ label: 'Proxy address', value: draft.text('upstream_proxy') });
      address.push({
        label: 'Listens on',
        value: `${draft.text('listen_addr')}:${draft.text('listen_port')}`,
      });
    }

    const files: Row[] = [
      {
        label: 'Files',
        value:
          nextcloud && draft.text('workspace') !== 'local' ? 'In your Nextcloud' : 'On this server',
      },
    ];
    if (nextcloud) {
      files.push(
        {
          label: 'Nextcloud address',
          value: draft.text('nextcloud_public_url') || draft.text('nextcloud_url'),
        },
        { label: 'Assistant’s Nextcloud username', value: draft.text('nextcloud_username') },
        { label: 'App password', value: secret('nextcloud_app_password') },
        username,
        {
          label: 'Chat in Nextcloud Talk',
          value: draft.flag('talk_enabled', false) ? 'Yes' : 'No',
        },
      );
    }

    const connections: Row[] = [];
    if (draft.flag('email_enabled', false)) {
      connections.push(
        { label: 'Email', value: `${draft.text('imap_user')} on ${draft.text('imap_host')}` },
        { label: 'Email password', value: secret('imap_password') },
      );
    } else {
      connections.push({ label: 'Email', value: 'Not connected' });
    }
    if (!nextcloud) {
      if (draft.text('caldav_url')) {
        connections.push(
          { label: 'Calendar', value: draft.text('caldav_url') },
          { label: 'Calendar password', value: secret('caldav_password') },
        );
      } else {
        connections.push({ label: 'Calendar', value: 'Not connected' });
      }
    }

    const on: string[] = [];
    const off: string[] = [];
    for (const { field, module } of modules) {
      (draft.flag(field, true) ? on : off).push(moduleTitle(module));
    }
    (draft.list('profiles').includes(BROWSER_PROFILE) ? on : off).push('Web browsing');
    const features: Row[] = [{ label: 'On', value: on.join(', ') || 'Nothing' }];
    if (off.length) features.push({ label: 'Off', value: off.join(', ') });

    return [
      { step: 'welcome', rows: welcome },
      { step: 'account', rows: [{ label: 'Password', value: secret('password') }] },
      { step: 'model', rows: model },
      { step: 'address', rows: address },
      { step: 'nextcloud', rows: files },
      { step: 'connections', rows: connections },
      { step: 'features', rows: features },
    ];
  });
</script>

<div class="step">
  <p>Check your answers, then finish. {botName} starts once setup is done.</p>
  {#each groups as group (group.step)}
    {@const title = stepInfo(group.step).title}
    <section class="review-group" aria-label={title} data-review-step={group.step}>
      <div class="card-head">
        <h3 class="micro-label">{title}</h3>
        <Button
          variant="ghost"
          size="sm"
          ariaLabel="Edit {title}"
          onclick={() => onEdit(group.step)}>Edit</Button
        >
      </div>
      <dl class="kv">
        {#each group.rows as row (row.label)}
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        {/each}
      </dl>
    </section>
  {/each}
</div>

<style>
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  p,
  h3,
  dl {
    margin: 0;
  }

  .review-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .card-head {
    align-items: center;
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
</style>
