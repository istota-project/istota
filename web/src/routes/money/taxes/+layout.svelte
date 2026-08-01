<script lang="ts">
  let { children } = $props();
</script>

<!-- The frame sits *inside* the scroller, not on it, so the scrollbar stays at
     the pane's edge, and the section header above stays full-bleed — the same
     shape portfolio and reports use. Taxes belongs with them rather than with
     the record-table tabs: it is a two-column stat and explanation layout, so
     on a wide monitor an uncapped column stretched its summary rows across the
     whole pane. `.tax-content` inside is the growing flex column that carries
     the page's own inset. -->
<div class="money-section-body">
  <div class="content-frame">
    {@render children()}
  </div>
</div>
