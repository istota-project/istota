<script lang="ts">
  import { Field, Checkbox } from '$lib/components/ui';
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
    /** A re-run: the module toggles are the user's settings, shown and kept. */
    rerun?: boolean;
  }

  let { draft, errors, botName, modules = [], memoryMib = 0, rerun = false }: Props = $props();

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
</script>

<div class="vstack">
  {#if rerun}
    <p>
      These features are settings of your own account. Change these in Settings. Web browsing is the
      server's, and can be changed here.
    </p>
  {:else}
    <p>Everything is on to start with. Turn off what you won't use.</p>
  {/if}
  {#each modules as { field, module } (field)}
    <Field
      label={moduleTitle(module)}
      checkbox
      description={MODULE_FEATURES[module]?.description}
      error={errors[field]}
    >
      <Checkbox
        checked={draft.flag(field, true)}
        disabled={rerun}
        onchange={(on) => draft.set(field, on)}
      />
    </Field>
  {/each}
  <Field
    label="Let {botName} browse the web"
    checkbox
    description="{botName} can open websites and read them for you."
    warning={memoryWarning}
    error={errors.profiles}
  >
    <Checkbox checked={browserOn} onchange={(on) => setBrowser(on)} />
  </Field>
</div>

<style>
  p {
    margin: 0;
  }
</style>
