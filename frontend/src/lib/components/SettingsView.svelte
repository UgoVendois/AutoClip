<script>
  import { onMount } from 'svelte';
  import { api } from '../api.js';
  import { app, askConfirm, run, toast } from '../state.svelte.js';
  import Icon from './Icon.svelte';
  import Section from './Section.svelte';
  import ThemeSwitch from './ThemeSwitch.svelte';

  const PLATFORMS = {
    youtube: {
      name: 'YouTube',
      help: "Identifiant OAuth « Application de bureau » d'un projet Google Cloud avec l'API YouTube Data v3 activée.",
      url: 'https://console.cloud.google.com/apis/credentials',
      urlLabel: 'Google Cloud Console',
    },
    youtube_data: {
      name: 'YouTube (lecture des commentaires)',
      help: "Facultatif. Clé d'API du même projet Google Cloud, pour repérer les moments cités dans les commentaires.",
      url: 'https://console.cloud.google.com/apis/credentials',
      urlLabel: 'Google Cloud Console',
    },
    twitch: {
      name: 'Twitch',
      help: "Client ID et Client Secret d'une application Twitch, pour trouver les VODs et lire le chat des lives.",
      url: 'https://dev.twitch.tv/console/apps',
      urlLabel: 'Twitch Developer Console',
    },
    tiktok: {
      name: 'TikTok',
      help: "Clés d'une app TikTok for Developers (Content Posting API). Connecteur prévu dans une prochaine phase.",
      url: 'https://developers.tiktok.com/apps',
      urlLabel: 'TikTok for Developers',
    },
    instagram: {
      name: 'Instagram',
      help: "App Meta avec l'API Instagram Graph. Connecteur prévu dans une prochaine phase.",
      url: 'https://developers.facebook.com/apps',
      urlLabel: 'Meta for Developers',
    },
    anthropic: {
      name: 'Anthropic (Claude)',
      help: "Clé d'API pour noter les extraits et rédiger titres et descriptions.",
      url: 'https://platform.claude.com/settings/keys',
      urlLabel: 'Claude Console',
    },
  };
  const FIELD_LABELS = {
    client_id: 'Client ID',
    client_secret: 'Client secret',
    client_key: 'Client key',
    app_id: 'App ID',
    app_secret: 'App secret',
    api_key: "Clé d'API",
  };
  const MODELS = [
    { value: 'tiny', label: 'tiny : très rapide, approximatif' },
    { value: 'base', label: 'base : rapide' },
    { value: 'small', label: 'small : bon compromis (recommandé)' },
    { value: 'medium', label: 'medium : plus précis, lent sur CPU' },
    { value: 'large-v3', label: 'large-v3 : le plus précis, très lent sur CPU' },
  ];
  const LANGUAGES = [
    { value: '', label: 'Détection automatique' },
    { value: 'fr', label: 'Français' },
    { value: 'en', label: 'Anglais' },
    { value: 'es', label: 'Espagnol' },
    { value: 'de', label: 'Allemand' },
    { value: 'it', label: 'Italien' },
    { value: 'pt', label: 'Portugais' },
  ];

  let creds = $state(null);
  let forms = $state({});
  let accounts = $state([]);
  let status = $state(null);
  let connecting = $state(false);
  let fileInput = $state();

  async function loadCreds() {
    creds = await api.get('/credentials');
    for (const [platform, fields] of Object.entries(creds.fields)) {
      forms[platform] = Object.fromEntries(fields.map((f) => [f, '']));
    }
  }

  async function loadAccounts() {
    accounts = (await run(api.get('/accounts'))) || [];
  }

  onMount(async () => {
    await Promise.all([run(loadCreds()), loadAccounts(), (async () => (status = await api.get('/status')))()]);
  });

  async function save(key, value) {
    const updated = await run(api.put('/settings', { [key]: value }), 'Réglage enregistré');
    if (updated) app.settings = updated;
  }

  async function saveCreds(platform) {
    if (await run(api.put(`/credentials/${platform}`, forms[platform]), `Clés ${PLATFORMS[platform].name} enregistrées dans le trousseau`)) {
      await loadCreds();
    }
  }

  async function deleteCreds(platform) {
    const ok = await askConfirm({
      title: `Supprimer les clés ${PLATFORMS[platform].name} ?`,
      message: 'Elles seront effacées du trousseau système. Les comptes déjà connectés restent connectés.',
      confirmLabel: 'Supprimer',
      danger: true,
    });
    if (ok && (await run(api.del(`/credentials/${platform}`), 'Clés supprimées'))) await loadCreds();
  }

  async function importClientSecret(text) {
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      toast("Ce fichier n'est pas un JSON valide.", 'error');
      return;
    }
    if (await run(api.put('/credentials/youtube', data), 'client_secret.json importé dans le trousseau')) await loadCreds();
  }

  async function pickClientSecret() {
    if (app.inWindow) {
      const text = await window.pywebview.api.pick_json();
      if (text) importClientSecret(text);
    } else {
      fileInput.click();
    }
  }

  async function onFile(e) {
    const file = e.target.files?.[0];
    if (file) importClientSecret(await file.text());
    e.target.value = '';
  }

  async function connectYoutube() {
    connecting = true;
    const result = await run(api.post('/accounts/youtube/connect'), 'Chaîne YouTube connectée');
    connecting = false;
    if (result) loadAccounts();
  }

  async function removeAccount(account) {
    const ok = await askConfirm({
      title: `Déconnecter « ${account.display_name} » ?`,
      message: 'Le compte sera retiré de tous les bots et son jeton effacé du trousseau. Les publications programmées vers ce compte seront annulées.',
      confirmLabel: 'Déconnecter',
      danger: true,
    });
    if (ok && (await run(api.del(`/accounts/${account.id}`), 'Compte déconnecté'))) loadAccounts();
  }
