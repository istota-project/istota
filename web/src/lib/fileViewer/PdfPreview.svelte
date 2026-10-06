<script lang="ts">
  import { base } from '$app/paths';
  import { version } from 'pdfjs-dist/package.json';
  import type { PDFDocumentProxy, PDFDocumentLoadingTask, RenderTask } from 'pdfjs-dist';
  import { Button } from '$lib/components/ui';

  let { url }: { url: string } = $props();
  let document = $state.raw<PDFDocumentProxy | null>(null);
  let pageNumber = $state(1);
  let error = $state('');
  let rendering = $state(false);
  let surface = $state<HTMLDivElement>();
  let width = $state(0);

  $effect(() => {
    const requested = url;
    let active = true;
    let loading: PDFDocumentLoadingTask | undefined;
    document = null;
    error = '';
    pageNumber = 1;
    async function load() {
      try {
        // PDF.js 6 no longer generates/evaluates JS for fonts/functions. Only
        // its canvas API is loaded: no scripting, annotation or XFA layer.
        const [pdf, worker] = await Promise.all([
          import('pdfjs-dist'),
          import('pdfjs-dist/build/pdf.worker.min.mjs?url'),
        ]);
        if (!active) return;
        pdf.GlobalWorkerOptions.workerSrc = worker.default;
        loading = pdf.getDocument({
          url: requested,
          wasmUrl: `${base}/pdfjs/${version}/wasm/`,
          cMapUrl: `${base}/pdfjs/${version}/cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${base}/pdfjs/${version}/standard_fonts/`,
          stopAtErrors: true,
          enableXfa: false,
          useWorkerFetch: false,
          useWasm: false,
          disableAutoFetch: true,
          disableStream: true,
          maxImageSize: 8_000_000,
          canvasMaxAreaInBytes: 32_000_000,
        });
        const loaded = await loading.promise;
        if (active) document = loaded;
      } catch (e) {
        if (!active) return;
        const name = e instanceof Error ? e.name : '';
        error =
          name === 'PasswordException'
            ? 'This PDF needs a password. Download it to open in another app.'
            : name === 'InvalidPDFException'
              ? 'This PDF is invalid. Try downloading the original file.'
              : "Couldn't load this PDF. Try downloading the original file.";
      }
    }
    void load();
    return () => {
      active = false;
      void loading?.destroy().catch(() => {});
    };
  });

  $effect(() => {
    const pdf = document;
    const pageIndex = pageNumber;
    const host = surface;
    const available = width;
    if (!pdf || !host) return;
    let active = true;
    let task: RenderTask | undefined;
    // A fresh canvas per render prevents a cancelled job and its replacement
    // from sharing a backing store while cancellation finishes asynchronously.
    const canvas = window.document.createElement('canvas');
    canvas.setAttribute('aria-label', `PDF page ${pageIndex}`);
    canvas.setAttribute('role', 'img');
    host.replaceChildren(canvas);
    rendering = true;
    async function draw() {
      let page;
      try {
        page = await pdf!.getPage(pageIndex);
        if (!active) return;
        const original = page.getViewport({ scale: 1 });
        if (!(
          Number.isFinite(original.width) &&
          Number.isFinite(original.height) &&
          original.width > 0 &&
          original.height > 0
        ))
          throw new Error('Invalid page size');
        const fit = Math.max(1, available || 800) / original.width;
        const scale = Math.min(
          fit * Math.min(window.devicePixelRatio || 1, 2),
          4096 / original.width,
          4096 / original.height,
          Math.sqrt(8_000_000 / (original.width * original.height)),
        );
        const viewport = page.getViewport({ scale });
        canvas.width = Math.max(1, Math.floor(viewport.width));
        canvas.height = Math.max(1, Math.floor(viewport.height));
        canvas.style.width = `${Math.min(available || 800, viewport.width)}px`;
        task = page.render({ canvas, viewport, annotationMode: 0 });
        await task.promise;
      } catch {
        if (active) error = "Couldn't render this PDF page. Try downloading the original file.";
      } finally {
        // The page can be reused by a rapid Previous/Next. cleanup() refuses
        // while another render still owns it; document.destroy handles close.
        page?.cleanup();
        if (active) rendering = false;
      }
    }
    void draw();
    return () => {
      active = false;
      task?.cancel();
      canvas.remove();
    };
  });
</script>

{#if error}
  <p role="alert">{error}</p>
{:else}
  {#if document}
    <div class="pdf-controls">
      <Button ariaLabel="Previous page" disabled={pageNumber === 1} onclick={() => pageNumber--}
        >Previous</Button
      >
      <span>Page {pageNumber} of {document.numPages}</span>
      <Button
        ariaLabel="Next page"
        disabled={pageNumber === document.numPages}
        onclick={() => pageNumber++}>Next</Button
      >
    </div>
  {/if}
  {#if !document || rendering}<p class="muted" role="status">Loading PDF…</p>{/if}
  <div class="pdf-page" bind:this={surface} bind:clientWidth={width}></div>
{/if}

<style>
  .pdf-controls {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }
  .pdf-page {
    width: 100%;
  }
  .pdf-page :global(canvas) {
    display: block;
    max-width: 100%;
    height: auto;
    margin-inline: auto;
  }
</style>
