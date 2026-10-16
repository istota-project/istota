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
  <div class="location-share-head">
    <span class="location-share-mark" aria-hidden="true"><MapPin size={13} /></span>
    <span class="location-share-owner">{share.ownerLabel}</span>
    <span>· {precisionLabel}</span>
    {#if href}
      <a class="location-share-link" {href} target="_blank" rel="noopener noreferrer">Open map</a>
    {/if}
  </div>
  {#if showMap}
    <div class="location-share-frame">
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
  .location-share-card {
    width: 100%;
    /* design-lint-allow: the card's fixed map size from the spec, not a spacing step. */
    max-width: 360px;
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
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
  .location-share-mark {
    display: inline-flex;
    align-self: center;
    color: var(--text-muted);
  }
  .location-share-owner {
    color: var(--text-secondary);
    font-weight: 600;
  }
  .location-share-link {
    margin-left: auto;
    color: var(--link);
  }
  .location-share-link:hover {
    text-decoration: underline;
  }
  .location-share-frame {
    height: 200px;
    margin-top: var(--space-2);
    overflow: hidden;
    border-radius: var(--radius-sm);
  }
</style>
