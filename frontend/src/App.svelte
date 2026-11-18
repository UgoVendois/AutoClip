<script>
  import { onMount } from 'svelte';
  import { api } from './lib/api.js';
  import BotView from './lib/components/BotView.svelte';
  import Icon from './lib/components/Icon.svelte';
  import Overlays from './lib/components/Overlays.svelte';
  import SettingsView from './lib/components/SettingsView.svelte';
  import Sidebar from './lib/components/Sidebar.svelte';
  import { app, loadSettings, refreshBots, run, selectBot } from './lib/state.svelte.js';

  let status = $state('loading'); // loading | ready | offline

  async function boot() {
    status = 'loading';
    try {
      await Promise.all([loadSettings(), refreshBots()]);
      status = 'ready';
    } catch {
      status = 'offline';
    }
  }

  async function firstBot() {
    const created = await run(api.post('/bots', { name: 'Mon premier bot' }));
    if (created) {
      await refreshBots();
      selectBot(created.id, 'config');
    }
  }

  onMount(() => {
    app.inWindow = Boolean(window.pywebview);
    const onReady = () => (app.inWindow = true);
    window.addEventListener('pywebviewready', onReady);
    boot();
    // Badges « à valider » de la barre latérale.
    const timer = setInterval(() => {
      if (status === 'ready') refreshBots().catch(() => {});
    }, 5000);
    return () => {
      clearInterval(timer);
      window.removeEventListener('pywebviewready', onReady);
    };
  });
</script>

<div class="flex h-screen bg-bg text-ink">
  {#if status === 'ready'}
    <Sidebar />
  {/if}
  <main class="min-w-0 flex-1 overflow-y-auto">
    {#if status === 'loading'}
      <div class="grid h-full place-items-center text-muted"><span class="spinner"></span></div>
    {:else if status === 'offline'}
      <div class="grid h-full place-items-center p-6">
        <div class="card max-w-md p-8 text-center">
          <div class="mx-auto mb-4 grid h-10 w-10 place-items-center rounded-full bg-danger/10 text-danger">
            <Icon name="alert" size={20} />
          </div>
          <h1 class="font-serif text-2xl">Démon injoignable</h1>
          <p class="mt-2 text-muted">
            L'interface ne parvient pas à joindre le démon AutoClip sur ce poste. Lance
            <code class="rounded bg-surface-2 px-1">autoclip app</code> ou
            <code class="rounded bg-surface-2 px-1">systemctl --user start autoclip</code>.
          </p>
          <button class="btn btn-primary mt-6" onclick={boot}><Icon name="refresh" /> Réessayer</button>
        </div>
      </div>
    {:else if app.view === 'settings'}
      <SettingsView />
    {:else if app.selectedId != null}
      {#key app.selectedId}
        <BotView />
      {/key}
    {:else}
      <div class="grid h-full place-items-center p-6">
        <div class="max-w-lg text-center">
          <div class="mx-auto mb-5 grid h-12 w-12 place-items-center rounded-2xl bg-accent text-white">
            <Icon name="film" size={24} />
          </div>
          <h1 class="font-serif text-4xl tracking-tight">Bienvenue dans AutoClip</h1>
          <p class="mt-3 text-[15px] leading-relaxed text-muted">
            Un bot transforme tes vidéos longues en Shorts : il les transcrit, choisit les meilleurs passages,
            les recadre en vertical avec sous-titres, puis les publie selon ton planning, après ta validation.
          </p>
          <button class="btn btn-primary mt-7 h-10 px-5" onclick={firstBot}>
            <Icon name="plus" /> Créer mon premier bot
          </button>
        </div>
      </div>
    {/if}
  </main>
</div>

<Overlays />
