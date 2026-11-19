<script>
  import { onMount, untrack } from 'svelte';
  import { api } from '../api.js';
  import { SOURCE_STATUS } from '../format.js';
  import { app, askConfirm, refreshBots, run } from '../state.svelte.js';
  import Badge from './Badge.svelte';
  import EtaCancel from './EtaCancel.svelte';
  import Icon from './Icon.svelte';
  import AccountLink from './AccountLink.svelte';
  import Section from './Section.svelte';

  let { bot, reload } = $props();

  const DAYS = [
    { key: 'mon', label: 'L', name: 'lundi' },
    { key: 'tue', label: 'M', name: 'mardi' },
    { key: 'wed', label: 'M', name: 'mercredi' },
    { key: 'thu', label: 'J', name: 'jeudi' },
    { key: 'fri', label: 'V', name: 'vendredi' },
    { key: 'sat', label: 'S', name: 'samedi' },
    { key: 'sun', label: 'D', name: 'dimanche' },
  ];
  const TIMEZONES = ['Europe/Paris', 'Europe/Brussels', 'Europe/Zurich', 'Europe/London', 'America/Montreal',
    'America/New_York', 'America/Los_Angeles', 'UTC'];

  // Formulaires initialisés une fois par bot : le rafraîchissement périodique ne
  // doit pas écraser une saisie en cours.
  const initial = untrack(() => bot);
  const s = initial.clip_settings;
  let clip = $state({
    min_seconds: s.min_seconds,
    max_seconds: s.max_seconds,
    max_clips: s.max_clips,
    subtitle_words: s.subtitle_words,
    font_size: s.font_size,
    tags: (s.tags ?? []).join(', '),
    layout: s.layout ?? 'auto',
    trim_silences: s.trim_silences ?? true,
    hook_text: s.hook_text ?? true,
    hook_seconds: s.hook_seconds ?? 3,
  });

  const LAYOUTS = [
    { value: 'auto', label: 'Automatique', hint: 'Facecam en haut si une webcam est détectée, sinon jeu entier sur fond flouté.' },
    { value: 'facecam', label: 'Facecam + jeu', hint: 'Webcam du streamer en haut, jeu en dessous (repli sur fond flouté sans webcam).' },
    { value: 'fit', label: 'Fond flouté', hint: "L'image entière au centre, rien n'est rogné : idéal pour le gameplay." },
    { value: 'crop', label: 'Recadrage central', hint: 'Pour une personne filmée en plein cadre (podcast, IRL).' },
  ];
  const sc = initial.schedule;
  let schedule = $state({
    every_hours: sc?.every_hours ?? 4,
    days: DAYS.map((d, i) => (sc ? Boolean((sc.weekdays >> i) & 1) : true)),
    start: sc?.window_start ?? '09:00',
    end: sc?.window_end ?? '21:00',
    timezone: sc?.timezone ?? 'Europe/Paris',
    enabled: sc ? Boolean(sc.enabled) : true,
  });

  let newSource = $state('');
  let adding = $state(false);
  let accounts = $state([]);
  let toLink = $state('');

  const linkedIds = $derived(new Set(bot.accounts.map((a) => a.id)));
  const available = $derived(accounts.filter((a) => !linkedIds.has(a.id)));

  onMount(async () => {
    accounts = (await run(api.get('/accounts'))) || [];
  });

  async function addSource(uri = newSource) {
    uri = uri.trim();
    if (!uri) return;
    adding = true;
    const ok = await run(api.post(`/bots/${bot.id}/sources`, { uri }), 'Vidéo ajoutée : traitement lancé');
    adding = false;
    if (ok) {
      newSource = '';
      reload();
    }
  }

  async function cancelSource(src) {
    const ok = await askConfirm({
      title: 'Annuler cette analyse ?',
      message: `« ${src.uri} » ne sera plus analysée. Les clips déjà créés sont conservés.`,
      confirmLabel: "Arrêter l'analyse",
      danger: true,
    });
    if (ok && (await run(api.post(`/sources/${src.id}/cancel`), 'Analyse annulée'))) reload();
  }

  async function browse() {
    const path = await window.pywebview?.api.pick_video();
    if (path) addSource(path);
  }

  async function toggleLive() {
    if (bot.dry_run) {
      const ok = await askConfirm({
        title: 'Activer la publication réelle ?',
        message: bot.accounts.length
          ? `Les clips que tu valides seront publiés sur ${bot.accounts.map((a) => a.display_name).join(', ')} selon le planning.`
          : "Ce bot n'a encore aucun compte lié : rien ne pourra être publié tant que tu n'en lies pas un.",
        confirmLabel: 'Activer',
      });
      if (!ok) return;
    }
    if (await run(api.patch(`/bots/${bot.id}`, { dry_run: !bot.dry_run }),
                  bot.dry_run ? 'Publication réelle activée' : 'Retour en dry run')) {
      reload();
      refreshBots();
    }
  }

  async function link() {
    if (!toLink) return;
    if (await run(api.post(`/bots/${bot.id}/accounts/${toLink}`), 'Compte lié')) {
      toLink = '';
      reload();
    }
  }

  async function unlink(account) {
    if (await run(api.del(`/bots/${bot.id}/accounts/${account.id}`))) reload();
  }

  async function saveClip() {
    const body = {
      ...clip,
      tags: clip.tags.split(',').map((t) => t.trim()).filter(Boolean),
    };
    for (const k of ['min_seconds', 'max_seconds', 'max_clips', 'subtitle_words', 'font_size', 'hook_seconds']) {
      body[k] = Number(body[k]);
    }
    if (await run(api.patch(`/bots/${bot.id}`, { clip_settings: body }), 'Paramètres de découpage enregistrés')) reload();
  }

  async function saveSchedule() {
    const days = DAYS.filter((_, i) => schedule.days[i]).map((d) => d.key);
    if (!days.length) {
      run(Promise.reject(new Error('Choisis au moins un jour.')));
      return;
    }
    const body = {
      every_hours: Number(schedule.every_hours),
      days: days.join(','),
      window: `${schedule.start}-${schedule.end}`,
      timezone: schedule.timezone,
      enabled: schedule.enabled,
    };
    if (await run(api.put(`/bots/${bot.id}/schedule`, body), 'Planning enregistré')) reload();
  }
