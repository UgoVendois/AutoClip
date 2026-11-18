<script>
  import { onDestroy, untrack } from 'svelte';
  import { api } from '../api.js';
  import { fmtDate } from '../format.js';
  import { askConfirm, run, toast } from '../state.svelte.js';
  import Icon from './Icon.svelte';
  import ModelPicker from './ModelPicker.svelte';
  import Section from './Section.svelte';

  let { bot, tick, reload } = $props();

  const LANGUAGES = [
    { value: 'fr', label: 'Français' },
    { value: 'en', label: 'Anglais' },
    { value: 'es', label: 'Espagnol' },
    { value: 'de', label: 'Allemand' },
    { value: 'it', label: 'Italien' },
    { value: 'pt', label: 'Portugais' },
    { value: '', label: 'Toutes les langues' },
  ];
  const FREQUENCIES = [
    { value: 6, label: 'toutes les 6 h' },
    { value: 12, label: 'toutes les 12 h' },
    { value: 24, label: 'une fois par jour' },
    { value: 72, label: 'tous les 3 jours' },
    { value: 168, label: 'une fois par semaine' },
  ];
  const PROVIDERS = [
    { value: 'none', label: 'Aucune (signaux seuls, gratuit)' },
    { value: 'anthropic', label: 'Claude (Anthropic)' },
    { value: 'gemini', label: 'Gemini (Google)' },
    { value: 'openai', label: 'ChatGPT (OpenAI)' },
    { value: 'mistral', label: 'Mistral AI' },
    { value: 'ollama', label: 'Ollama (local)' },
  ];
  const DISCOVERED = {
    new: { label: 'Nouveau', cls: 'badge' },
    presorted: { label: 'Téléchargement des moments forts', cls: 'badge badge-warning' },
    scoring: { label: 'Notation IA', cls: 'badge badge-warning' },
    deferred: { label: 'Reporté (budget IA)', cls: 'badge badge-warning' },
    done: { label: 'Terminé', cls: 'badge badge-success' },
    skipped: { label: 'Aucun moment fort', cls: 'badge' },
    failed: { label: 'Échec', cls: 'badge badge-danger' },
  };
  const SIGNALS = { chat: 'pics du chat', comments: 'commentaires horodatés', audio: 'énergie du son' };

  // Formulaires copiés une fois : le rafraîchissement périodique n'écrase pas la saisie.
  const a = untrack(() => bot.ai_settings);
  let scoutForm = $state({
    scout_enabled: a.scout_enabled,
    scout_theme: a.scout_theme,
    scout_platforms: [...a.scout_platforms],
    scout_language: a.scout_language,
    scout_max_channels: a.scout_max_channels,
    scout_every_hours: a.scout_every_hours,
  });
  let aiForm = $state({
    provider: a.provider === 'fake' ? 'none' : a.provider,
    model: a.model,
    effort: a.effort,
    daily_token_budget: a.daily_token_budget,
    min_score: a.min_score,
    clips_per_video: a.clips_per_video,
    max_windows: a.max_windows,
    vision: a.vision,
    chat_delay_seconds: a.chat_delay_seconds,
  });

  let scoutRuns = $state([]);
  let scoutPending = $state(false);
  let discovered = $state([]);
  let usage = $state(null);
  let creds = $state(null);
  let twitch = $state(null);
  let twitchCode = $state(null);
  let defaults = $state({ text: {} });
  let newWatch = $state({ platform: 'twitch', channel: '' });
  let saving = $state(false);
  let twitchTimer = null;

  const watches = $derived(bot.watches);
  const aiCount = $derived(watches.filter((w) => w.origin === 'ai').length);
  const lastRun = $derived(scoutRuns[0]);

  async function load() {
    try {
      const [sc, disc, u, tw] = await Promise.all([
        api.get(`/bots/${bot.id}/scout`),
        api.get(`/bots/${bot.id}/discovered`),
        api.get(`/bots/${bot.id}/ai-usage`),
        api.get('/twitch/status'),
      ]);
      const wasPending = scoutPending;
      scoutRuns = sc.runs;
      scoutPending = sc.pending;
      discovered = disc.slice(0, 20);
      usage = u;
      twitch = tw;
      if (wasPending && !sc.pending && sc.runs[0]) {
        const n = sc.runs[0].added.length;
        toast(sc.runs[0].status === 'failed' ? sc.runs[0].error : n ? `${n} nouvelle${n > 1 ? 's' : ''} chaîne${n > 1 ? 's' : ''} trouvée${n > 1 ? 's' : ''}` : 'Aucune nouvelle chaîne',
          sc.runs[0].status === 'failed' ? 'error' : 'success');
      }
    } catch {
      /* prochain rafraîchissement */
    }
  }

  $effect(() => {
    tick;
    untrack(load);
  });

  (async () => {
    creds = await api.get('/credentials').catch(() => null);
    defaults = await api.get('/ai/providers').catch(() => ({ text: {} }));
  })();

  onDestroy(() => clearInterval(twitchTimer));

  function togglePlatform(p) {
    scoutForm.scout_platforms = scoutForm.scout_platforms.includes(p)
      ? scoutForm.scout_platforms.filter((x) => x !== p)
      : [...scoutForm.scout_platforms, p];
  }

  async function saveScout(andRun = false) {
    if (!scoutForm.scout_platforms.length) return toast('Choisis au moins une plateforme', 'error');
    saving = true;
    const body = { ...scoutForm, scout_max_channels: Number(scoutForm.scout_max_channels), scout_every_hours: Number(scoutForm.scout_every_hours) };
    let ok = await run(api.patch(`/bots/${bot.id}`, { ai_settings: body }), andRun ? null : 'Éclaireur enregistré');
    if (ok && andRun) {
      ok = await run(api.post(`/bots/${bot.id}/scout`), "L'IA cherche des chaînes…");
      if (ok) scoutPending = true;
    }
    saving = false;
    if (ok) reload();
  }

  async function saveAI() {
    const body = { ...aiForm };
    for (const k of ['daily_token_budget', 'min_score', 'clips_per_video', 'max_windows', 'chat_delay_seconds']) body[k] = Number(body[k]);
    if (await run(api.patch(`/bots/${bot.id}`, { ai_settings: body }), 'Réglages IA enregistrés')) reload();
  }

  async function addWatch() {
    if (!newWatch.channel.trim()) return;
    if (await run(api.post(`/bots/${bot.id}/watches`, newWatch), 'Chaîne ajoutée')) {
      newWatch.channel = '';
      reload();
    }
  }

  async function patchWatch(w, changes) {
    if (await run(api.patch(`/watches/${w.id}`, changes))) reload();
  }

  async function removeWatch(w) {
    const ok = await askConfirm({
      title: `Ne plus surveiller ${w.channel} ?`,
      message: w.origin === 'ai'
        ? "L'éclaireur ne reproposera plus cette chaîne. Les clips déjà créés sont conservés."
        : 'Les clips déjà créés sont conservés.',
      confirmLabel: 'Retirer',
      danger: true,
    });
    if (ok && (await run(api.del(`/watches/${w.id}`), 'Chaîne retirée'))) reload();
  }

  async function discoverNow() {
    const res = await run(api.post(`/bots/${bot.id}/discover`));
    if (res) toast(`${res.watches} chaîne${res.watches > 1 ? 's' : ''} en cours de vérification`, 'success');
  }

  async function connectTwitch() {
    const res = await run(api.post('/twitch/login'));
    if (!res) return;
    twitchCode = res;
    clearInterval(twitchTimer);
    twitchTimer = setInterval(async () => {
      const st = await api.get('/twitch/status').catch(() => null);
      if (!st) return;
      twitch = st;
      const state = st.login_flow?.state;
      if (state === 'connected' || state === 'error') {
        clearInterval(twitchTimer);
        twitchCode = null;
        toast(state === 'connected' ? `Twitch connecté : ${st.login}` : st.login_flow.error, state === 'connected' ? 'success' : 'error');
      }
    }, 2000);
  }

  function channelUrl(w) {
    if (w.platform === 'twitch') return `https://www.twitch.tv/${w.channel}`;
    const id = w.channel_id || w.channel;
    return id.startsWith('UC') ? `https://www.youtube.com/channel/${id}` : `https://www.youtube.com/${id.startsWith('@') ? id : '@' + id}`;
  }

  const pct = $derived(usage && usage.budget ? Math.min(100, Math.round((usage.used / usage.budget) * 100)) : 0);
