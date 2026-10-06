<script lang="ts">
  let {
    kind,
    url,
    poster,
    failureMessage,
  }: {
    kind: 'audio' | 'video';
    url: string;
    poster?: string;
    failureMessage: string;
  } = $props();
  let failed = $state(false);
  let media = $state<HTMLMediaElement | undefined>();

  $effect(() => {
    kind;
    url;
    failed = false;
  });
  $effect(() => {
    const player = media;
    return () => {
      if (player) {
        player.pause();
        player.removeAttribute('src');
        player.load();
      }
    };
  });
</script>

{#key `${kind}:${url}`}
  {#if failed}
    <p role="alert">{failureMessage}</p>
  {:else if kind === 'audio'}
    <audio bind:this={media} src={url} controls preload="metadata" onerror={() => (failed = true)}
    ></audio>
  {:else}
    <!-- svelte-ignore a11y_media_has_caption -->
    <video
      bind:this={media}
      src={url}
      {poster}
      controls
      preload="metadata"
      playsinline
      onerror={() => (failed = true)}
    ></video>
  {/if}
{/key}

<style>
  audio,
  video {
    display: block;
    width: 100%;
  }
  video {
    max-height: 65dvh;
  }
</style>
