<script>
  import { untrack } from 'svelte';
  import { api } from '../api.js';
  import { app, askConfirm, run, toast } from '../state.svelte.js';
  import Icon from './Icon.svelte';
  import Section from './Section.svelte';

  let { bot, tick } = $props();

  const STUDIO = 'https://studio.youtube.com/';

  let ident = $state(null); // dernière proposition
  let playlists = $state([]); // playlists déjà créées sur la chaîne
  let accounts = $state([]);
  let loaded = $state(false);
  let brief = $state('');
  let form = $state(null); // copie modifiable de ident.choice
  let formFor = $state(null); // id de la proposition copiée dans form
  let dirty = $state(false);
  let busy = $state(false);
  let showSafe = $state(true);
  let imgStamp = $state(0);

  const proposal = $derived(ident?.proposal);
  const working = $derived(ident && (ident.status === 'generating' || ident.status === 'applying'));
  const youtubeAccounts = $derived(bot.accounts.filter((a) => a.platform === 'youtube'));
  const targetId = $derived(form?.account_id ?? youtubeAccounts[0]?.id ?? null);
  const target = $derived(accounts.find((a) => a.id === targetId));

  async function load() {
    try {
      const [data, accs] = await Promise.all([api.get(`/bots/${bot.id}/identity`), api.get('/accounts')]);
      const prev = ident;
      ident = data.identity;
      playlists = data.playlists;
      accounts = accs;
      if (ident?.choice && ident.status !== 'generating' && (formFor !== ident.id || !form)) {
        form = structuredClone($state.snapshot(ident.choice));
        form.keywordsText = form.keywords.join(', ');
        for (const pl of form.playlists) pl.keywordsText = pl.keywords.join(', ');
        formFor = ident.id;
        dirty = false;
      }
      if (prev && ident && (prev.avatar_path !== ident.avatar_path || prev.banner_path !== ident.banner_path)) {
        imgStamp = Date.now();
      }
      if (prev?.status === 'applying' && ident?.status === 'applied') toast('Identité appliquée sur la chaîne', 'success');
      if (prev?.status === 'applying' && ident?.status === 'ready' && ident.error) toast(ident.error, 'error');
      loaded = true;
    } catch {
      /* prochain rafraîchissement */
    }
  }

  $effect(() => {
    tick;
    untrack(load);
  });

  function splitList(text) {
    return text.split(',').map((k) => k.trim()).filter(Boolean);
  }

  function choicePayload() {
    return {
      name: form.name,
      description: form.description,
      keywords: splitList(form.keywordsText),
      banner: form.banner,
      account_id: form.account_id ?? null,
      playlists: form.playlists.map((pl) => ({
        title: pl.title,
        description: pl.description,
        keywords: splitList(pl.keywordsText),
        enabled: pl.enabled,
      })),
    };
  }

  async function saveChoice() {
    if (!dirty) return true;
    const ok = await run(api.patch(`/identity/${ident.id}`, choicePayload()));
    if (ok) dirty = false;
    return ok;
  }

  async function propose() {
    if (ident && ident.status !== 'failed') {
      const ok = await askConfirm({
        title: 'Nouvelle proposition ?',
        message: "L'agent repart de zéro avec ton brief. La proposition actuelle sera remplacée (rien n'est retiré de la chaîne).",
        confirmLabel: 'Proposer',
      });
      if (!ok) return;
    }
    busy = true;
    if (await run(api.post(`/bots/${bot.id}/identity`, { brief }), "L'agent identité est au travail")) {
      form = null;
      formFor = null;
      await load();
    }
    busy = false;
  }

  async function regenerate() {
    busy = true;
    if ((await saveChoice()) && (await run(api.post(`/identity/${ident.id}/images`), 'Nouveaux visuels en cours'))) await load();
    busy = false;
  }

  async function applyAll() {
    const ok = await askConfirm({
      title: bot.dry_run ? 'Simuler l’application ?' : `Appliquer sur « ${target?.display_name ?? 'la chaîne'} » ?`,
      message: bot.dry_run
        ? 'Bot en dry run : tout est simulé, rien n’est envoyé à YouTube.'
        : `La description, les mots-clés${form.banner ? ', la bannière' : ''} et ${form.playlists.filter((p) => p.enabled).length} playlist(s) seront envoyés sur YouTube. Le nom et la photo restent à reporter à la main.`,
      confirmLabel: bot.dry_run ? 'Simuler' : 'Appliquer',
    });
    if (!ok) return;
    busy = true;
    if ((await saveChoice()) && (await run(api.post(`/identity/${ident.id}/apply`)))) await load();
    busy = false;
  }

  async function copy(text, what) {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${what} copié`, 'success');
    } catch {
      toast('Copie impossible : sélectionne le texte à la main', 'error');
    }
  }

  function touch() {
    dirty = true;
  }

  function addPlaylist() {
    form.playlists.push({ title: '', description: '', keywordsText: '', enabled: true });
    touch();
  }

  const imgUrl = (kind) => `/api/identity/${ident.id}/image/${kind}?v=${imgStamp}-${ident.updated_at}`;
</script>

{#if !loaded}
  <div class="grid place-items-center py-16 text-muted"><span class="spinner"></span></div>
{:else}
  <div class="space-y-5">
    <!-- Brief et lancement -->
    <Section
      title="Identité de la chaîne"
      icon="palette"
      description="L'agent propose un nom, une description, des mots-clés, une palette, une photo de profil, une bannière et des playlists. Tu choisis, tu retouches, puis tu appliques."
    >
      <div class="flex flex-col gap-3 md:flex-row md:items-end">
        <div class="flex-1">
          <label class="label" for="brief">Brief (facultatif)</label>
          <textarea
            id="brief"
            class="input min-h-[72px] resize-y"
            placeholder="Ex. : best-of humour des streams FR de Zerator et Antoine Daniel, ton décalé, public 15-25 ans"
            bind:value={brief}
          ></textarea>
          <p class="hint">L'agent connaît déjà les chaînes surveillées et les titres des derniers clips de ce bot.</p>
        </div>
        <button class="btn btn-primary md:mb-6" disabled={busy || working} onclick={propose}>
          {#if ident?.status === 'generating'}<span class="spinner"></span>{:else}<Icon name="sparkle" />{/if}
          {ident ? 'Nouvelle proposition' : 'Proposer une identité'}
        </button>
      </div>
      {#if ident?.status === 'generating'}
        <p class="mt-2 flex items-center gap-2 text-sm text-muted"><span class="spinner"></span> L'agent réfléchit à ton identité de chaîne…</p>
      {:else if ident?.status === 'failed'}
        <p class="mt-2 rounded-lg bg-[var(--danger-soft)] px-3 py-2 text-sm text-danger"><Icon name="alert" size={14} class="mr-1 inline" />{ident.error}</p>
      {/if}
    </Section>

    {#if proposal && form}
      <!-- Nom -->
      <Section title="Nom de la chaîne" icon="pencil" description="À reporter dans YouTube Studio : l'API YouTube ne permet pas de renommer une chaîne.">
        <div class="grid gap-2 sm:grid-cols-3">
          {#each proposal.names as name}
            <button
              class="rounded-xl border px-4 py-3 text-left transition-colors {form.name === name
                ? 'border-accent bg-accent-soft'
                : 'border-line hover:bg-surface-2'}"
              onclick={() => { form.name = name; touch(); }}
            >
              <span class="block font-serif text-lg leading-tight">{name}</span>
            </button>
          {/each}
        </div>
        <div class="mt-4 flex flex-wrap items-end gap-2">
          <div class="min-w-56 flex-1">
            <label class="label" for="name">Nom retenu</label>
            <input id="name" class="input" maxlength="50" bind:value={form.name} oninput={touch} />
          </div>
          <button class="btn" onclick={() => copy(form.name, 'Nom')}><Icon name="copy" /> Copier</button>
          <a class="btn" href={STUDIO} target="_blank" rel="noreferrer"><Icon name="external" /> YouTube Studio</a>
        </div>
        <p class="hint mt-2">« {proposal.tagline} »</p>
      </Section>

      <!-- Visuels -->
      <Section title="Photo de profil et bannière" icon="image">
        {#snippet actions()}
          <button class="btn btn-sm" disabled={busy || ident.images_status === 'generating'} onclick={regenerate}>
            {#if ident.images_status === 'generating'}<span class="spinner"></span>{:else}<Icon name="refresh" size={14} />{/if}
            Régénérer
          </button>
        {/snippet}
        {#if ident.images_status === 'ready'}
          <div class="flex flex-col gap-6 md:flex-row md:items-start">
            <div class="flex shrink-0 flex-col items-center gap-3">
              <img src={imgUrl('avatar')} alt="Logo proposé pour la chaîne" class="size-36 rounded-full border border-line object-cover shadow-sm" />
              <a class="btn btn-sm" href={imgUrl('avatar')} download="photo-de-profil.png"><Icon name="download" size={14} /> Télécharger</a>
              <p class="hint max-w-40 text-center">À importer dans YouTube Studio (pas d'API pour la photo).</p>
            </div>
            <div class="min-w-0 flex-1">
              <div class="relative overflow-hidden rounded-xl border border-line">
                <img src={imgUrl('banner')} alt="Bannière proposée" class="block aspect-video w-full object-cover" />
                {#if showSafe}
                  <!-- Zone visible partout : 1546 x 423 px au centre d'une image 2560 x 1440. -->
                  <div class="pointer-events-none absolute inset-0 grid place-items-center">
                    <div class="h-[29.4%] w-[60.4%] rounded border-2 border-dashed border-white/80 shadow-[0_0_0_9999px_rgba(0,0,0,0.35)]"></div>
                  </div>
                {/if}
              </div>
              <div class="mt-3 flex flex-wrap items-center gap-3">
                <label class="flex items-center gap-2 text-sm text-muted">
                  <input type="checkbox" bind:checked={showSafe} class="accent-[var(--accent)]" /> Zone visible sur mobile
                </label>
                <label class="flex items-center gap-2 text-sm">
                  <input type="checkbox" bind:checked={form.banner} onchange={touch} class="accent-[var(--accent)]" /> Envoyer la bannière sur YouTube
                </label>
                <a class="btn btn-sm ml-auto" href={imgUrl('banner')} download="banniere.jpg"><Icon name="download" size={14} /> Télécharger</a>
              </div>
            </div>
          </div>
        {:else if ident.images_status === 'failed'}
          <p class="rounded-lg bg-[var(--danger-soft)] px-3 py-2 text-sm text-danger">{ident.error}</p>
        {:else}
          <p class="flex items-center gap-2 text-sm text-muted"><span class="spinner"></span> Création des visuels pour « {form.name} »…</p>
        {/if}
        {#if dirty && ident.images_status === 'ready'}
          <p class="hint mt-3">Nom modifié ? Clique sur Régénérer pour l'intégrer à la bannière.</p>
        {/if}
      </Section>

      <!-- Textes -->
      <Section title="Description et mots-clés" icon="list">
        <div class="space-y-4">
          <div>
            <div class="flex items-baseline justify-between">
              <label class="label" for="desc">Description</label>
              <span class="text-xs {form.description.length > 1000 ? 'text-danger' : 'text-faint'}">{form.description.length} / 1000</span>
            </div>
            <textarea id="desc" class="input min-h-[140px] resize-y" bind:value={form.description} oninput={touch}></textarea>
          </div>
          <div>
            <label class="label" for="kw">Mots-clés de la chaîne</label>
            <input id="kw" class="input" bind:value={form.keywordsText} oninput={touch} />
            <p class="hint">Séparés par des virgules.</p>
          </div>
          <div class="flex items-center gap-2">
            <span class="label mb-0">Palette</span>
            {#each proposal.palette as color}
              <button class="flex items-center gap-1.5 rounded-lg border border-line px-2 py-1 text-xs" title="Copier" onclick={() => copy(color, 'Couleur')}>
                <span class="size-4 rounded" style="background:{color}"></span>{color}
              </button>
            {/each}
          </div>
        </div>
      </Section>

      <!-- Playlists -->
      <Section title="Playlists" icon="film" description="Chaque Short publié rejoint les playlists dont un mot-clé apparaît dans son titre ou le nom de la chaîne d'origine. Sans mot-clé : tous les Shorts.">
        <div class="space-y-3">
          {#each form.playlists as pl, i}
            <div class="rounded-xl border border-line p-3 {pl.enabled ? '' : 'opacity-60'}">
              <div class="flex items-center gap-3">
                <input type="checkbox" bind:checked={pl.enabled} onchange={touch} class="accent-[var(--accent)]" aria-label="Créer cette playlist" />
                <input class="input flex-1 font-medium" placeholder="Titre" bind:value={pl.title} oninput={touch} />
                <button class="icon-btn danger" aria-label="Retirer" onclick={() => { form.playlists.splice(i, 1); touch(); }}><Icon name="trash" size={14} /></button>
              </div>
              <div class="mt-2 grid gap-2 pl-7 md:grid-cols-2">
                <input class="input" placeholder="Description" bind:value={pl.description} oninput={touch} />
                <input class="input" placeholder="Mots-clés (vide = tous les Shorts)" bind:value={pl.keywordsText} oninput={touch} />
              </div>
            </div>
          {/each}
          <button class="btn btn-ghost" onclick={addPlaylist}><Icon name="plus" /> Ajouter une playlist</button>
        </div>
        {#if playlists.length}
          <div class="mt-5 border-t border-line pt-4">
            <p class="label">Déjà sur la chaîne</p>
            <ul class="flex flex-wrap gap-2">
              {#each playlists as p}
                <li class="badge">{p.title} · {p.items} Short{p.items > 1 ? 's' : ''}</li>
              {/each}
            </ul>
          </div>
        {/if}
      </Section>

      <!-- Application -->
      <section class="card flex flex-col gap-4 p-6 md:flex-row md:items-center">
        <div class="min-w-0 flex-1">
          <h2 class="flex items-center gap-2 text-[15px] font-semibold"><Icon name="youtube" size={16} class="text-accent" /> Appliquer sur YouTube</h2>
          {#if !youtubeAccounts.length}
            <p class="mt-1 text-sm text-muted">Lie d'abord une chaîne YouTube à ce bot (onglet Configuration).</p>
          {:else}
            <div class="mt-3 flex flex-wrap items-center gap-3">
              {#if youtubeAccounts.length > 1}
                <select class="input w-auto" value={targetId} onchange={(e) => { form.account_id = Number(e.target.value); touch(); }}>
                  {#each youtubeAccounts as a}<option value={a.id}>{a.display_name}</option>{/each}
                </select>
              {:else}
                <span class="text-sm">Chaîne : <strong>{youtubeAccounts[0].display_name}</strong></span>
              {/if}
              {#if bot.dry_run}<span class="badge">Dry run : simulé</span>{/if}
              {#if ident.status === 'applied'}<span class="badge badge-success"><Icon name="check" size={11} /> Appliquée</span>{/if}
              {#if dirty}<span class="badge badge-warning">Modifications non envoyées</span>{/if}
            </div>
            {#if !bot.dry_run && target?.connected && !target.can_manage}
              <p class="mt-2 text-sm text-warning">
                Cette chaîne a été connectée avant l'agent identité : reconnecte-la dans
                <button class="font-medium underline" onclick={() => (app.view = 'settings')}>Paramètres</button> pour autoriser la gestion de la chaîne.
              </p>
            {/if}
            {#if ident.error && ident.status === 'ready'}
              <p class="mt-2 text-sm text-danger">{ident.error}</p>
            {/if}
          {/if}
        </div>
        <button
          class="btn btn-primary"
          disabled={busy || working || !youtubeAccounts.length || form.description.length > 1000 || ident.images_status === 'generating'}
          onclick={applyAll}
        >
          {#if ident.status === 'applying'}<span class="spinner"></span>{:else}<Icon name="send" />{/if}
          {ident.status === 'applied' ? 'Réappliquer' : 'Appliquer'}
        </button>
      </section>
    {/if}
  </div>
{/if}
