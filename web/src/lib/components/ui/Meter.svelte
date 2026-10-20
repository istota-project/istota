<!--
  A thin bar showing a quantity against a range: one fill, or a stack of
  segments sized against their total. `role="meter"`, named by `label`.
-->
<script lang="ts">
  export interface MeterSegment {
    value: number;
    /** A colour value: a token (`var(--accent)`) or a categorical colour. */
    color: string;
    /** The segment's tooltip, and its part of the meter's spoken value. */
    label?: string;
  }

  interface Props {
    value?: number;
    /** The top of the range; 100 by default, the segments' total with segments. */
    max?: number;
    segments?: MeterSegment[];
    /** The single fill's colour; `--accent` by default. */
    color?: string;
    label: string;
    /** `sm` (`--space-1`, the default) or `xs` (half of it). */
    size?: 'xs' | 'sm';
    /** Placement only. */
    class?: string;
  }

  let {
    value = 0,
    max,
    segments,
    color = 'var(--accent)',
    label,
    size = 'sm',
    class: className = '',
  }: Props = $props();

  const total = $derived(segments ? segments.reduce((sum, s) => sum + s.value, 0) : value);
  const top = $derived(max ?? (segments ? total : 100));
  const valueText = $derived(
    segments?.some((s) => s.label)
      ? segments
          .map((s) => s.label)
          .filter(Boolean)
          .join(', ')
      : undefined,
  );

  function share(n: number): string {
    if (top <= 0) return '0%';
    return `${Math.min(100, Math.max(0, (n / top) * 100))}%`;
  }
</script>

<div
  class="ui-meter size-{size} {className}"
  role="meter"
  aria-label={label}
  aria-valuemin={0}
  aria-valuemax={top}
  aria-valuenow={total}
  aria-valuetext={valueText}
>
  {#if segments}
    {#each segments as seg, i (i)}
      <span
        class="ui-meter-fill"
        style:width={share(seg.value)}
        style:background={seg.color}
        title={seg.label}
      ></span>
    {/each}
  {:else}
    <span class="ui-meter-fill" style:width={share(value)} style:background={color}></span>
  {/if}
</div>

<style>
  .ui-meter {
    display: flex;
    width: 100%;
    overflow: hidden;
    border-radius: var(--radius-pill);
    background: var(--surface-base);
  }
  .size-sm {
    height: var(--space-1);
  }
  .size-xs {
    height: calc(var(--space-1) / 2);
  }
  .ui-meter-fill {
    display: block;
    flex: none;
    height: 100%;
  }
  @media (forced-colors: active) {
    .ui-meter {
      border: 1px solid CanvasText;
    }
    .ui-meter-fill {
      background: Highlight !important;
    }
  }
</style>
