<script lang="ts">
  import type { ServarrApp } from '$lib/api';
  import { Badge, Button, Field, Input, MultiSelect, TextArea } from '$lib/components/ui';
  import { DEFAULT_FIELDS, SERVARR_APPS, type ServarrForm } from './servarrForm';

  interface Props {
    form: ServarrForm;
    /** From `formProblems`, keyed by `<app>.url` and `<app>.key`. */
    problems: Record<string, string>;
    /** Configured user ids, for the requester picker. */
    users: string[];
  }

  let { form = $bindable(), problems, users }: Props = $props();

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
    return field.replace(/_/g, ' ');
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
    <fieldset class="app" data-testid={`servarr-edit-${name}`}>
      <legend class="app-name">{name}</legend>
      <Field label="Enabled" checkbox>
        <input type="checkbox" bind:checked={form.apps[name].enabled} />
      </Field>
      {#if app.enabled}
        <Field label="URL" error={problems[`${name}.url`]}>
          <Input
            bind:value={form.apps[name].url}
            monospace
            autocomplete="off"
            invalid={!!problems[`${name}.url`]}
          />
        </Field>
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
        {#each DEFAULT_FIELDS[name] as field (field)}
          <Field label={fieldLabel(field)}>
            <Input bind:value={form.defaults[name][field]} placeholder="not set" />
          </Field>
        {/each}
      {/if}
    </fieldset>
  {/each}

  <Field
    label="Private hosts"
    hint="One host name or IP address per line. Plain HTTP goes only to these, and so does a host with a private address."
  >
    <TextArea bind:value={form.privateHosts} rows={2} monospace />
  </Field>

  <Field label="Requesters" labelled={false}>
    <MultiSelect
      bind:value={form.requesters}
      options={requesterOptions}
      ariaLabel="Requesters"
      emptyLabel="Admins only"
    />
  </Field>
</div>

<style>
  .servarr-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .app {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin: 0;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
  }

  .app-name {
    text-transform: capitalize;
    font-weight: 600;
  }

  .key-state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
</style>
