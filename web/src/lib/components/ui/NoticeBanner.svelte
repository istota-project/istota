<!-- Deprecated: `Alert appearance="bordered"`, kept until its call sites move. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import Alert from './Alert.svelte';

  interface Props {
    title: string;
    variant?: 'info' | 'warn' | 'danger';
    collapsed?: boolean;
    children?: Snippet;
  }

  let { title, variant = 'warn', collapsed = $bindable(true), children }: Props = $props();
</script>

<Alert appearance="bordered" {title} {variant} bind:collapsed {children} />
