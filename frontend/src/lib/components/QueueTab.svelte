<script>
  import { untrack } from 'svelte';
  import { api, videoUrl } from '../api.js';
  import { JOB_KIND, PUBLISH_STATUS, SOURCE_STATUS, fmtClock, fmtDate } from '../format.js';
  import { app, askConfirm, refreshBots, run, toast } from '../state.svelte.js';
  import Badge from './Badge.svelte';
  import Icon from './Icon.svelte';
  import AccountLink from './AccountLink.svelte';
  import Section from './Section.svelte';

  let { bot, tick, reload } = $props();

  let clips = $state([]);
  let publications = $state([]);
  let failedJobs = $state([]);
  let edits = $state({}); // { [clipId]: { title, description, hook } }
  let busy = $state({}); // { [clipId]: true } pendant une validation
  let loaded = $state(false);

  const review = $derived(clips.filter((c) => c.status === 'review'));
  const rendering = $derived(clips.filter((c) => c.status === 'render').length);
  const working = $derived(bot.sources.filter((s) => SOURCE_STATUS[s.status]?.busy));
  const upcoming = $derived(publications.filter((p) => p.status !== 'published'));
  const recent = $derived(
    publications.filter((p) => p.status === 'published').sort((a, b) => b.updated_at - a.updated_at).slice(0, 8),
  );

  async function load(botId) {
    try {
      const [allClips, pubs, jobs] = await Promise.all([
        api.get(`/clips?bot_id=${botId}`),
        api.get(`/publish-jobs?bot_id=${botId}`),
        api.get('/jobs?status=failed&limit=200'),
      ]);
      for (const c of allClips) {
        if (c.status === 'review' && !edits[c.id]) edits[c.id] = { title: c.title, description: c.description, hook: c.hook ?? '' };
      }
      // Ne garder que les échecs qui concernent ce bot.
      const sourceIds = new Set(bot.sources.map((s) => s.id));
      const clipIds = new Set(allClips.map((c) => c.id));
      const pubIds = new Set(pubs.map((p) => p.id));
      failedJobs = jobs.jobs.filter((j) => {
        const p = JSON.parse(j.payload);
        return sourceIds.has(p.source_id) || clipIds.has(p.clip_id) || pubIds.has(p.publish_job_id);
      });
      clips = allClips;
      publications = pubs;
      loaded = true;
    } catch {
      /* le prochain rafraîchissement réessaiera */
    }
  }

  $effect(() => {
    tick; // relance à chaque rafraîchissement périodique
    untrack(() => load(bot.id));
  });

  async function decide(clip, approve) {
    busy[clip.id] = true;
    const ok = approve
      ? await run(api.post(`/clips/${clip.id}/approve`, { title: edits[clip.id].title, description: edits[clip.id].description }), 'Clip validé : il sera publié selon le planning')
      : await run(api.post(`/clips/${clip.id}/reject`), 'Clip rejeté');
    busy[clip.id] = false;
    if (ok) {
      delete edits[clip.id];
      await load(bot.id);
      reload();
      refreshBots();
    }
  }

  // L'accroche est incrustée dans la vidéo : la changer relance le rendu du clip.
  async function saveHook(clip) {
    busy[clip.id] = true;
    const ok = await run(api.put(`/clips/${clip.id}/hook`, { hook: edits[clip.id].hook }), 'Nouvelle accroche : rendu en cours');
    busy[clip.id] = false;
    if (ok) {
      delete edits[clip.id];
      await load(bot.id);
    }
  }

  let bulk = $state(false);
  const autoApprove = $derived(Boolean(bot.clip_settings?.auto_approve));

  async function approveAll() {
    const ok = await askConfirm({
      title: `Valider les ${review.length} clips ?`,
      message: "Titres et descriptions tels qu'affichés ; les meilleurs scores partent en premier, selon le planning.",
      confirmLabel: 'Tout valider',
    });
    if (!ok) return;
    bulk = true;
    // Titres et descriptions modifiés à la main : envoyés avec la validation.
    const changed = {};
    for (const clip of review) {
      const e = edits[clip.id];
      if (e && (e.title !== clip.title || e.description !== clip.description)) {
        changed[clip.id] = { title: e.title, description: e.description };
      }
    }
    const res = await run(api.post(`/bots/${bot.id}/clips/approve-all`, changed));
    bulk = false;
    if (res) {
      toast(`${res.approved} clip${res.approved > 1 ? 's' : ''} validé${res.approved > 1 ? 's' : ''}`);
      edits = {};
      await load(bot.id);
      reload();
      refreshBots();
    }
  }

  async function toggleAutoApprove() {
    const on = !autoApprove;
    if (await run(api.patch(`/bots/${bot.id}`, { clip_settings: { auto_approve: on } }),
                  on ? 'Validation automatique activée : les nouveaux clips partent sans relecture' : 'Validation automatique désactivée')) {
      reload();
    }
  }

  async function publishNow(pub) {
    const ok = await askConfirm({
      title: 'Publier maintenant ?',
      message: bot.dry_run
        ? `« ${pub.title} » sera publié tout de suite (simulé : le bot est en dry run).`
        : `« ${pub.title} » part tout de suite sur ${pub.account_name}. Les publications suivantes reprennent l'espacement du planning à partir de maintenant.`,
      confirmLabel: 'Publier',
    });
    if (ok && (await run(api.post(`/publish-jobs/${pub.id}/publish-now`), 'Publication lancée'))) load(bot.id);
  }

  async function retry(job) {
    if (await run(api.post(`/jobs/${job.id}/retry`), 'Relancé')) load(bot.id);
  }
