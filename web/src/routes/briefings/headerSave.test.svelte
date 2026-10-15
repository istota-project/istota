<script lang="ts">
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';

  useSettingsSave(() => ({ dirty: true, saving: false, save: async () => {} }));
</script>
