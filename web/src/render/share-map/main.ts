// The share-map renderer: a location share's map as the PNG Talk and WhatsApp
// get. It mounts the same static `ShareMap` the web chat card draws, so the
// picture and the card cannot drift, and settles with ShareMap's own ready
// result.

import { mount } from 'svelte';
import ShareMap from '$lib/components/location/ShareMap.svelte';
import { registerRenderer, type RenderResult } from '../host';
import { parseSnapshotPayload } from './payload';

registerRenderer((raw, frame) => {
  const parsed = parseSnapshotPayload(raw);
  if (!parsed) return { ok: false, reason: 'bad_payload' };
  return new Promise<RenderResult>((resolve) => {
    mount(ShareMap, {
      target: frame,
      props: {
        observation: parsed.observation,
        basemap: parsed.basemap,
        ownerLabel: parsed.ownerLabel,
        interactive: false,
        theme: 'light',
        width: '100%',
        height: '100%',
        onReady: resolve,
      },
    });
  });
});
