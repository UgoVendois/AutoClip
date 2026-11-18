<script>
  import { untrack } from 'svelte';
  import { api, videoUrl } from '../api.js';
  import { JOB_KIND, PUBLISH_STATUS, SOURCE_STATUS, fmtClock, fmtDate } from '../format.js';
  import { app, refreshBots, run } from '../state.svelte.js';
  import Badge from './Badge.svelte';
  import Icon from './Icon.svelte';
  import Section from './Section.svelte';

  let { bot, tick, reload } = $props();

  let clips = $state([]);
  let publications = $state([]);
  let failedJobs = $state([]);
  let edits = $state({}); // { [clipId]: { title, description } }
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
        if (c.status === 'review' && !edits[c.id]) edits[c.id] = { title: c.title, description: c.description };
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
      ? await run(api.post(`/clips/${clip.id}/approve`, edits[clip.id]), 'Clip validé : il sera publié selon le planning')
      : await run(api.post(`/clips/${clip.id}/reject`), 'Clip rejeté');
    busy[clip.id] = false;
    if (ok) {
      delete edits[clip.id];
      await load(bot.id);
      reload();
      refreshBots();
    }
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
        : 'Aucun clip en attente. Ajoute une vidéo dans Configuration pour en générer.'}
    >
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
                  <span title="Score de l'analyse">score {clip.score.toFixed(2)}</span>
                </div>
                {#if edits[clip.id]}
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
                  {pub.account_name} · {pub.scheduled_at ? fmtDate(pub.scheduled_at) : 'créneau à venir'}
                </p>
                {#if pub.last_error}<p class="mt-1 text-xs text-danger">{pub.last_error}</p>{/if}
              </div>
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
                <p class="mt-0.5 text-xs text-muted">{pub.account_name} · {fmtDate(pub.updated_at)}</p>
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
