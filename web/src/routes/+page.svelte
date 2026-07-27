<script lang="ts">
  import { base } from '$app/paths';
  import { getMe, type User } from '$lib/api';
  import { onMount } from 'svelte';
  import { HeartPulse, MapPin, MessageSquare, Newspaper, Rss, Wallet } from 'lucide-svelte';

  let user: User | null = $state(null);

  onMount(async () => {
    try {
      user = await getMe();
    } catch {
      // layout handles auth redirect
    }
  });
</script>

<div class="dashboard">
  {#if user}
    <h1>Dashboard</h1>
    <div class="feature-grid card-grid" style="--card-min: 200px; --card-gap: 1rem;">
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
  {/if}
</div>

<style>
  .dashboard h1 {
    font-size: 1.1rem;
    font-weight: 600;
    margin: 0 0 1.5rem;
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
    gap: 0.5rem;
    font-weight: 600;
    font-size: 0.9rem;
    margin-bottom: 0.25rem;
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
