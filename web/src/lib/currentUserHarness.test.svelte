<script lang="ts">
  /* Test-only harness: mounts a route page under a `CurrentUser` context, the
     way the root layout supplies one in the app (ISSUE-355).

     For tests that are about something else on the page and need the identity
     only to exist. A test of the seam itself mounts the real layout instead —
     see `routes/dashboardInLayout.test.svelte`.

     Not collected as a test: vitest picks up `*.test.ts` only, so this is
     compiled solely by the tests that import it. */
  import type { Component } from 'svelte';
  import type { User } from '$lib/api';
  import { setCurrentUser } from '$lib/userContext';

  interface Props {
    user: User;
    /** Whether the server confirmed the record. Defaults to the ordinary case. */
    live?: boolean;
    component: Component<Record<string, never>>;
  }

  let { user, live = true, component: Page }: Props = $props();

  // A getter, so a test can drive the prop and the page sees the new record —
  // the reactivity the real layout relies on when it upgrades a cached identity
  // to a live one.
  setCurrentUser({
    get user() {
      return user;
    },
    get live() {
      return live;
    },
    reload: async () => live,
  });
</script>

<Page />