</script>

<div class="space-y-5">
  <!-- Éclaireur IA -->
  <Section title="Éclaireur IA" icon="radar" description="Décris ce que tu veux clipper : l'IA trouve elle-même les chaînes Twitch et YouTube à surveiller, puis recommence régulièrement.">
    {#snippet actions()}
      <button class="switch" role="switch" aria-checked={scoutForm.scout_enabled} aria-label="Recherche automatique"
        onclick={() => (scoutForm.scout_enabled = !scoutForm.scout_enabled)}></button>
    {/snippet}
    <div class="space-y-4">
      <div>
        <label class="label" for="theme">Thème</label>
        <textarea id="theme" class="input min-h-[64px] resize-y" placeholder="Ex. : GTA 6, streamers français, moments drôles et courses-poursuites"
          bind:value={scoutForm.scout_theme}></textarea>
      </div>
      <div class="grid gap-4 md:grid-cols-4">
        <div class="md:col-span-1">
          <p class="label">Plateformes</p>
          <div class="flex gap-2">
            {#each ['twitch', 'youtube'] as p}
              <button class="rounded-lg border px-3 py-1.5 text-sm capitalize {scoutForm.scout_platforms.includes(p) ? 'border-accent bg-accent-soft font-medium' : 'border-line text-muted'}"
                onclick={() => togglePlatform(p)}>{p === 'twitch' ? 'Twitch' : 'YouTube'}</button>
            {/each}
          </div>
        </div>
        <div>
          <label class="label" for="lang">Langue</label>
          <select id="lang" class="input" bind:value={scoutForm.scout_language}>
            {#each LANGUAGES as l}<option value={l.value}>{l.label}</option>{/each}
          </select>
        </div>
        <div>
          <label class="label" for="maxch">Chaînes surveillées par l'IA</label>
          <input id="maxch" class="input" type="number" min="1" max="30" bind:value={scoutForm.scout_max_channels} />
        </div>
        <div>
          <label class="label" for="freq">Nouvelle recherche</label>
          <select id="freq" class="input" bind:value={scoutForm.scout_every_hours}>
            {#each FREQUENCIES as f}<option value={f.value}>{f.label}</option>{/each}
          </select>
        </div>
      </div>
      {#if creds}
        {#if scoutForm.scout_platforms.includes('twitch') && !creds.configured.twitch}
          <p class="text-sm text-warning"><Icon name="alert" size={14} class="mr-1 inline" />Twitch : enregistre d'abord les clés Twitch dans Paramètres › Clés d'API.</p>
        {/if}
        {#if scoutForm.scout_platforms.includes('youtube') && !creds.configured.youtube_data}
          <p class="text-sm text-warning"><Icon name="alert" size={14} class="mr-1 inline" />YouTube : la recherche demande la clé « YouTube (lecture des commentaires) » dans Paramètres › Clés d'API.</p>
        {/if}
      {/if}
      <div class="flex flex-wrap items-center justify-end gap-2">
        <span class="mr-auto text-xs text-faint">{aiCount} / {scoutForm.scout_max_channels} chaînes trouvées par l'IA · retire celles qui ne te plaisent pas, l'IA les remplacera</span>
        <button class="btn" disabled={saving} onclick={() => saveScout(false)}><Icon name="check" /> Enregistrer</button>
        <button class="btn btn-primary" disabled={saving || scoutPending || !scoutForm.scout_theme.trim()} onclick={() => saveScout(true)}>
          {#if scoutPending}<span class="spinner"></span>{:else}<Icon name="sparkle" />{/if}
          Chercher maintenant
        </button>
      </div>
      {#if lastRun}
        <div class="rounded-xl border border-line bg-surface-2 px-4 py-3 text-sm">
          <p class="text-muted">
            Dernière recherche : {fmtDate(lastRun.created_at)} · {lastRun.candidates} chaîne{lastRun.candidates > 1 ? 's' : ''} examinée{lastRun.candidates > 1 ? 's' : ''}
            {#if lastRun.plan?.twitch_categories?.length}· catégories : {lastRun.plan.twitch_categories.join(', ')}{/if}
          </p>
          {#if lastRun.error}<p class="mt-1 {lastRun.status === 'failed' ? 'text-danger' : 'text-warning'}">{lastRun.error}</p>{/if}
          {#each lastRun.added as added}
            <p class="mt-1.5"><span class="badge badge-accent mr-1.5">+ {added.platform === 'twitch' ? 'Twitch' : 'YouTube'}</span><strong>{added.name}</strong> <span class="text-muted">: {added.reason}</span></p>
          {/each}
        </div>
      {/if}
    </div>
  </Section>

  <!-- Chaînes surveillées -->
  <Section title="Chaînes surveillées" icon="list" description="Leurs nouvelles VODs et vidéos sont analysées : seuls les moments forts sont téléchargés et deviennent des clips.">
    {#snippet actions()}
      <button class="btn btn-sm" disabled={!watches.length} onclick={discoverNow}><Icon name="refresh" size={14} /> Vérifier maintenant</button>
    {/snippet}
    {#if watches.length}
      <ul class="mb-4 divide-y divide-line rounded-xl border border-line">
        {#each watches as w (w.id)}
          <li class="flex flex-wrap items-center gap-3 px-4 py-3 {w.enabled ? '' : 'opacity-60'}">
            <span class="w-16 text-xs font-medium uppercase text-faint">{w.platform}</span>
            <div class="min-w-0 flex-1">
              <a class="font-medium hover:underline" href={channelUrl(w)} target="_blank" rel="noreferrer">{w.channel}</a>
              {#if w.origin === 'ai'}
                <span class="badge badge-accent ml-1.5" title="Trouvée par l'éclaireur IA"><Icon name="sparkle" size={11} /> IA{w.score != null ? ` · ${w.score}` : ''}</span>
              {/if}
              {#if w.reason}<p class="truncate text-xs text-muted" title={w.reason}>{w.reason}</p>{/if}
              <p class="text-xs text-faint">{w.last_checked_at ? `vérifiée ${fmtDate(w.last_checked_at)}` : 'pas encore vérifiée'}</p>
            </div>
            <select class="input w-auto py-1 text-sm" value={w.interval_minutes} onchange={(e) => patchWatch(w, { interval_minutes: Number(e.target.value) })} aria-label="Fréquence">
              {#each [15, 30, 60, 180, 720] as m}<option value={m}>{m < 60 ? `${m} min` : `${m / 60} h`}</option>{/each}
            </select>
            <button class="switch" role="switch" aria-checked={Boolean(w.enabled)} aria-label="Surveillance active" onclick={() => patchWatch(w, { enabled: !w.enabled })}></button>
            <button class="icon-btn danger" aria-label="Retirer {w.channel}" onclick={() => removeWatch(w)}><Icon name="trash" size={14} /></button>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="mb-4 text-sm text-muted">Aucune chaîne pour l'instant. Lance l'éclaireur IA ci-dessus, ou ajoute une chaîne à la main.</p>
    {/if}
    <form class="flex flex-wrap gap-2" onsubmit={(e) => { e.preventDefault(); addWatch(); }}>
      <select class="input w-auto" bind:value={newWatch.platform} aria-label="Plateforme">
        <option value="twitch">Twitch</option>
        <option value="youtube">YouTube</option>
      </select>
      <input class="input min-w-56 flex-1" placeholder={newWatch.platform === 'twitch' ? 'pseudo ou lien twitch.tv/…' : '@chaine ou lien youtube.com/…'} bind:value={newWatch.channel} />
      <button class="btn" disabled={!newWatch.channel.trim()}><Icon name="plus" /> Ajouter à la main</button>
    </form>
  </Section>

  <!-- Chat Twitch -->
  <Section title="Chat Twitch en direct" icon="radar" description="Les pics du chat repèrent les meilleurs moments. Twitch ne donne pas le chat des VODs passées : AutoClip l'enregistre pendant les lives, tant que l'app est ouverte.">
    {#if twitch?.connected}
      <p class="flex items-center gap-2 text-sm"><span class="badge badge-success"><Icon name="check" size={11} /> Connecté</span> compte <strong>{twitch.login}</strong> (lecture du chat uniquement)</p>
    {:else if twitchCode}
      <div class="rounded-xl border border-line bg-surface-2 px-4 py-3 text-sm">
        <p>Ouvre <a class="font-medium text-accent hover:underline" href={twitchCode.verification_uri} target="_blank" rel="noreferrer">{twitchCode.verification_uri}</a> et saisis ce code :</p>
        <p class="my-2 font-mono text-2xl tracking-widest">{twitchCode.user_code}</p>
        <p class="flex items-center gap-2 text-muted"><span class="spinner"></span> En attente de ta validation sur Twitch…</p>
      </div>
    {:else}
      <div class="flex flex-wrap items-center gap-3">
        <button class="btn btn-primary" disabled={creds && !creds.configured.twitch} onclick={connectTwitch}><Icon name="link" /> Connecter un compte Twitch</button>
        <p class="text-sm text-muted">N'importe quel compte Twitch convient. Sans connexion, AutoClip se rabat sur l'énergie du son.</p>
      </div>
    {/if}
  </Section>

  <!-- IA de notation -->
  <Section title="IA de notation" icon="sparkle" description="Note les moments forts présélectionnés (paroles et images), écrit titres, descriptions et accroches. Elle sert aussi à l'éclaireur.">
    <div class="grid gap-4 md:grid-cols-3">
      <div>
        <label class="label" for="prov">Fournisseur</label>
        <select id="prov" class="input" bind:value={aiForm.provider} onchange={() => (aiForm.model = null)}>
          {#each PROVIDERS as p}<option value={p.value}>{p.label}</option>{/each}
        </select>
      </div>
      <div>
        <label class="label" for="aimodel">Modèle</label>
        {#if aiForm.provider === 'none'}
          <input id="aimodel" class="input" disabled value="—" />
        {:else}
          <ModelPicker id="aimodel" provider={aiForm.provider} value={aiForm.model} defaultModel={defaults.text[aiForm.provider]} onchange={(v) => (aiForm.model = v)} />
        {/if}
      </div>
      <div>
        <label class="label" for="budget">Budget par jour (tokens)</label>
        <input id="budget" class="input" type="number" min="0" step="10000" bind:value={aiForm.daily_token_budget} />
        {#if usage}
          <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-2"><div class="h-full rounded-full {pct >= 100 ? 'bg-danger' : 'bg-accent'}" style="width:{pct}%"></div></div>
          <p class="hint">Aujourd'hui : {usage.used.toLocaleString('fr-FR')} tokens ({pct} %)</p>
        {/if}
      </div>
      <div>
        <label class="label" for="minscore">Score minimal d'un clip (0-100)</label>
        <input id="minscore" class="input" type="number" min="0" max="100" bind:value={aiForm.min_score} />
      </div>
      <div>
        <label class="label" for="cpv">Clips par vidéo</label>
        <input id="cpv" class="input" type="number" min="1" max="10" bind:value={aiForm.clips_per_video} />
      </div>
      <div>
        <label class="label" for="mw">Moments analysés par vidéo</label>
        <input id="mw" class="input" type="number" min="1" max="30" bind:value={aiForm.max_windows} />
      </div>
      <div>
        <label class="label" for="delay">Retard du chat sur l'action (s)</label>
        <input id="delay" class="input" type="number" min="0" max="30" bind:value={aiForm.chat_delay_seconds} />
      </div>
      {#if aiForm.provider === 'anthropic'}
        <div>
          <label class="label" for="effort">Effort de réflexion</label>
          <select id="effort" class="input" bind:value={aiForm.effort}>
            <option value="low">Faible (économique)</option>
            <option value="medium">Moyen</option>
            <option value="high">Élevé</option>
          </select>
        </div>
      {/if}
      <div class="flex items-start justify-between gap-4 md:col-span-1">
        <div>
          <p class="label mb-0">L'IA voit l'image</p>
          <p class="hint mt-1">2 images par moment, pour juger l'action à l'écran.</p>
        </div>
        <button class="switch mt-1" role="switch" aria-checked={aiForm.vision} aria-label="Images pour l'IA" onclick={() => (aiForm.vision = !aiForm.vision)}></button>
      </div>
    </div>
    {#if bot.dry_run && aiForm.provider !== 'none'}
      <p class="hint mt-3">Bot en dry run : la notation des clips est simulée (aucun appel payant). L'éclaireur, lui, utilise la vraie IA (deux appels par recherche).</p>
    {/if}
    <div class="mt-4 flex justify-end">
      <button class="btn btn-primary" onclick={saveAI}><Icon name="check" /> Enregistrer</button>
    </div>
  </Section>

  <!-- Contenus trouvés -->
  <Section title="Contenus trouvés" icon="film" description="Les VODs et vidéos repérées sur les chaînes surveillées, et où elles en sont.">
    {#if discovered.length}
      <ul class="divide-y divide-line rounded-xl border border-line">
        {#each discovered as d (d.id)}
          {@const st = DISCOVERED[d.status] ?? { label: d.status, cls: 'badge' }}
          <li class="flex items-center gap-3 px-4 py-2.5 text-sm">
            <span class="w-16 text-xs font-medium uppercase text-faint">{d.platform}</span>
            <div class="min-w-0 flex-1">
              <a class="block truncate hover:underline" href={d.url} target="_blank" rel="noreferrer" title={d.title}>{d.title || d.url}</a>
              <p class="text-xs text-faint">{fmtDate(d.created_at)}{d.signal ? ` · ${SIGNALS[d.signal] ?? d.signal}` : ''}{d.error ? ` · ${d.error}` : ''}</p>
            </div>
            <span class={st.cls}>{st.label}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="text-sm text-muted">Rien pour l'instant. Les nouvelles VODs apparaissent ici dès qu'une chaîne surveillée en publie.</p>
    {/if}
  </Section>
</div>
