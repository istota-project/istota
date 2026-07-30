<script lang="ts">
  import { onMount } from 'svelte';
  import { ChevronLeft, ChevronRight } from 'lucide-svelte';

  let {
    images = [],
    index = null,
    onClose,
  }: {
    images?: string[];
    index?: number | null;
    onClose: () => void;
  } = $props();

  let current = $state<number | null>(null);
  $effect(() => {
    current = index;
  });

  function next(e?: Event) {
    e?.stopPropagation();
    if (current === null || images.length === 0) return;
    current = (current + 1) % images.length;
  }

  function prev(e?: Event) {
    e?.stopPropagation();
    if (current === null || images.length === 0) return;
    current = (current - 1 + images.length) % images.length;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (current === null) return;
    if (e.key === 'Escape') onClose();
    else if (e.key === 'ArrowRight') next();
    else if (e.key === 'ArrowLeft') prev();
  }

  onMount(() => {
    document.addEventListener('keydown', handleKeydown);
    return () => document.removeEventListener('keydown', handleKeydown);
  });
</script>

{#if current !== null && images.length > 0}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="lightbox open" onclick={onClose}>
    <img src={images[current]} alt="" />
    {#if images.length > 1}
      <div class="controls">
        <button class="nav" onclick={prev} aria-label="Previous image">
          <ChevronLeft size={24} />
        </button>
        <div class="counter">{current + 1} / {images.length}</div>
        <button class="nav" onclick={next} aria-label="Next image">
          <ChevronRight size={24} />
        </button>
      </div>
    {/if}
  </div>
{/if}

<style>
  .lightbox {
    position: fixed;
    inset: 0;
    z-index: 100;
    /* design-lint-allow: fixed chrome — the lightbox is a theme-invariant dark
       overlay over the image; darkening is the whole point of the surface. */
    background: rgba(0, 0, 0, 0.9);
    display: flex;
    justify-content: center;
    align-items: center;
    cursor: zoom-out;
  }
  .lightbox img {
    max-width: 90vw;
    /* Leave room for the bottom control bar so it never covers the image, and
		   for the device insets — 5vh of slack is under the Dynamic Island's height
		   on a phone, so without this the top of a tall image sits behind it. */
    max-height: calc(90dvh - 4rem - var(--safe-top) - var(--safe-bottom));
    object-fit: contain;
  }
  /* Full-bleed overlay, so its controls carry their own safe-area offsets. */
  .controls {
    position: absolute;
    bottom: max(1rem, var(--safe-bottom));
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: default;
  }
  .nav {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 2.5rem;
    height: 2.5rem;
    border: none;
    border-radius: 50%;
    /* design-lint-allow-begin: fixed chrome — the lightbox is a theme-invariant
       dark overlay over the image, so its controls stay dark-on-white in both
       themes. */
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    /* design-lint-allow-end */
    cursor: pointer;
    transition: background 120ms;
  }
  .nav:hover {
    /* design-lint-allow: fixed chrome — see .nav above. */
    background: rgba(0, 0, 0, 0.75);
  }
  .counter {
    padding: 0.25rem 0.6rem;
    /* design-lint-allow-begin: fixed chrome — see .nav above. */
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    /* design-lint-allow-end */
    font-size: 0.8rem;
    border-radius: 0.25rem;
    pointer-events: none;
    white-space: nowrap;
  }
</style>
