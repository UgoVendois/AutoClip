<script>
  // Nom d'une chaîne, cliquable vers sa page publique quand elle est connue.
  import Icon from './Icon.svelte';

  let { name, url = null, class: klass = '' } = $props();
</script>

{#if url}
  <a class="inline-flex items-center gap-1 hover:text-accent hover:underline {klass}" href={url} target="_blank"
     rel="noreferrer" title="Ouvrir la chaîne sur YouTube">
    {name}<Icon name="external" size={12} class="opacity-60" />
  </a>
{:else}
  <span class={klass}>{name}</span>
{/if}
