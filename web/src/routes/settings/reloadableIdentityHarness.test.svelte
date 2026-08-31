<script lang="ts">
  /* Test-only harness: `lib/currentUserHarness.test.svelte` with a `reload()`
     that does what the root layout's does — publish a fresh record — rather
     than merely reporting whether the server answered.

     The shared harness is right for a page that needs an identity to exist.
     It is not enough for one whose behaviour turns on the record *changing*:
     the profile-picture control adopts an upload's own hash and then drops it
     again once `reload()` has put the same value on the shared record, and
     against a `reload()` that publishes nothing that reads as the preview
     reverting to the old picture.

     Not collected as a test: vitest picks up `*.test.ts` only, so this is
     compiled solely by the test that imports it. */
  import { untrack, type Component } from 'svelte';
  import type { User } from '$lib/api';
  import { setCurrentUser } from '$lib/userContext';

  interface Props {
    user: User;
    /**
     * What the server answers a `reload()` with. `null` stands for a server
     * that did not answer, which the layout reports as `false` while leaving
     * the record alone; omitting it keeps the record unchanged and still
     * reports success, which is the ordinary no-op reload.
     */
    onReload?: () => User | null;
    onExpireSession?: () => void;
    component: Component<Record<string, never>>;
  }

  let { user, onReload, onExpireSession, component: Page }: Props = $props();

  /* Seeded from the prop and owned from then on — `reload()` is what replaces
     it, the way the root layout replaces the record it publishes. `untrack`
     says that on purpose: reading a prop into `$state` captures its initial
     value, and here that is the whole point rather than a mistake. */
  let current = $state(untrack(() => user));

  setCurrentUser({
    get user() {
      return current;
    },
    get live() {
      return true;
    },
    expireSession: () => onExpireSession?.(),
    reload: async () => {
      const next = onReload?.();
      if (next) current = next;
      return next !== null;
    },
  });
</script>

<Page />
