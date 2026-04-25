<script lang="ts">
	let { children } = $props();
</script>

<div class="page-shell">
	<div class="page-body">
		{@render children()}
	</div>
</div>
