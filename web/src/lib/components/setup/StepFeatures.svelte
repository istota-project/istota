<script lang="ts">
  import { Field } from '$lib/components/ui';
  import {
    applyFeatureDefaults,
    BROWSER_MEMORY_WARN_MIB,
    BROWSER_PROFILE,
    MODULE_FEATURES,
    moduleTitle,
    type SetupDraftStore,
  } from './setupDraft.svelte';

  interface Props {
    draft: SetupDraftStore;
    errors: Record<string, string>;
    botName: string;
    /** The module toggles this image can run, from `/state`. */
    modules?: { field: string; module: string }[];
    /** The machine's memory, from `/state`; 0 when it could not be read. */
    memoryMib?: number;
  }

  let { draft, errors, botName, modules = [], memoryMib = 0 }: Props = $props();

  // Once, when the screen opens.
  function open() {
    applyFeatureDefaults(draft, modules);
  }
  open();

  const browserOn = $derived(draft.list('profiles').includes(BROWSER_PROFILE));

  function setBrowser(on: boolean) {
    const others = draft.list('profiles').filter((p) => p !== BROWSER_PROFILE);
    draft.set('profiles', on ? [...others, BROWSER_PROFILE] : others);
  }

  function gigabytes(mib: number): string {
    return (mib / 1024).toFixed(1).replace(/\.0$/, '');
  }

  const memoryWarning = $derived(
    memoryMib > 0 && memoryMib < BROWSER_MEMORY_WARN_MIB
      ? `This server has ${gigabytes(memoryMib)} GB of memory. The browser uses up to 2 GB of it; you can turn it off if ${botName} runs slowly.`
      : undefined,
  );

  const checked = (e: Event) => (e.currentTarget as HTMLInputElement).checked;
</script>

<div class="step">
  <p>Everything is on to start with. Turn off what you won't use.</p>
  {#each modules as { field, module } (field)}
    <div class="feature">
      <Field label={moduleTitle(module)} checkbox error={errors[field]}>
        <input
          type="checkbox"
          checked={draft.flag(field, true)}
          onchange={(e: Event) => draft.set(field, checked(e))}
        />
      </Field>
      {#if MODULE_FEATURES[module]}
        <p class="caption">{MODULE_FEATURES[module].description}</p>
      {/if}
    </div>
  {/each}
  <div class="feature">
    <Field
      label="Let {botName} browse the web"
      checkbox
      warning={memoryWarning}
      error={errors.profiles}
    >
      <input type="checkbox" checked={browserOn} onchange={(e: Event) => setBrowser(checked(e))} />
    </Field>
    <p class="caption">{botName} can open websites and read them for you.</p>
  </div>
</div>

<style>
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .feature {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  p {
    margin: 0;
  }
</style>