</script>

<div class="space-y-5">
  <Section title="Sources" icon="film" description="Une vidéo précise à découper : lien YouTube (ou tout site pris en charge par yt-dlp), ou fichier de ton ordinateur. Pour que l'IA trouve elle-même les chaînes à clipper, va dans l'onglet Découverte.">
    <form class="flex gap-2" onsubmit={(e) => { e.preventDefault(); addSource(); }}>
      <input class="input" placeholder="https://www.youtube.com/watch?v=… ou /chemin/vers/video.mp4" bind:value={newSource} />
      {#if app.inWindow}
        <button type="button" class="btn" onclick={browse}><Icon name="folder" /> Parcourir…</button>
      {/if}
      <button class="btn btn-primary" disabled={adding || !newSource.trim()}>
        {#if adding}<span class="spinner"></span>{:else}<Icon name="plus" />{/if} Ajouter
      </button>
    </form>

    {#if bot.sources.length}
      <ul class="mt-4 divide-y divide-line rounded-xl border border-line">
        {#each bot.sources as src (src.id)}
          <li class="flex items-center gap-3 px-4 py-3">
            <Icon name={src.kind === 'url' ? 'globe' : 'folder'} size={16} class="shrink-0 text-faint" />
            <div class="min-w-0 flex-1">
              <p class="truncate" title={src.uri}>{src.uri}</p>
              {#if src.error && src.error !== 'annulé'}<p class="mt-0.5 text-xs text-danger">{src.error}</p>{/if}
            </div>
            <Badge status={src.error === 'annulé' ? 'cancelled' : src.status} map={SOURCE_STATUS} />
            {#if ['ingest', 'transcribe', 'analyze'].includes(src.status)}
              <EtaCancel eta={src.eta_seconds} label="Annuler l'analyse" oncancel={() => cancelSource(src)} />
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </Section>

  <Section title="Mode de publication" icon="send">
    <div class="flex items-center justify-between gap-6">
      <div>
        <p class="font-medium">{bot.dry_run ? 'Dry run' : 'Publication réelle'}</p>
        <p class="mt-0.5 text-sm text-muted">
          {bot.dry_run
            ? "Tout le circuit s'exécute (rendu, validation, planning), mais aucune vidéo n'est envoyée."
            : 'Les clips validés sont envoyés sur les comptes liés selon le planning.'}
        </p>
      </div>
      <button class="switch" role="switch" aria-checked={!bot.dry_run} aria-label="Publication réelle" onclick={toggleLive}></button>
    </div>
  </Section>

  <Section title="Comptes cibles" icon="youtube" description="Chaînes sur lesquelles ce bot publie. Un compte peut servir plusieurs bots.">
    {#if bot.accounts.length}
      <ul class="mb-4 flex flex-wrap gap-2">
        {#each bot.accounts as account (account.id)}
          <li class="flex items-center gap-2 rounded-full border border-line bg-surface-2 py-1 pl-3 pr-1">
            <span class="text-xs font-medium uppercase text-faint">{account.platform}</span>
            <AccountLink name={account.display_name} url={account.url} />
            <button class="icon-btn danger h-6 w-6" title="Délier" aria-label="Délier {account.display_name}" onclick={() => unlink(account)}>
              <Icon name="x" size={13} />
            </button>
          </li>
        {/each}
      </ul>
    {/if}
    {#if available.length}
      <div class="flex gap-2">
        <select class="input max-w-xs" bind:value={toLink} aria-label="Compte à lier">
          <option value="">Choisir un compte…</option>
          {#each available as account}
            <option value={account.id}>{account.display_name} ({account.platform})</option>
          {/each}
        </select>
        <button class="btn" disabled={!toLink} onclick={link}><Icon name="link" /> Lier</button>
      </div>
    {:else if !accounts.length}
      <p class="text-sm text-muted">
        Aucun compte connecté.
        <button class="font-medium text-accent hover:underline" onclick={() => (app.view = 'settings')}>Connecter une chaîne dans les paramètres</button>
      </p>
    {/if}
  </Section>

  <Section title="Découpage" icon="sparkle" description="Comment les extraits sont choisis et mis en forme.">
    <div class="grid grid-cols-2 gap-4 md:grid-cols-3">
      <div>
        <label class="label" for="min">Durée minimale (s)</label>
        <input id="min" class="input" type="number" min="5" max="180" bind:value={clip.min_seconds} />
      </div>
      <div>
        <label class="label" for="max">Durée maximale (s)</label>
        <input id="max" class="input" type="number" min="5" max="180" bind:value={clip.max_seconds} />
      </div>
      <div>
        <label class="label" for="count">Clips par vidéo</label>
        <input id="count" class="input" type="number" min="1" max="30" bind:value={clip.max_clips} />
      </div>
      <div>
        <label class="label" for="words">Mots par sous-titre</label>
        <input id="words" class="input" type="number" min="1" max="8" bind:value={clip.subtitle_words} />
      </div>
      <div>
        <label class="label" for="font">Taille du texte</label>
        <input id="font" class="input" type="number" min="40" max="140" bind:value={clip.font_size} />
      </div>
      <div class="col-span-2 md:col-span-1">
        <label class="label" for="tags">Tags</label>
        <input id="tags" class="input" placeholder="podcast, humour" bind:value={clip.tags} />
      </div>
    </div>

    <div class="mt-6 border-t border-line pt-5">
      <p class="label">Mise en page verticale</p>
      <div class="grid gap-2 sm:grid-cols-2">
        {#each LAYOUTS as l}
          <button
            class="rounded-xl border px-4 py-3 text-left transition-colors {clip.layout === l.value ? 'border-accent bg-accent-soft' : 'border-line hover:bg-surface-2'}"
            onclick={() => (clip.layout = l.value)}
          >
            <span class="block font-medium">{l.label}</span>
            <span class="mt-0.5 block text-xs text-muted">{l.hint}</span>
          </button>
        {/each}
      </div>
    </div>

    <div class="mt-5 grid gap-4 md:grid-cols-2">
      <div class="flex items-start justify-between gap-4">
        <div>
          <p class="label mb-0">Couper les silences</p>
          <p class="hint mt-1">Retire les passages sans voix ni son de jeu (plus de 0,7 s) pour garder le rythme.</p>
        </div>
        <button class="switch mt-1" role="switch" aria-checked={clip.trim_silences} aria-label="Couper les silences"
          onclick={() => (clip.trim_silences = !clip.trim_silences)}></button>
      </div>
      <div class="flex items-start justify-between gap-4">
        <div>
          <p class="label mb-0">Texte d'accroche</p>
          <p class="hint mt-1">Une phrase écrite par l'IA, affichée au début du clip pour retenir le spectateur.</p>
          {#if clip.hook_text}
            <div class="mt-2 flex items-center gap-2">
              <input id="hook-s" class="input w-20" type="number" min="1" max="10" step="0.5" bind:value={clip.hook_seconds} />
              <label for="hook-s" class="text-sm text-muted">secondes</label>
            </div>
          {/if}
        </div>
        <button class="switch mt-1" role="switch" aria-checked={clip.hook_text} aria-label="Texte d'accroche"
          onclick={() => (clip.hook_text = !clip.hook_text)}></button>
      </div>
    </div>
    <p class="hint mt-4">Avec un pic de chat, l'extrait démarre juste avant l'action ; sinon il commence et finit sur une fin de phrase. Les changements s'appliquent aux prochains rendus.</p>
    <div class="mt-5 flex justify-end">
      <button class="btn btn-primary" onclick={saveClip}><Icon name="check" /> Enregistrer</button>
    </div>
  </Section>

  <Section title="Planning" icon="clock" description="Les clips validés sont publiés un par un, à ce rythme.">
    {#snippet actions()}
      <label class="flex cursor-pointer items-center gap-2 text-sm text-muted">
        Actif
        <button
          class="switch"
          role="switch"
          aria-checked={schedule.enabled}
          aria-label="Planning actif"
          onclick={() => (schedule.enabled = !schedule.enabled)}
        ></button>
      </label>
    {/snippet}
    <div class="grid gap-5 md:grid-cols-2">
      <div>
        <label class="label" for="every">Une publication toutes les</label>
        <div class="flex items-center gap-2">
          <input id="every" class="input w-24" type="number" min="0.5" step="0.5" bind:value={schedule.every_hours} />
          <span class="text-muted">heures</span>
        </div>
      </div>
      <div>
        <span class="label">Jours</span>
        <div class="flex gap-1.5">
          {#each DAYS as day, i}
            <button
              class="h-9 w-9 rounded-full border text-sm font-medium transition-colors
                {schedule.days[i] ? 'border-accent bg-accent text-white' : 'border-line text-muted hover:border-faint'}"
              aria-pressed={schedule.days[i]}
              title={day.name}
              onclick={() => (schedule.days[i] = !schedule.days[i])}
            >{day.label}</button>
          {/each}
        </div>
      </div>
      <div>
        <span class="label">Plage horaire</span>
        <div class="flex items-center gap-2">
          <input class="input w-32" type="time" aria-label="Début" bind:value={schedule.start} />
          <span class="text-muted">à</span>
          <input class="input w-32" type="time" aria-label="Fin" bind:value={schedule.end} />
        </div>
        <p class="hint">Mêmes heures de début et de fin : toute la journée.</p>
      </div>
      <div>
        <label class="label" for="tz">Fuseau horaire</label>
        <input id="tz" class="input" list="timezones" bind:value={schedule.timezone} />
        <datalist id="timezones">
          {#each TIMEZONES as tz}<option value={tz}></option>{/each}
        </datalist>
      </div>
    </div>
    {#if !bot.schedule}
      <p class="mt-4 flex items-center gap-2 text-sm text-warning">
        <Icon name="alert" size={14} /> Pas encore de planning : les clips validés attendront qu'il soit enregistré.
      </p>
    {/if}
    <div class="mt-5 flex justify-end">
      <button class="btn btn-primary" onclick={saveSchedule}><Icon name="check" /> Enregistrer</button>
    </div>
  </Section>
</div>
