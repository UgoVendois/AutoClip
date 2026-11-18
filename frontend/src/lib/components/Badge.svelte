<script>
  let { status, map } = $props();
  const info = $derived(map[status] ?? { label: status, tone: 'neutral' });
</script>

<span class="badge {info.tone !== 'neutral' ? `badge-${info.tone}` : ''}">
  {#if info.busy}<span class="spinner" style="width:0.6rem;height:0.6rem;border-width:1.5px"></span>{/if}
  {info.label}
</span>
