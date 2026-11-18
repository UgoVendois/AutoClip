<script>
  import { app, setTheme } from '../state.svelte.js';
  import Icon from './Icon.svelte';

  const THEMES = [
    { value: 'system', icon: 'monitor', label: 'Système' },
    { value: 'light', icon: 'sun', label: 'Clair' },
    { value: 'dark', icon: 'moon', label: 'Sombre' },
  ];
  let { labels = false } = $props();
</script>

<div class="flex gap-0.5 rounded-xl bg-surface-2 p-0.5" role="radiogroup" aria-label="Thème">
  {#each THEMES as t}
    {@const active = (app.settings?.ui_theme ?? 'system') === t.value}
    <button
      role="radio"
      aria-checked={active}
      title={t.label}
      class="flex h-7 flex-1 items-center justify-center gap-1.5 rounded-[0.6rem] text-xs font-medium transition-colors
        {active ? 'bg-surface text-ink shadow-sm' : 'text-muted hover:text-ink'}"
      onclick={() => setTheme(t.value)}
    >
      <Icon name={t.icon} size={14} />
      {#if labels}{t.label}{/if}
    </button>
  {/each}
</div>
