<script lang="ts">
  import { base } from '$app/paths';
  import { LogOut } from 'lucide-svelte';
  import { ConfirmDialog } from '$lib/components/ui';

  interface Props {
    /**
     * How to leave for the logout endpoint. Injected only so a test can assert
     * the confirm gate actually holds — jsdom refuses a real assignment to
     * `window.location`, and "did it navigate?" is the whole behaviour here.
     */
    navigate?: (url: string) => void;
  }

  // A real navigation, not `goto`: /istota/logout is a server route that clears
  // the session cookie and redirects into the login flow, so it has to leave
  // the SPA.
  let {
    navigate = (url: string) => {
      window.location.href = url;
    },
  }: Props = $props();

  let confirming = $state(false);

  function logout() {
    confirming = false;
    navigate(`${base}/logout`);
  }
</script>

<!--
  Logout is a button rather than a link to /logout because it is gated: the
  control sits next to the menu trigger and both are small touch targets, so a
  mistap used to end the session outright and bounce the user through the OAuth
  flow, losing whatever view state they were in (ISSUE-209). The dialog is the
  guard; the enlarged hit areas in app.css are the other half.
-->
<button
  type="button"
  class="nav-icon-btn logout-btn"
  onclick={() => (confirming = true)}
  title="Log out"
  aria-label="Log out"
>
  <LogOut size={14} />
</button>

<ConfirmDialog
  bind:open={confirming}
  title="Log out"
  message="Are you sure you want to log out? You will need to sign in again to get back in."
  confirmLabel="Log out"
  onConfirm={logout}
/>

<style>
  /* Everything but the resting color is the shared `.nav-icon-btn` rule in
     app.css, alongside its two neighbours in the nav. */
  .logout-btn {
    color: var(--text-dim);
  }
</style>
