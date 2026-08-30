<script lang="ts">
  import { base } from '$app/paths';
  import { getProfile } from '$lib/api';
  import { onMount } from 'svelte';
  import { HeartPulse, MapPin, MessageSquare, Newspaper, Rss, Wallet } from 'lucide-svelte';
  import { buildGreeting, noteSegments, type Greeting } from '$lib/greeting';
  import { AppShell, ShellHeader } from '$lib/components/ui';
  import { getCurrentUser } from '$lib/userContext';

  /* The identity the root layout resolved, not a second `/me` of this page's
     own (ISSUE-355). Nothing here is network data: every tile is a static link
     gated on `user.features` and the welcome card needs `bot_name` and
     `contact`, so with the fetch here the page had nothing to draw whenever it
     failed — which offline is always, and which is why a cold launch in
     airplane mode landed on a header with an empty frame under it. */
  const identity = getCurrentUser();
  const user = $derived(identity.user);

  /* `null` until the timezone question has been answered, including answered
     with '' — which is what an unreachable `/profile` yields. Kept distinct
     from '' so the card and the tiles land in the same paint rather than the
     greeting being drawn once from the browser clock and again a request
     later. */
  let timeZone = $state<string | null>(null);
  onMount(async () => {
    timeZone = await userTimezone();
  });

  /* Derived rather than built once at mount: the layout swaps a cached identity
     for the live one when the connection returns (ISSUE-354), and a greeting
     pinned at mount would go on speaking as whoever the cache remembered. It
     re-rolls when that happens, which is what it already did on every load. */
  const welcome: Greeting | null = $derived.by(() => {
    if (timeZone === null) return null;
    return buildGreeting(user.bot_name, {
      timeZone,
      // Tips are only shown when they're true of this deployment, so they are
      // gated on the same payload the tiles below are.
      tips: {
        email: user.contact?.email,
        talk: user.contact?.talk,
        features: user.features,
      },
    });
  });

  /* The profile timezone is what the bot itself runs on, so the greeting should
     agree with it rather than with wherever this browser happens to be. It is a
     second request, so a failure just yields '' and `buildGreeting` falls back
     to the browser clock. */
  async function userTimezone(): Promise<string> {
    try {
      const { profile } = await getProfile();
      return profile?.timezone ?? '';
    } catch {
      return '';
    }
  }
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader title="Dashboard" />
  {/snippet}

  <div class="dashboard">
    <!-- One grid, so the tiles flow around the welcome card: it takes the first
       three tracks and whatever fits beside it fills out the row. -->
    <div class="feature-grid card-grid" style="--card-min: 200px; --card-gap: 1rem;">
      {#if welcome}
        <section class="welcome-card">
          <img
            class="welcome-sigil"
            src="{base}/octopus-sigil.webp"
            alt=""
            width="19"
            height="20"
          />
          <div class="welcome-text">
            <p class="welcome-greeting">{welcome.greeting}</p>
            <!-- Rendered segment by segment rather than as one string, so the
               address is a real mailto link. Kept on one line (and off
               prettier) because a break between the tags would put a space
               either side of the link, inside a sentence. -->
            <!-- prettier-ignore -->
            <p class="welcome-note">{#each noteSegments(welcome.note, user.contact?.email) as segment}{#if segment.mailto}<a href="mailto:{segment.mailto}">{segment.text}</a>{:else}{segment.text}{/if}{/each}</p>
          </div>
        </section>
      {/if}
      {#if user.features.chat}
        <a href="{base}/chat" class="feature-card">
          <div class="feature-title"><MessageSquare aria-hidden="true" />Chat</div>
          <div class="feature-desc">Talk to Istota in the app</div>
        </a>
      {/if}
      {#if user.features.briefings}
        <a href="{base}/briefings" class="feature-card">
          <div class="feature-title"><Newspaper aria-hidden="true" />Briefings</div>
          <div class="feature-desc">Your generated briefings and archive</div>
        </a>
      {/if}
      {#if user.features.feeds}
        <a href="{base}/feeds" class="feature-card">
          <div class="feature-title"><Rss aria-hidden="true" />Feeds</div>
          <div class="feature-desc">RSS feed reader</div>
        </a>
      {/if}
      {#if user.features.location}
        <a href="{base}/location" class="feature-card">
          <div class="feature-title"><MapPin aria-hidden="true" />Location</div>
          <div class="feature-desc">GPS tracking and map</div>
        </a>
      {/if}
      {#if user.features.money}
        <a href="{base}/money" class="feature-card">
          <div class="feature-title"><Wallet aria-hidden="true" />Money</div>
          <div class="feature-desc">Accounts, transactions, and reports</div>
        </a>
      {/if}
      {#if user.features.health}
        <a href="{base}/health" class="feature-card">
          <div class="feature-title"><HeartPulse aria-hidden="true" />Health</div>
          <div class="feature-desc">Body stats, bloodwork, and biomarker trends</div>
        </a>
      {/if}
    </div>
  </div>
</AppShell>

<style>
  /* The shell is edge to edge, so the page carries the padding `.app-content`
	   used to give it. The horizontal safe-area insets stay on the shell around
	   this, and the bottom one on its scroll pane, so plain values are right
	   here. */
  .dashboard {
    padding: var(--space-6);
  }

  @media (max-width: 640px) {
    .dashboard {
      padding: var(--space-4) var(--space-3);
    }
  }

  .welcome-card {
    grid-column: 1 / -1;
    display: flex;
    /* Centred rather than top-aligned, because from four columns up the card
		   stretches to its row's height and the two lines would otherwise sit high
		   in a box taller than they are. */
    align-items: center;
    gap: var(--space-3);
    background: var(--surface-card);
    border: 1px solid var(--border-subtle);
    border-left: 2px solid var(--accent-amber);
    border-radius: var(--radius-card);
    padding: 1.25rem;
  }
  /* Three tracks exist from ~760px up (200px min + 1rem gap against the 1.5rem
	   page padding, with room for a scrollbar), so the span can't spill into an
	   implicit fourth column. Below it the card is the full row, like every
	   other card on a phone. Set at 769px, the mobile breakpoint's complement,
	   rather than the 760px it was derived at — a one-off value that close to
	   the shared one leaves a 9px band where this page alone changes layout. */
  @media (min-width: 769px) {
    .welcome-card {
      grid-column: span 3;
    }
  }
  /* Tall enough to span both lines of copy (roughly their two line boxes plus
	   the gap between them). Sized in em, so it keeps that relationship as the
	   text-scale preference moves. --sigil-filter carries the light-theme
	   inversion (see app.css). */
  .welcome-sigil {
    height: 2.75em;
    width: auto;
    flex: none;
    filter: var(--sigil-filter);
  }
  .welcome-text {
    min-width: 0;
  }
  .welcome-greeting {
    margin: 0;
    font-weight: 600;
    font-size: 0.95rem;
    color: var(--text-primary);
  }
  .welcome-note {
    margin: var(--space-1) 0 0;
    font-size: 0.85rem;
    color: var(--text-muted);
    /* A plus-address is one unbroken token and can be long; on a phone it would
		   otherwise run past the card's edge rather than wrap. */
    overflow-wrap: anywhere;
  }
  .welcome-note a {
    color: var(--accent-blue);
    text-decoration: none;
  }
  .welcome-note a:hover {
    text-decoration: underline;
  }

  /* Equal-height cards at every width. Without `grid-auto-rows: 1fr` each grid
	   row sizes to its own content, so a description that wraps to two lines
	   (Health, Money) makes that row taller than the rest — most visible in the
	   single-column phone layout, where every card is its own row.

	   The welcome card is the first item, so `grid-template-rows: auto` exempts
	   the row it leads from that equalisation while rows 2+ stay 1fr. That is
	   what lets it be content-height wherever it holds the row alone (every
	   width up to three columns, the phone layout included) instead of being
	   stretched to a tile's height. From four columns up it shares the row with
	   the tiles that flow around it and stretches to match them, which is what
	   keeps that row looking like the ones below it. */
  .feature-grid {
    grid-template-rows: auto;
    grid-auto-rows: 1fr;
  }
  .feature-card {
    display: block;
    background: var(--surface-card);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-card);
    padding: 1.25rem;
    text-decoration: none;
    transition: background var(--transition-fast);
  }
  .feature-card:hover {
    background: var(--surface-raised);
  }
  .feature-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-weight: 600;
    font-size: 0.9rem;
    margin-bottom: var(--space-1);
    color: var(--text-primary);
  }
  /* Size the glyph in CSS rather than through lucide's `size` prop, which
	   bakes a px number into the width/height attributes and so wouldn't
	   follow the text-scale preference. */
  .feature-title :global(svg) {
    flex: none;
    width: 1.15em;
    height: 1.15em;
    color: var(--accent-amber);
  }
  .feature-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
  }
</style>
