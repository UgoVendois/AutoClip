<script>
  import { api } from '../api.js';
  import { app, askConfirm, refreshBots, run, selectBot } from '../state.svelte.js';
  import Icon from './Icon.svelte';
  import ThemeSwitch from './ThemeSwitch.svelte';

  let editingId = $state(null);
  let draft = $state('');

  async function newBot() {
    const created = await run(api.post('/bots', { name: 'Nouveau bot' }));
    if (!created) return;
    await refreshBots();
    selectBot(created.id, 'config');
    startRename({ id: created.id, name: 'Nouveau bot' });
  }

  function startRename(bot) {
    editingId = bot.id;
    draft = bot.name;
  }

  async function commitRename() {
    if (editingId == null) return;
    const id = editingId;
    editingId = null;
    const name = draft.trim();
    const current = app.bots.find((b) => b.id === id);
    if (!name || name === current?.name) return;
    await run(api.patch(`/bots/${id}`, { name }));
    await refreshBots();
  }

  async function remove(bot) {
    const ok = await askConfirm({
      title: 'Supprimer ce bot ?',
      message: `« ${bot.name} », ses sources, ses clips et son planning seront supprimés. Les vidéos déjà publiées restent en ligne.`,
      confirmLabel: 'Supprimer',
      danger: true,
    });
    if (!ok) return;
    if (await run(api.del(`/bots/${bot.id}`), 'Bot supprimé')) await refreshBots();
  }

  function autofocus(node) {
    node.focus();
    node.select();
  }
</script>

<aside class="flex w-64 shrink-0 flex-col border-r border-line bg-sidebar">
  <div class="flex items-center gap-2.5 px-4 pb-4 pt-5">
    <div class="grid h-7 w-7 place-items-center rounded-lg bg-accent text-white">
      <Icon name="film" size={15} />
    </div>
    <span class="font-serif text-[1.35rem] leading-none tracking-tight">AutoClip</span>
  </div>

  <div class="px-3">
    <button class="btn w-full justify-start" onclick={newBot}>
      <span class="grid h-5 w-5 place-items-center rounded-full bg-accent text-white">
        <Icon name="plus" size={13} />
      </span>
      Nouveau bot
    </button>
  </div>

  <div class="mb-1 mt-6 px-5 text-xs font-medium text-faint">Bots</div>
  <nav class="flex-1 overflow-y-auto px-2 pb-3" aria-label="Bots">
    {#each app.bots as bot (bot.id)}
      {@const selected = app.view === 'bot' && app.selectedId === bot.id}
      {#if editingId === bot.id}
        <input
          class="input my-0.5 h-9"
          aria-label="Nouveau nom du bot"
          bind:value={draft}
          use:autofocus
          maxlength="60"
          onkeydown={(e) => {
            if (e.key === 'Enter') commitRename();
            if (e.key === 'Escape') editingId = null;
          }}
          onblur={commitRename}
        />
      {:else}
        <div
          class="group my-0.5 flex items-center rounded-lg transition-colors
            {selected ? 'bg-sidebar-active text-ink' : 'text-muted hover:bg-sidebar-active/60 hover:text-ink'}"
        >
          <button
            class="flex min-w-0 flex-1 items-center gap-2 py-2 pl-3 text-left"
            onclick={() => selectBot(bot.id)}
            ondblclick={() => startRename(bot)}
            aria-current={selected ? 'page' : undefined}
          >
            <span
              class="h-1.5 w-1.5 shrink-0 rounded-full {bot.dry_run ? 'bg-faint' : 'bg-success'}"
              title={bot.dry_run ? 'Dry run' : 'Publication réelle'}
            ></span>
            <span class="truncate {selected ? 'font-medium' : ''}">{bot.name}</span>
            {#if bot.stats?.review}
              <span class="badge badge-accent ml-auto h-5 px-1.5" title="Clips à valider">{bot.stats.review}</span>
            {/if}
          </button>
          <div class="hidden shrink-0 items-center pr-1 group-hover:flex group-focus-within:flex">
            <button class="icon-btn" title="Renommer" aria-label="Renommer {bot.name}" onclick={() => startRename(bot)}>
              <Icon name="pencil" size={13} />
            </button>
            <button class="icon-btn danger" title="Supprimer" aria-label="Supprimer {bot.name}" onclick={() => remove(bot)}>
              <Icon name="trash" size={13} />
            </button>
          </div>
        </div>
      {/if}
    {:else}
      <p class="px-3 py-2 text-sm text-faint">Aucun bot pour l'instant.</p>
    {/each}
  </nav>

  <div class="space-y-2 border-t border-line p-3">
    <button
      class="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 transition-colors
        {app.view === 'settings' ? 'bg-sidebar-active font-medium text-ink' : 'text-muted hover:bg-sidebar-active/60 hover:text-ink'}"
      onclick={() => (app.view = 'settings')}
    >
      <Icon name="sliders" size={16} />
      Paramètres
    </button>
    <ThemeSwitch />
  </div>
</aside>
