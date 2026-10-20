<script lang="ts">
  import type { ServarrApp } from '$lib/api';
  import { Button, Field, Input, Checkbox } from '$lib/components/ui';
  import ConfigSecretField from './SecretField.svelte';
  import FieldRow from './FieldRow.svelte';
  import { credentialStatus } from './credentialStatus';
  import {
    APP_LABELS,
    DEFAULT_PORTS,
    SERVARR_APPS,
    privateHostNeed,
    type ServarrAppsForm,
  } from './servarrApps';

  interface Props {
    form: ServarrAppsForm;
    /** From `appProblems`, keyed by `<app>.url` and `<app>.key`. */
    problems: Record<string, string>;
    /** `[servarr] private_hosts` as it will be after this change. */
    privateHosts: string[];
    /** False when nothing on the page can be changed, or `[servarr.apps]` is read-only. */
    editable: boolean;
    readOnlyReason?: string | null;
    onAllowHost: (app: ServarrApp, host: string) => void;
  }

  let {
    form = $bindable(),
    problems,
    privateHosts,
    editable,
    readOnlyReason = null,
    onAllowHost,
  }: Props = $props();
</script>

<!--
  One block per arr app: whether it is connected, its URL and its API key.
  A stored key is never sent to the browser, so the key input starts empty
  and an empty input keeps the stored key. What these values become is in
  servarrApps.ts.
-->
<div class="servarr-apps">
  {#each SERVARR_APPS as name (name)}
    {@const app = form[name]}
    {@const label = APP_LABELS[name]}
    {@const need = editable && app.enabled ? privateHostNeed(app.url, privateHosts) : null}
    {@const status = credentialStatus(app.keySet === true, app.keySource ?? undefined)}
    <div class="app vstack" style:--vstack-gap="var(--space-2)" data-testid={`servarr-app-${name}`}>
      <h3 class="micro-label">{label}</h3>
      {#if editable}
        <Field label="Connected" layout="inline" checkbox>
          <Checkbox bind:checked={form[name].enabled} />
        </Field>
      {/if}
      {#if app.enabled}
        {#if editable}
          <Field
            label="URL"
            layout="inline"
            hint={`The address Istota reaches ${label} at, from the machine it runs on.`}
            error={problems[`${name}.url`]}
            warning={problems[`${name}.url`] ? undefined : need?.message}
          >
            <Input
              bind:value={form[name].url}
              monospace
              autocomplete="off"
              spellcheck="false"
              placeholder={`http://192.168.1.10:${DEFAULT_PORTS[name]}`}
              invalid={!!problems[`${name}.url`]}
            />
          </Field>
          {#if need}
            <div class="row-actions">
              <Button variant="secondary" size="sm" onclick={() => onAllowHost(name, need.host)}
                >Add {need.host} to private hosts</Button
              >
            </div>
          {/if}
        {:else}
          <Field label="URL" layout="inline">
            <span class="config-value">{app.url || '—'}</span>
          </Field>
        {/if}
        <FieldRow>
          {#if editable && !app.keyReadOnly}
            <ConfigSecretField
              label="API key"
              configured={app.keySet === true}
              pending={app.clearKey ? '' : app.key === '' ? undefined : app.key}
              hint={`In ${label}, under Settings → General → Security.`}
              {status}
              inline
              onChange={(value) => {
                form[name].clearKey = value === '';
                form[name].key = value ?? '';
              }}
            />
          {:else}
            <ConfigSecretField
              label="API key"
              configured={app.keySet === true}
              pending={undefined}
              {status}
              readOnlyReason={app.keyReadOnly ?? readOnlyReason ?? 'Read-only'}
              inline
              onChange={() => {}}
            />
          {/if}
        </FieldRow>
        {#if problems[`${name}.key`]}
          <p class="caption problem" role="alert">{problems[`${name}.key`]}</p>
        {/if}
      {:else}
        <p class="caption">Not connected.</p>
      {/if}
    </div>
  {/each}
  {#if !editable && readOnlyReason}
    <p class="caption">{readOnlyReason}</p>
  {/if}
</div>

<style>
  .servarr-apps {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-2) 0;
    border-bottom: 1px solid var(--border-subtle);
  }

  .app > .micro-label {
    margin: 0;
  }

  .servarr-apps .caption {
    margin: 0;
  }

  .problem {
    color: var(--status-danger-fg);
  }

  .row-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .config-value {
    display: flex;
    align-items: center;
    min-height: var(--control-height-lg);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }
</style>
