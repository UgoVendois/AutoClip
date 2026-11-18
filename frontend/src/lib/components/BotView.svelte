<script>
  import { onMount } from 'svelte';
  import { api } from '../api.js';
  import { app, toast } from '../state.svelte.js';
  import ChannelTab from './ChannelTab.svelte';
  import ConfigTab from './ConfigTab.svelte';
  import Icon from './Icon.svelte';
  import QueueTab from './QueueTab.svelte';
  import StatsTab from './StatsTab.svelte';

  let bot = $state(null);
  let tick = $state(0); // rafraîchissement périodique des onglets

  async function load() {
    try {
      bot = await api.get(`/bots/${app.selectedId}`);
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  onMount(() => {
    load();
    const timer = setInterval(() => {
      tick += 1;
      load();
    }, 4000);
    return () => clearInterval(timer);
  });

  const TABS = [
    { id: 'config', label: 'Configuration', icon: 'sliders' },
    { id: 'queue', label: "File d'attente", icon: 'list' },
    { id: 'channel', label: 'Chaîne', icon: 'palette' },
    { id: 'stats', label: 'Statistiques', icon: 'chart' },
  ];
</script>

{#if bot}
  <div class="mx-auto max-w-5xl px-8 pb-16 pt-8">
    <header class="flex flex-wrap items-center gap-3">
      <h1 class="min-w-0 truncate font-serif text-3xl tracking-tight">{bot.name}</h1>
      {#if bot.dry_run}
        <span class="badge" title="Tout s'exécute, mais rien n'est publié">Dry run</span>
      {:else}
        <span class="badge badge-success"><Icon name="send" size={11} /> Publication réelle</span>
      {/if}
    </header>

    <div class="mt-6 flex gap-1 border-b border-line" role="tablist">
      {#each TABS as t}
        {@const active = app.tab === t.id}
        <button
          role="tab"
          aria-selected={active}
          class="-mb-px flex items-center gap-2 border-b-2 px-3 pb-2.5 pt-1 font-medium transition-colors
            {active ? 'border-accent text-ink' : 'border-transparent text-muted hover:text-ink'}"
          onclick={() => (app.tab = t.id)}
        >
          <Icon name={t.icon} size={15} />
          {t.label}
          {#if t.id === 'queue' && bot.stats.review}
            <span class="badge badge-accent h-5 px-1.5">{bot.stats.review}</span>
          {/if}
        </button>
      {/each}
    </div>

    <div class="pt-6" role="tabpanel">
      {#if app.tab === 'config'}
        <ConfigTab {bot} reload={load} />
      {:else if app.tab === 'queue'}
        <QueueTab {bot} {tick} reload={load} />
      {:else if app.tab === 'channel'}
        <ChannelTab {bot} {tick} />
      {:else}
        <StatsTab {bot} {tick} />
      {/if}
    </div>
  </div>
{:else}
  <div class="grid h-full place-items-center text-muted"><span class="spinner"></span></div>
{/if}
