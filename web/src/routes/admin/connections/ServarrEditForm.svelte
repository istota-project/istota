<script lang="ts">
  import { untrack } from 'svelte';
  import type { ServarrApp } from '$lib/api';
  import { Badge, Button, Field, Input, MultiSelect, TextArea } from '$lib/components/ui';
  import {
    APP_LABELS,
    DEFAULT_FIELDS,
    DEFAULT_PORTS,
    SERVARR_APPS,
    privateHostNeed,
    withPrivateHost,
    type ServarrForm,
  } from './servarrForm';

  interface Props {
    form: ServarrForm;
    /** From `formProblems`, keyed by `<app>.url` and `<app>.key`; the quick
     *  fix clears the URL entry it answers. */
    problems: Record<string, string>;
    /** Configured user ids, for the requester picker. */
    users: string[];
  }

  let { form = $bindable(), problems = $bindable(), users }: Props = $props();

  /** Apps whose stored key the admin chose to replace in this session. */
  let replacing = $state<Partial<Record<ServarrApp, boolean>>>({});

  let requesterOptions = $derived(
    [...new Set([...users, ...form.requesters])].sort().map((id) => ({ value: id, label: id })),
  );

  function keep(app: ServarrApp) {
    replacing[app] = false;
    form.apps[app].key = '';
  }

  function fieldLabel(field: string): string {
    const text = field.replace(/_/g, ' ');
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  // Read once: an `open` bound to live values would shut the section under
  // the admin as they clear its last field.
  const defaultsOpen = untrack(() =>
    Object.fromEntries(
      SERVARR_APPS.map((app) => [
        app,
        Object.values(form.defaults[app]).some((value) => value.trim() !== ''),
      ]),
    ),
  ) as Record<ServarrApp, boolean>;

  function allowHost(app: ServarrApp, host: string) {
    form.privateHosts = withPrivateHost(form, host);
    const { [`${app}.url`]: _answered, ...rest } = problems;
    problems = rest;
  }
</script>

<!--
  The servarr card's form. A stored API key is never sent to the browser, so
  the key input starts empty and an empty input keeps the stored key; "Replace
  key" reveals it. What the card does with these values is in servarrForm.ts.
-->
<div class="servarr-form">
  {#each SERVARR_APPS as name (name)}
    {@const app = form.apps[name]}
    {@const label = APP_LABELS[name]}
    {@const need = app.enabled ? privateHostNeed(app.url, form.privateHosts) : null}
    <fieldset class="app" data-testid={`servarr-edit-${name}`}>
      <legend class="app-name">{label}</legend>
      <Field label="Enabled" checkbox>
        <input type="checkbox" bind:checked={form.apps[name].enabled} />
      </Field>
      {#if app.enabled}
        <Field
          label="URL"
          hint={`The address Istota reaches ${label} at, from the machine it runs on.`}
          error={problems[`${name}.url`]}
          warning={problems[`${name}.url`] ? undefined : need?.message}
        >
          <Input
            bind:value={form.apps[name].url}
            monospace
            autocomplete="off"
            spellcheck="false"
            placeholder={`http://192.168.1.10:${DEFAULT_PORTS[name]}`}
            invalid={!!problems[`${name}.url`]}
          />
        </Field>
        {#if need}
          <div class="key-state">
            <Button variant="secondary" size="sm" onclick={() => allowHost(name, need.host)}
              >Add {need.host} to private hosts</Button
            >
          </div>
        {/if}
        {#if app.keyReadOnly}
          <Field label="API key" labelled={false}>
            <div class="key-state">
              <Badge variant={app.keySet === true ? 'success' : 'warn'}
                >{app.keySet === true ? 'set' : 'not set'}</Badge
              >
              <span class="caption">{app.keyReadOnly}</span>
            </div>
          </Field>
        {:else if app.keySet === true && !replacing[name]}
          <Field label="API key" labelled={false}>
            <div class="key-state">
              <Badge variant="success">set</Badge>
              <Button variant="secondary" size="sm" onclick={() => (replacing[name] = true)}
                >Replace key</Button
              >
            </div>
          </Field>
        {:else}
          <Field
            label={app.keySet === true ? 'New API key' : 'API key'}
            hint={`In ${label}, under Settings → General → Security.`}
            error={problems[`${name}.key`]}
          >
            <Input
              type="password"
              bind:value={form.apps[name].key}
              autocomplete="new-password"
              invalid={!!problems[`${name}.key`]}
            />
          </Field>
          {#if app.keySet === true}
            <div class="key-state">
              <Button variant="secondary" size="sm" onclick={() => keep(name)}
                >Keep the stored key</Button
              >
            </div>
          {/if}
        {/if}
        {#if DEFAULT_FIELDS[name].length > 0}
          <details class="defaults" open={defaultsOpen[name]}>
            <summary>Add defaults</summary>
            <p class="caption">
              Optional. Used when a request does not name them. With none set, a request uses the
              app's only option or asks for one.
            </p>
            {#each DEFAULT_FIELDS[name] as field (field)}
              <Field label={fieldLabel(field)}>
                <Input bind:value={form.defaults[name][field]} placeholder="not set" />
              </Field>
            {/each}
          </details>
        {/if}
      {:else}
        <p class="caption">Not connected.</p>
      {/if}
    </fieldset>
  {/each}

  <Field
    label="Private hosts"
    hint="One host name or IP address per line. Plain HTTP goes only to these, and so does a host with a private address."
  >
    <TextArea bind:value={form.privateHosts} rows={2} monospace />
  </Field>

  <Field
    label="Requesters"
    labelled={false}
    hint="Users besides admins who may search, request with the defaults and follow the queue. Admins always can."
  >
    <MultiSelect
      bind:value={form.requesters}
      options={requesterOptions}
      ariaLabel="Requesters"
      emptyLabel="Admins only"
    />
  </Field>
</div>

<style>
  /* The four app fieldsets sit side by side as the card widens, matching the
     tiles they edit; the install-wide fields under them run full width. */
  .servarr-form {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 14rem), 1fr));
    gap: var(--space-3);
  }

  .servarr-form > :global(:not(.app)) {
    grid-column: 1 / -1;
  }

  .app {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
    margin: 0;
    padding: var(--space-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-raised);
  }

  /* Floated so the name sits inside the tile like the read view's, rather
     than on the fieldset's border line. */
  .app-name {
    float: left;
    width: 100%;
    padding: 0;
    margin-bottom: var(--space-1);
    font-size: var(--text-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .caption {
    margin: 0;
  }

  .key-state {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .defaults summary {
    cursor: pointer;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  .defaults > :global(* + *) {
    margin-top: var(--space-2);
  }
</style>