</script>

<div class="mx-auto max-w-3xl px-8 pb-16 pt-8">
  <h1 class="font-serif text-3xl tracking-tight">Paramètres</h1>

  <div class="mt-8 space-y-5">
    <Section title="Apparence" icon="sun">
      <div class="max-w-sm"><ThemeSwitch labels /></div>
    </Section>

    {#if app.settings}
      <Section title="Transcription" icon="sparkle" description="faster-whisper tourne sur le processeur de cette machine.">
        <div class="grid gap-4 md:grid-cols-2">
          <div>
            <label class="label" for="model">Modèle</label>
            <select id="model" class="input" value={app.settings.whisper_model} onchange={(e) => save('whisper_model', e.target.value)}>
              {#each MODELS as m}<option value={m.value}>{m.label}</option>{/each}
            </select>
          </div>
          <div>
            <label class="label" for="lang">Langue des vidéos</label>
            <select id="lang" class="input" value={app.settings.whisper_language ?? ''} onchange={(e) => save('whisper_language', e.target.value || null)}>
              {#each LANGUAGES as l}<option value={l.value}>{l.label}</option>{/each}
            </select>
          </div>
        </div>
      </Section>

      <Section title="Publication" icon="send">
        <div class="grid gap-5 md:grid-cols-2">
          <div>
            <label class="label" for="privacy">Visibilité sur YouTube</label>
            <select id="privacy" class="input" value={app.settings.youtube_privacy} onchange={(e) => save('youtube_privacy', e.target.value)}>
              <option value="private">Privée</option>
              <option value="unlisted">Non répertoriée</option>
              <option value="public">Publique</option>
            </select>
            <p class="hint">Tant que Google n'a pas audité ton projet, l'API force les vidéos en privé.</p>
          </div>
          <div class="flex items-start justify-between gap-4">
            <div>
              <p class="label mb-0">Dry run par défaut</p>
              <p class="hint mt-1">Les nouveaux bots ne publient rien tant que tu ne les passes pas en publication réelle.</p>
            </div>
            <button
              class="switch mt-1"
              role="switch"
              aria-checked={app.settings.default_dry_run}
              aria-label="Dry run par défaut"
              onclick={() => save('default_dry_run', !app.settings.default_dry_run)}
            ></button>
          </div>
        </div>
      </Section>
    {/if}

    <Section title="Clés d'API" icon="key" description="Stockées dans le trousseau système (libsecret), jamais en clair. Elles ne sont jamais réaffichées ici.">
      {#if creds}
        <div class="space-y-3">
          {#each Object.entries(creds.fields) as [platform, fields] (platform)}
            {@const configured = creds.configured[platform]}
            <details class="group rounded-xl border border-line" open={platform === 'youtube' && !configured}>
              <summary class="flex cursor-pointer list-none items-center gap-3 px-4 py-3">
                <span class="font-medium">{PLATFORMS[platform]?.name ?? platform}</span>
                {#if configured}
                  <span class="badge badge-success"><Icon name="check" size={11} /> Configurée</span>
                {:else}
                  <span class="badge">Non configurée</span>
                {/if}
                <Icon name="plus" size={14} class="ml-auto text-faint transition-transform group-open:rotate-45" />
              </summary>
              <div class="border-t border-line px-4 pb-4 pt-3">
                <p class="mb-3 text-sm text-muted">
                  {PLATFORMS[platform]?.help}
                  {#if PLATFORMS[platform]?.url}
                    <a
                      class="inline-flex items-center gap-1 font-medium text-accent hover:underline"
                      href={PLATFORMS[platform].url}
                      target="_blank"
                      rel="noreferrer"
                    >{PLATFORMS[platform].urlLabel}<Icon name="external" size={12} /></a>
                  {/if}
                </p>
                {#if forms[platform]}
                  <form class="grid gap-3 md:grid-cols-2" onsubmit={(e) => { e.preventDefault(); saveCreds(platform); }}>
                    {#each fields as field}
                      <div>
                        <label class="label" for="{platform}-{field}">{FIELD_LABELS[field] ?? field}</label>
                        <input
                          id="{platform}-{field}"
                          class="input"
                          type="password"
                          autocomplete="off"
                          placeholder={configured ? '•••••••• (inchangé)' : ''}
                          bind:value={forms[platform][field]}
                        />
                      </div>
                    {/each}
                    <div class="flex flex-wrap items-center gap-2 md:col-span-2">
                      {#if platform === 'youtube'}
                        <button type="button" class="btn" onclick={pickClientSecret}><Icon name="upload" /> Importer client_secret.json</button>
                      {/if}
                      {#if configured}
                        <button type="button" class="btn btn-danger" onclick={() => deleteCreds(platform)}><Icon name="trash" /> Supprimer</button>
                      {/if}
                      <button class="btn btn-primary ml-auto" disabled={fields.some((f) => !forms[platform][f])}>
                        <Icon name="check" /> Enregistrer
                      </button>
                    </div>
                  </form>
                {/if}
              </div>
            </details>
          {/each}
        </div>
        <input bind:this={fileInput} type="file" accept=".json,application/json" class="hidden" onchange={onFile} />
      {:else}
        <span class="spinner text-muted"></span>
      {/if}
    </Section>

    <Section title="Comptes connectés" icon="youtube" description="Chaînes sur lesquelles tes bots peuvent publier. Lie-les à un bot dans sa configuration.">
      {#if accounts.length}
        <ul class="mb-4 divide-y divide-line rounded-xl border border-line">
          {#each accounts as account (account.id)}
            <li class="flex items-center gap-3 px-4 py-3">
              <span class="text-xs font-medium uppercase text-faint">{account.platform}</span>
              <span class="flex-1">{account.display_name}</span>
              {#if !account.connected}<span class="badge" title="Compte utilisé pour les dry runs">Simulé</span>{/if}
              <button class="icon-btn danger" title="Déconnecter" aria-label="Déconnecter {account.display_name}" onclick={() => removeAccount(account)}>
                <Icon name="trash" size={14} />
              </button>
            </li>
          {/each}
        </ul>
      {/if}
      <div class="flex flex-wrap items-center gap-3">
        <button class="btn btn-primary" disabled={connecting || !creds?.configured.youtube} onclick={connectYoutube}>
          {#if connecting}<span class="spinner"></span>{:else}<Icon name="youtube" />{/if}
          Connecter une chaîne YouTube
        </button>
        {#if connecting}
          <p class="text-sm text-muted">Une page Google s'est ouverte dans ton navigateur : autorise l'accès puis reviens ici.</p>
        {:else if creds && !creds.configured.youtube}
          <p class="text-sm text-muted">Enregistre d'abord les clés YouTube ci-dessus.</p>
        {/if}
      </div>
    </Section>

    {#if status}
      <p class="px-1 text-xs text-faint">AutoClip {status.version} · données : {status.home}</p>
    {/if}
  </div>
</div>