</script>

{#if !loaded}
  <div class="grid h-40 place-items-center text-muted"><span class="spinner"></span></div>
{:else}
  <div class="space-y-5">
    {#if !bot.accounts.length && review.length}
      <div class="flex items-center gap-3 rounded-xl border border-line bg-warning/10 px-4 py-3 text-sm">
        <Icon name="alert" size={16} class="shrink-0 text-warning" />
        <p class="flex-1">Ce bot n'a aucun compte cible : lie un compte avant de valider des clips.</p>
        <button class="btn btn-sm" onclick={() => (app.tab = 'config')}>Configurer</button>
      </div>
    {/if}

    {#if working.length || rendering}
      <Section title="En cours de traitement" icon="refresh">
        <ul class="space-y-2">
          {#each working as src (src.id)}
            <li class="flex items-center gap-3">
              <Badge status={src.status} map={SOURCE_STATUS} />
              <span class="truncate text-muted" title={src.uri}>{src.uri}</span>
            </li>
          {/each}
          {#if rendering}
            <li class="flex items-center gap-3">
              <span class="badge badge-warning"><span class="spinner" style="width:0.6rem;height:0.6rem;border-width:1.5px"></span>Rendu</span>
              <span class="text-muted">{rendering} clip{rendering > 1 ? 's' : ''} en cours de montage</span>
            </li>
          {/if}
        </ul>
        <p class="hint">La transcription tourne sur le processeur : compte à peu près la durée de la vidéo.</p>
      </Section>
    {/if}

    <Section
      title="À valider"
      icon="check"
      description={review.length
        ? 'Regarde chaque clip, ajuste le titre si besoin, puis valide ou rejette.'
        : autoApprove
          ? 'Validation automatique active : les nouveaux clips partent directement dans le planning.'
          : 'Aucun clip en attente. Ajoute une vidéo dans Configuration pour en générer.'}
    >
      {#snippet actions()}
        <div class="flex flex-wrap items-center justify-end gap-3">
        <label class="flex items-center gap-2 text-sm text-muted" title="Chaque nouveau clip est validé dès la fin de son montage, sans relecture">
          Valider automatiquement
          <button class="switch" role="switch" aria-checked={autoApprove} aria-label="Valider automatiquement" onclick={toggleAutoApprove}></button>
        </label>
        {#if review.length > 1}
          <button class="btn btn-sm btn-primary" disabled={bulk || !bot.accounts.length} onclick={approveAll}>
            {#if bulk}<span class="spinner"></span>{:else}<Icon name="check" size={14} />{/if}
            Tout valider ({review.length})
          </button>
        {/if}
        </div>
      {/snippet}
      {#if review.length}
        <div class="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(440px,1fr))]">
          {#each review as clip (clip.id)}
            <article class="flex overflow-hidden rounded-xl border border-line bg-surface">
              <!-- svelte-ignore a11y_media_has_caption : sous-titres incrustés dans la vidéo -->
              <video
                class="aspect-[9/16] w-[176px] shrink-0 self-start bg-black object-contain"
                src={videoUrl(clip.id)}
                preload="metadata"
                controls
              ></video>
              <div class="flex min-w-0 flex-1 flex-col gap-3 p-4">
                <div class="flex items-center justify-between text-xs text-muted">
                  <span title="Position dans la vidéo source">
                    {fmtClock(clip.start_ms / 1000)} → {fmtClock(clip.end_ms / 1000)}
                    · {Math.round((clip.end_ms - clip.start_ms) / 1000)} s
                  </span>
                  <span title="Score de l'analyse">score {clip.score > 1 ? Math.round(clip.score) : clip.score.toFixed(2)}</span>
                </div>
                {#if clip.reason}
                  <p class="text-xs text-muted"><Icon name="sparkle" size={12} class="mr-1 inline text-accent" />{clip.reason}</p>
                {/if}
                {#if edits[clip.id]}
                  <div>
                    <label class="label" for="hook-{clip.id}">Accroche à l'écran</label>
                    <div class="flex gap-2">
                      <input id="hook-{clip.id}" class="input" maxlength="60" placeholder="Aucune accroche" bind:value={edits[clip.id].hook} />
                      {#if (edits[clip.id].hook ?? '') !== (clip.hook ?? '')}
                        <button class="btn shrink-0" disabled={busy[clip.id]} onclick={() => saveHook(clip)} title="Refaire la vidéo avec cette accroche">
                          <Icon name="refresh" size={14} /> Appliquer
                        </button>
                      {/if}
                    </div>
                  </div>
                  <div>
                    <label class="label" for="title-{clip.id}">Titre</label>
                    <input id="title-{clip.id}" class="input" maxlength="100" bind:value={edits[clip.id].title} />
                  </div>
                  <div>
                    <label class="label" for="desc-{clip.id}">Description</label>
                    <textarea id="desc-{clip.id}" class="input text-sm" rows="3" bind:value={edits[clip.id].description}></textarea>
                  </div>
                {/if}
                <div class="mt-auto flex gap-2 pt-1">
                  <button class="btn btn-danger flex-1" disabled={busy[clip.id]} onclick={() => decide(clip, false)}>
                    <Icon name="x" /> Rejeter
                  </button>
                  <button class="btn btn-primary flex-1" disabled={busy[clip.id] || !bot.accounts.length} onclick={() => decide(clip, true)}>
                    <Icon name="check" /> Valider
                  </button>
                </div>
              </div>
            </article>
          {/each}
        </div>
      {/if}
    </Section>

    <Section title="Publications à venir" icon="clock" description={bot.dry_run ? 'Dry run : ces publications sont simulées.' : ''}>
      {#if upcoming.length}
        <ul class="divide-y divide-line rounded-xl border border-line">
          {#each upcoming as pub (pub.id)}
            <li class="flex items-center gap-4 px-4 py-3">
              <div class="min-w-0 flex-1">
                <p class="truncate font-medium">{pub.title}</p>
                <p class="mt-0.5 text-xs text-muted">
                  <AccountLink name={pub.account_name} url={pub.account_url} /> · {pub.scheduled_at ? fmtDate(pub.scheduled_at) : 'créneau à venir'}
                </p>
                {#if pub.last_error}<p class="mt-1 text-xs text-danger">{pub.last_error}</p>{/if}
              </div>
              {#if pub.status === 'pending'}
                <button class="btn btn-sm" onclick={() => publishNow(pub)} title="Publier tout de suite, sans attendre le créneau">
                  <Icon name="send" size={14} /> Publier
                </button>
              {/if}
              <Badge status={pub.status} map={PUBLISH_STATUS} />
            </li>
          {/each}
        </ul>
      {:else}
        <p class="text-sm text-muted">Rien de programmé. Les clips validés apparaissent ici avec leur créneau.</p>
      {/if}
      {#if !bot.schedule && upcoming.length}
        <p class="mt-3 flex items-center gap-2 text-sm text-warning">
          <Icon name="alert" size={14} /> Aucun planning : enregistre-en un dans Configuration pour que ces clips partent.
        </p>
      {/if}
    </Section>

    {#if recent.length}
      <Section title="Dernières publications" icon="send">
        <ul class="divide-y divide-line rounded-xl border border-line">
          {#each recent as pub (pub.id)}
            <li class="flex items-center gap-4 px-4 py-3">
              <div class="min-w-0 flex-1">
                <p class="truncate">{pub.title}</p>
                <p class="mt-0.5 text-xs text-muted"><AccountLink name={pub.account_name} url={pub.account_url} /> · {fmtDate(pub.updated_at)}</p>
              </div>
              {#if pub.url?.startsWith('http')}
                <a class="btn btn-sm" href={pub.url} target="_blank" rel="noreferrer"><Icon name="external" size={14} /> Voir</a>
              {:else}
                <Badge status="published" map={PUBLISH_STATUS} />
              {/if}
            </li>
          {/each}
        </ul>
      </Section>
    {/if}

    {#if failedJobs.length}
      <Section title="Échecs" icon="alert" description="Ces étapes ont échoué après plusieurs essais.">
        <ul class="divide-y divide-line rounded-xl border border-line">
          {#each failedJobs as job (job.id)}
            <li class="flex items-center gap-4 px-4 py-3">
              <div class="min-w-0 flex-1">
                <p class="font-medium">{JOB_KIND[job.kind] ?? job.kind}</p>
                <p class="mt-0.5 line-clamp-2 text-xs text-danger" title={job.last_error}>{job.last_error}</p>
              </div>
              <button class="btn btn-sm" onclick={() => retry(job)}><Icon name="refresh" size={14} /> Relancer</button>
            </li>
          {/each}
        </ul>
      </Section>
    {/if}
  </div>
{/if}
