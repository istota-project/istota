<!--
  One location share under an answer: whose it is, an interactive map of the
  shared point or area, and the map link the answer's text carries. The map
  draws exactly what that text discloses. When it cannot be drawn (no WebGL, a
  failed style or tile, a latitude Web Mercator has no row for) the card keeps
  its line and link and drops the frame, rather than leaving an empty canvas.
-->
<script lang="ts">
  import { MapPin } from '@lucide/svelte';
  import ShareMap from '$lib/components/location/ShareMap.svelte';
  import { isMercatorSafe } from '$lib/location/shareMap';
  import { isWebUrl, type LocationShareCardData } from '$lib/stores/segments';

  let { share }: { share: LocationShareCardData } = $props();

  let failed = $state(false);

  const showMap = $derived(!failed && isMercatorSafe(share.observation.lat));
  const precisionLabel = $derived(
    share.observation.precision === 'area' ? 'Approximate area' : 'Exact location',
  );
  const href = $derived(isWebUrl(share.mapUrl) ? share.mapUrl : null);

  function onReady(result: { ok: boolean }) {
    if (!result.ok) failed = true;
  }
</script>

<div class="location-share-card" data-testid="location-share-card">
  <div class="location-share-head caption">
    <span class="location-share-mark" aria-hidden="true"><MapPin size={13} /></span>
    <span class="location-share-owner">{share.ownerLabel}</span>
    <span>· {precisionLabel}</span>
    {#if href}
      <a class="location-share-link" {href} target="_blank" rel="noopener noreferrer">Open map</a>
    {/if}
  </div>
  {#if showMap}
    <div class="location-share-frame embed embed-fixed">
      <ShareMap
        observation={share.observation}
        interactive
        ownerLabel={share.ownerLabel}
        {onReady}
      />
    </div>
  {/if}
</div>

<style>
  /* The mail card's chrome (`MailCard.svelte`), so the two cards an answer can
     carry read as one family. */
  /* As wide as the embed box, so the map inside it is drawn at embed size. */
  .location-share-card {
    width: 100%;
    max-width: var(--embed-max-width);
    padding: var(--space-2);
    background: var(--surface-reading-card);
    border-left: 2px solid var(--border-hover);
    border-radius: var(--radius-sm);
  }
  .location-share-head {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .location-share-mark {
    display: inline-flex;
    align-self: center;
    color: var(--text-muted);
  }
  .location-share-owner {
    color: var(--text-secondary);
    font-weight: var(--weight-semibold);
  }
  .location-share-link {
    margin-left: auto;
    color: var(--link);
  }
  .location-share-link:hover {
    text-decoration: underline;
  }
  /* Sized by `.embed-fixed` (primitives.css), the box every chat embed shares. */
  .location-share-frame {
    margin-top: var(--space-2);
    overflow: hidden;
    border-radius: var(--radius-sm);
  }
</style>
