<script>
  let { title, description = '', icon = null, children, actions = null } = $props();
  import Icon from './Icon.svelte';
</script>

<section class="card p-6">
  <div class="mb-5 flex items-start justify-between gap-4">
    <div>
      <h2 class="flex items-center gap-2 text-[15px] font-semibold">
        {#if icon}<Icon name={icon} size={16} class="text-accent" />{/if}
        {title}
      </h2>
      {#if description}<p class="mt-1 text-sm text-muted">{description}</p>{/if}
    </div>
    {#if actions}<div class="shrink-0">{@render actions()}</div>{/if}
  </div>
  {@render children()}
</section>
