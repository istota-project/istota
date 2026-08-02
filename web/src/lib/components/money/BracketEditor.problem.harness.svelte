<script lang="ts">
  import BracketEditor from './BracketEditor.svelte';

  let { value = [] }: { value?: number[][] } = $props();

  // The taxes settings page's exact shape: two editors reporting into one
  // record of problems, each write producing a fresh object. A callback that
  // *reads* parent state cannot be invoked from a tracked effect in the child,
  // or the child's effect becomes a dependent of what the parent writes.
  let problems = $state<Record<string, string>>({});
</script>

<BracketEditor
  {value}
  onchange={() => {}}
  onproblem={(p) => (problems = { ...problems, federal: p })}
/>
<BracketEditor
  {value}
  onchange={() => {}}
  onproblem={(p) => (problems = { ...problems, state: p })}
/>

<output data-testid="problems">{JSON.stringify(problems)}</output>
