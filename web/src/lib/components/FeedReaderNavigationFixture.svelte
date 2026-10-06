<script lang="ts">
  import type { FeedEntry } from '$lib/api';
  import FeedReader from './FeedReader.svelte';
  import Lightbox from './Lightbox.svelte';
  let {
    entries,
    hasMore = false,
    onNeedMore,
    onView,
    onStarToggle,
  }: {
    entries: FeedEntry[];
    hasMore?: boolean;
    onNeedMore?: () => Promise<void>;
    onView?: (id: number) => void;
    onStarToggle?: (id: number, starred: boolean) => void;
  } = $props();
  let index = $state<number | null>(null);
  let images = $state<string[]>([]);
  let imageIndex = $state<number | null>(null);
</script>

<button onclick={() => (index = 0)}>Open reader</button>
<FeedReader
  {entries}
  {index}
  {hasMore}
  {onNeedMore}
  {onView}
  {onStarToggle}
  onClose={() => (index = null)}
  onImageClick={(urls, i) => {
    images = urls;
    imageIndex = i;
  }}
/>
<Lightbox {images} index={imageIndex} onClose={() => (imageIndex = null)} />
