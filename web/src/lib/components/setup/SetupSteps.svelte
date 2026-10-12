<script lang="ts">
  import { SETUP_STEPS, stepInfo, stepPosition, type SetupStepId } from './setupDraft.svelte';

  interface Props {
    current: SetupStepId;
  }

  let { current }: Props = $props();

  const position = $derived(stepPosition(current));
  const title = $derived(stepInfo(current).title);
</script>

<!--
  Numbered dots joined by a line, with the current step's name under them.
  Below 640px eight dots no longer fit beside their labels, so the row gives
  way to one line of text saying the same thing.
-->
<nav class="setup-steps" aria-label="Setup progress">
  <ol class="dots">
    {#each SETUP_STEPS as step, index (step.id)}
      <li
        class="dot"
        class:done={index + 1 < position}
        class:current={index + 1 === position}
        aria-current={index + 1 === position ? 'step' : undefined}
      >
        <span class="number" title={step.title}>{index + 1}</span>
      </li>
    {/each}
  </ol>
  <p class="label">Step {position} of {SETUP_STEPS.length} · {title}</p>
</nav>

<style>
  .setup-steps {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
  }

  .dots {
    display: flex;
    align-items: center;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .dot {
    display: flex;
    align-items: center;
  }

  /* The joining line, drawn before every dot but the first. */
  .dot + .dot::before {
    content: '';
    width: 1.5rem;
    height: 1px;
    background: var(--border-default);
  }

  .dot.done + .dot::before,
  .dot.done + .dot.current::before {
    background: var(--accent-blue);
  }

  .number {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.5rem;
    height: 1.5rem;
    border: 1px solid var(--border-default);
    border-radius: 50%;
    font-size: var(--text-xs);
    color: var(--text-muted);
    background: var(--surface-card);
  }

  .done .number {
    border-color: var(--accent-blue);
    color: var(--accent-blue);
  }

  .current .number {
    border-color: var(--accent-blue);
    background: var(--accent-blue);
    color: var(--on-accent-fg);
    font-weight: 600;
  }

  .label {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  @media (max-width: 640px) {
    .dots {
      display: none;
    }
  }
</style>
