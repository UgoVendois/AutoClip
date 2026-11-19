<script>
  // Temps restant estimé d'un travail en cours, et bouton pour l'annuler.
  import { fmtEta } from '../format.js';
  import Icon from './Icon.svelte';

  let { eta = null, label = 'Annuler', oncancel, disabled = false } = $props();
</script>

<span class="inline-flex shrink-0 items-center gap-1">
  {#if eta != null}
    <span class="whitespace-nowrap text-xs tabular-nums text-muted" title="Temps restant estimé avant que les clips soient prêts">{fmtEta(eta)}</span>
  {/if}
  <button class="icon-btn danger h-6 w-6" title={label} aria-label={label} {disabled} onclick={oncancel}>
    <Icon name="x" size={13} />
  </button>
</span>
