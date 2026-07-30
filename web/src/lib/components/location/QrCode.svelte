<script lang="ts">
  /**
   * A QR code, rendered as inline SVG from the module matrix.
   *
   * `uqr` also ships a `renderSVG` that returns markup, which would mean
   * `{@html}` — a sink this page has no reason to open when the alternative is
   * a loop over booleans. Every module is a `<rect>`; nothing here interpolates
   * a string into the DOM.
   *
   * Drawn in module units with a `viewBox`, so the caller sizes it in CSS and
   * the code stays crisp at any size. The quiet zone (4 modules, per the spec)
   * is part of the viewBox rather than padding, because a scanner needs it to
   * be the same colour as the code's background and CSS padding on an inline
   * SVG is not.
   *
   * Nothing about this is location-specific; it lives here because there is one
   * consumer. Promoting it to `ui/` is a move, not a rewrite.
   */
  import { encode } from 'uqr';

  interface Props {
    value: string;
    /** Rendered size. Anything under ~180px asks a phone camera to work hard. */
    size?: string;
    label?: string;
  }

  let { value, size = '13rem', label = 'QR code' }: Props = $props();

  const QUIET_ZONE = 4;

  const matrix = $derived(encode(value));
  const extent = $derived(matrix.size + QUIET_ZONE * 2);
</script>

<svg
  class="qr"
  style:width={size}
  style:height={size}
  viewBox="0 0 {extent} {extent}"
  role="img"
  aria-label={label}
  shape-rendering="crispEdges"
>
  <!-- design-lint-allow-begin: a QR code is dark-on-light or it does not scan.
       These two are the format, not the palette — theming them would produce a
       code most readers refuse, and the light modules are the background here
       so only the dark ones are drawn. -->
  <rect x="0" y="0" width={extent} height={extent} fill="#ffffff" />
  {#each matrix.data as row, y (y)}
    {#each row as filled, x (x)}
      {#if filled}
        <rect x={x + QUIET_ZONE} y={y + QUIET_ZONE} width="1" height="1" fill="#000000" />
      {/if}
    {/each}
  {/each}
  <!-- design-lint-allow-end -->
</svg>

<style>
  .qr {
    display: block;
    border-radius: var(--radius-card);
    /* The white field is the quiet zone; a border keeps it from bleeding into
       a light-theme card and losing its edge. */
    border: 1px solid var(--border-subtle);
  }
</style>
