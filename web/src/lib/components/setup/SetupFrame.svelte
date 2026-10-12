<script lang="ts">
  import type { Snippet } from 'svelte';
  import { base } from '$app/paths';
  import { Button } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import SetupSteps from './SetupSteps.svelte';
  import type { SetupStepId } from './setupDraft.svelte';

  interface Props {
    botName: string;
    /** The step indicator's position; none on the unlock and configured screens. */
    step?: SetupStepId | null;
    title: string;
    description?: string;
    /** Omitted on the first step, which has nowhere to go back to. */
    onBack?: () => void;
    onNext?: () => void;
    nextLabel?: string;
    nextLoading?: boolean;
    nextLoadingLabel?: string;
    /** Extra actions beside Next, such as "Continue without testing". */
    actions?: Snippet;
    children: Snippet;
  }

  let {
    botName,
    step = null,
    title,
    description,
    onBack,
    onNext,
    nextLabel = 'Next',
    nextLoading = false,
    nextLoadingLabel = 'Saving…',
    actions,
    children,
  }: Props = $props();
</script>

<!--
  The setup page's column: the sigil and the bot's name as the login card
  shows them, the step indicator, one card per screen, and Back / Next. The
  `settings` shell supplies the card and the column's rhythm; this narrows
  it to one short form.
-->
<div class="settings setup-frame">
  <header class="setup-head">
    <img class="sigil" src="{base}/octopus-sigil.webp" alt="" width="68" height="72" />
    <h1>{botName}</h1>
  </header>

  {#if step}<SetupSteps current={step} />{/if}

  <SettingsCard {title} {description}>
    {@render children()}
  </SettingsCard>

  {#if onBack || onNext || actions}
    <footer class="setup-footer">
      <div class="back">
        {#if onBack}<Button variant="ghost" onclick={onBack}>Back</Button>{/if}
      </div>
      <div class="forward">
        {@render actions?.()}
        {#if onNext}
          <Button
            variant="primary"
            loading={nextLoading}
            loadingLabel={nextLoadingLabel}
            onclick={onNext}>{nextLabel}</Button
          >
        {/if}
      </div>
    </footer>
  {/if}
</div>

<style>
  /* Narrows the settings shell's 980px form column, as /admin widens it. */
  .setup-frame {
    max-width: var(--setup-max);
    padding-top: var(--space-8);
  }

  .setup-head {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-3);
  }

  /* Sized in em with the heading so it tracks the text scale; the filter is
     the light theme's inversion of the flat near-white mark. */
  .sigil {
    height: 3.25em;
    width: auto;
    filter: var(--sigil-filter);
  }

  h1 {
    margin: 0;
    font-size: var(--text-xl);
    font-weight: 600;
    color: var(--text-primary);
  }

  .setup-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
  }

  .forward {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--space-2);
  }

  /* On a phone the buttons stay within reach of a thumb at the bottom of the
     screen, clear of the home indicator. */
  @media (max-width: 640px) {
    .setup-footer {
      position: sticky;
      bottom: 0;
      margin-inline: calc(-1 * var(--space-4));
      padding: var(--space-3) var(--space-4) max(var(--space-4), var(--safe-bottom));
      background: var(--surface-base);
      border-top: 1px solid var(--border-subtle);
    }
  }
</style>
