<script lang="ts">
  import { useHeaderSave } from '@minot/ui/stores/headerSave';

  useHeaderSave(() => ({ dirty: true, saving: false, save: async () => {} }));
</script>
