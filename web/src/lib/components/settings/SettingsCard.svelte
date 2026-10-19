<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    title: string;
    description?: string;
    actions?: Snippet;
    // Rendered beside the title (typically a .status-pill), matching the
    // header layout ServiceCard uses for connected services.
    status?: Snippet;
    children: Snippet;
  }

  let { title, description, actions, status, children }: Props = $props();
</script>

<section class="card">
  <header class="section-header">
    {#if status}
      <div class="hstack">
        <h2 class="title-section">{title}</h2>
        {@render status()}
      </div>
    {:else}
      <h2 class="title-section">{title}</h2>
    {/if}
    {#if actions}
      <div class="hstack">{@render actions()}</div>
    {/if}
  </header>
  {#if description}<p class="description">{description}</p>{/if}
  {@render children()}
</section>
