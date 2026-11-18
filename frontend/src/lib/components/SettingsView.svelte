<script>
  import { onDestroy, onMount } from 'svelte';
  import { api } from '../api.js';
  import { app, askConfirm, run, toast } from '../state.svelte.js';
  import Icon from './Icon.svelte';
  import ModelPicker from './ModelPicker.svelte';
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
      url: 'https://developers.facebook.com/',
      urlLabel: 'Meta for Developers',
    },
    anthropic: {
      name: 'Anthropic (Claude)',
      help: "Clé d'API pour noter les extraits, rédiger titres et descriptions, et proposer l'identité des chaînes.",
      url: 'https://platform.claude.com/settings/keys',
      urlLabel: 'Claude Console',
    },
    gemini: {
      name: 'Google Gemini',
      help: "Clé d'API Gemini : texte, et images (Nano Banana) pour les photos de profil et les bannières.",
      url: 'https://aistudio.google.com/apikey',
      urlLabel: 'Google AI Studio',
    },
    openai: {
      name: 'OpenAI (ChatGPT)',
      help: "Clé d'API OpenAI : texte, et images (GPT Image) pour les photos de profil et les bannières.",
      url: 'https://platform.openai.com/api-keys',
      urlLabel: 'OpenAI Platform',
    },
    mistral: {
      name: 'Mistral AI',
      help: "Clé d'API Mistral (La Plateforme) : modèles de texte européens.",
      url: 'https://console.mistral.ai/api-keys',
      urlLabel: 'Console Mistral',
    },
  };

  // Les clés rangées par usage : qui réfléchit, où l'on publie, où l'on cherche.
  const GROUPS = [
    {
      id: 'ai',
      title: 'Intelligence artificielle',
      icon: 'cpu',
      blurb: 'Les cerveaux : notation des extraits, textes, identité des chaînes et visuels.',
      platforms: ['anthropic', 'gemini', 'openai', 'mistral'],
    },
    {
      id: 'social',
      title: 'Réseaux sociaux',
      icon: 'share',
      blurb: 'Là où les Shorts sont publiés.',
      platforms: ['youtube', 'tiktok', 'instagram'],
    },
    {
      id: 'scouting',
      title: 'Veille & collecte',
      icon: 'radar',
      blurb: 'Là où les bots repèrent les moments forts à clipper.',
      platforms: ['twitch', 'youtube_data'],
    },
  ];

  const TEXT_PROVIDERS = [
    { value: 'anthropic', label: 'Claude (Anthropic)' },
    { value: 'gemini', label: 'Gemini (Google)' },
    { value: 'openai', label: 'ChatGPT (OpenAI)' },
    { value: 'mistral', label: 'Mistral AI' },
    { value: 'ollama', label: 'Ollama (local, gratuit)' },
  ];
  const IMAGE_PROVIDERS = [
    { value: 'svg', label: 'Dessin vectoriel par le modèle de texte' },
    { value: 'gemini', label: 'Gemini : Nano Banana' },
    { value: 'openai', label: 'OpenAI : GPT Image' },
  ];
  let defaults = $state({ text: {}, image: {} });
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
    await Promise.all([
      run(loadCreds()),
      loadAccounts(),
      (async () => (status = await api.get('/status')))(),
      (async () => (defaults = await api.get('/ai/providers')))(),
    ]);
  });

  async function save(key, value) {
    const updated = await run(api.put('/settings', { [key]: value }), 'Réglage enregistré');
    if (updated) app.settings = updated;
  }

  async function saveCreds(platform) {
    if (await run(api.put(`/credentials/${platform}`, forms[platform]), `Clés ${PLATFORMS[platform].name} enregistrées dans le trousseau`)) {
      await loadCreds();
      testCreds(platform);
    }
  }

  // Résultat du dernier test par plateforme : { state: running | ok | bad | unknown, message }.
  let checks = $state({});

  async function testCreds(platform) {
    checks[platform] = { state: 'running', message: 'Test en cours…' };
    try {
      const res = await api.post(`/credentials/${platform}/test`);
      checks[platform] = { state: res.ok ? 'ok' : res.conclusive ? 'bad' : 'unknown', message: res.message };
    } catch (err) {
      checks[platform] = { state: 'unknown', message: err.message };
    }
  }

  async function testAll() {
    await Promise.all(Object.keys(creds.configured).filter((p) => creds.configured[p]).map(testCreds));
    const bad = Object.values(checks).filter((c) => c.state === 'bad').length;
    toast(bad ? `${bad} clé${bad > 1 ? 's' : ''} refusée${bad > 1 ? 's' : ''}` : 'Tests terminés', bad ? 'error' : 'success');
  }

  const CHECK_BADGE = {
    running: { cls: 'badge', label: 'Test…' },
    ok: { cls: 'badge badge-success', label: 'Valide', icon: 'check' },
    bad: { cls: 'badge badge-danger', label: 'Invalide', icon: 'x' },
    unknown: { cls: 'badge badge-warning', label: 'Non vérifiée', icon: 'alert' },
  };

  async function deleteCreds(platform) {
    const ok = await askConfirm({
      title: `Supprimer les clés ${PLATFORMS[platform].name} ?`,
      message: 'Elles seront effacées du trousseau système. Les comptes déjà connectés restent connectés.',
      confirmLabel: 'Supprimer',
      danger: true,
    });
    if (ok && (await run(api.del(`/credentials/${platform}`), 'Clés supprimées'))) {
      delete checks[platform];
      await loadCreds();
    }
  }

  async function importClientSecret(text) {
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      toast("Ce fichier n'est pas un JSON valide.", 'error');
      return;
    }
    if (await run(api.put('/credentials/youtube', data), 'client_secret.json importé dans le trousseau')) {
      await loadCreds();
      testCreds('youtube');
    }
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

  // Connexion OAuth : l'URL Google arrive tout de suite, on suit ensuite l'état.
  let oauth = $state(null); // { auth_url, remaining, error }
  let oauthTimer = null;

  function stopPolling() {
    clearInterval(oauthTimer);
    oauthTimer = null;
    connecting = false;
  }

  async function connectYoutube() {
    connecting = true;
    const started = await run(api.post('/accounts/youtube/connect'));
    if (!started) {
      connecting = false;
      return;
    }
    oauth = { auth_url: started.auth_url, remaining: started.timeout, error: null };
    oauthTimer = setInterval(async () => {
      oauth.remaining = Math.max(0, oauth.remaining - 1.5);
      let st;
      try {
        st = await api.get('/accounts/youtube/connect');
      } catch {
        return;
      }
      if (st.state === 'pending') return;
      stopPolling();
      if (st.state === 'connected') {
        oauth = null;
        toast('Chaîne YouTube connectée', 'success');
        loadAccounts();
      } else if (st.state === 'error') {
        oauth = { ...oauth, error: st.error };
      } else {
        oauth = null;
      }
    }, 1500);
  }

  async function cancelConnect() {
    await run(api.del('/accounts/youtube/connect'));
    stopPolling();
    oauth = null;
  }

  onDestroy(() => clearInterval(oauthTimer));

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

      <Section title="Agent identité de chaîne" icon="palette" description="L'IA qui propose nom, description, playlists, photo de profil et bannière (onglet Chaîne de chaque bot).">
        <div class="grid gap-x-4 gap-y-5 md:grid-cols-2">
          <div>
            <label class="label" for="id-provider">Textes : fournisseur</label>
            <select id="id-provider" class="input" value={app.settings.identity_provider}
              onchange={async (e) => { await save('identity_provider', e.target.value); await save('identity_model', null); }}>
              {#if app.settings.identity_provider === 'fake'}<option value="fake">Simulé (tests, aucun appel)</option>{/if}
              {#each TEXT_PROVIDERS as p}<option value={p.value}>{p.label}{creds && creds.fields[p.value] && !creds.configured[p.value] ? ' (clé manquante)' : ''}</option>{/each}
            </select>
          </div>
          <div>
            <label class="label" for="id-model">Textes : modèle</label>
            <ModelPicker id="id-model" provider={app.settings.identity_provider} value={app.settings.identity_model}
              defaultModel={defaults.text[app.settings.identity_provider]} onchange={(v) => save('identity_model', v)} />
          </div>
          <div>
            <label class="label" for="img-provider">Images : fournisseur</label>
            <select id="img-provider" class="input" value={app.settings.image_provider}
              onchange={async (e) => { await save('image_provider', e.target.value); await save('image_model', null); }}>
              {#if app.settings.image_provider === 'fake'}<option value="fake">Simulé (tests, aucun appel)</option>{/if}
              {#each IMAGE_PROVIDERS as p}<option value={p.value}>{p.label}{creds && creds.fields[p.value] && !creds.configured[p.value] ? ' (clé manquante)' : ''}</option>{/each}
            </select>
            <p class="hint">Le dessin vectoriel donne un rendu graphique et net ; Gemini et OpenAI génèrent de vraies illustrations.</p>
          </div>
          <div>
            <label class="label" for="img-model">Images : modèle</label>
            <ModelPicker id="img-model" kind="image" provider={app.settings.image_provider} value={app.settings.image_model}
              defaultModel={defaults.image[app.settings.image_provider]} onchange={(v) => save('image_model', v)} />
          </div>
        </div>
      </Section>
    {/if}

    <Section title="Clés d'API" icon="key" description="Stockées dans le trousseau système (libsecret), jamais en clair. Elles ne sont jamais réaffichées ici.">
      {#snippet actions()}
        {#if creds && Object.values(creds.configured).some(Boolean)}
          <button class="btn btn-sm" disabled={Object.values(checks).some((c) => c.state === 'running')} onclick={testAll}>
            <Icon name="refresh" size={14} /> Tout tester
          </button>
        {/if}
      {/snippet}
      {#if creds}
        <div class="space-y-7">
        {#each GROUPS as group (group.id)}
        {@const count = group.platforms.filter((p) => creds.configured[p]).length}
        <div>
          <div class="mb-3 flex items-center gap-2.5">
            <span class="grid size-7 place-items-center rounded-lg bg-accent-soft text-accent"><Icon name={group.icon} size={15} /></span>
            <div class="min-w-0 flex-1">
              <h3 class="text-sm font-semibold">{group.title}</h3>
              <p class="text-xs text-muted">{group.blurb}</p>
            </div>
            <span class="text-xs text-faint">{count} / {group.platforms.length} configurée{count > 1 ? 's' : ''}</span>
          </div>
        <div class="space-y-2">
          {#each group.platforms.filter((p) => creds.fields[p]) as platform (platform)}
            {@const fields = creds.fields[platform]}
            {@const configured = creds.configured[platform]}
            {@const check = checks[platform]}
            <details class="group rounded-xl border border-line" open={platform === 'youtube' && !configured}>
              <summary class="flex cursor-pointer list-none items-center gap-3 px-4 py-3">
                <span class="font-medium">{PLATFORMS[platform]?.name ?? platform}</span>
                {#if configured && check}
                  {@const b = CHECK_BADGE[check.state]}
                  <span class={b.cls} title={check.message}>
                    {#if check.state === 'running'}<span class="spinner size-2.5"></span>{:else}<Icon name={b.icon} size={11} />{/if}
                    {b.label}
                  </span>
                {:else if configured}
                  <span class="badge badge-success"><Icon name="check" size={11} /> Configurée</span>
                {:else}
                  <span class="badge">Non configurée</span>
                {/if}
                {#if configured}
                  <button
                    type="button"
                    class="btn btn-sm btn-ghost ml-auto"
                    disabled={check?.state === 'running'}
                    onclick={(e) => { e.preventDefault(); e.stopPropagation(); testCreds(platform); }}
                  ><Icon name="refresh" size={13} /> Tester</button>
                {/if}
                <Icon name="plus" size={14} class="{configured ? '' : 'ml-auto'} text-faint transition-transform group-open:rotate-45" />
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
                {#if configured && check && check.state !== 'running'}
                  <p class="mb-3 flex items-start gap-2 rounded-lg px-3 py-2 text-sm
                    {check.state === 'ok' ? 'bg-[var(--success-soft)] text-success' : check.state === 'bad' ? 'bg-[var(--danger-soft)] text-danger' : 'bg-[var(--warning-soft)] text-warning'}">
                    <Icon name={CHECK_BADGE[check.state].icon} size={14} class="mt-0.5 shrink-0" />{check.message}
                  </p>
                {/if}
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
        </div>
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
              {#if account.connected && account.platform === 'youtube' && !account.can_manage}
                <button class="badge badge-warning" title="Nouvelle autorisation nécessaire pour la bannière, la description et les playlists" onclick={connectYoutube} disabled={connecting}>
                  <Icon name="refresh" size={11} /> Reconnecter
                </button>
              {/if}
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
          <button class="btn" onclick={cancelConnect}><Icon name="x" /> Annuler</button>
        {:else if creds && !creds.configured.youtube}
          <p class="text-sm text-muted">Enregistre d'abord les clés YouTube ci-dessus.</p>
        {/if}
      </div>
      {#if oauth}
        <div class="mt-4 rounded-xl border px-4 py-3 text-sm {oauth.error ? 'border-[var(--danger)] bg-[var(--danger-soft)]' : 'border-line bg-surface-2'}">
          {#if oauth.error}
            <p class="flex items-start gap-2 font-medium text-danger"><Icon name="alert" size={15} class="mt-0.5 shrink-0" />{oauth.error}</p>
          {:else}
            <p>
              Une page Google s'est ouverte dans ton navigateur : choisis le compte de la chaîne et autorise l'accès.
              <span class="text-faint">(attente encore {Math.ceil(oauth.remaining / 60)} min)</span>
            </p>
            <a class="mt-1 inline-flex items-center gap-1 font-medium text-accent hover:underline" href={oauth.auth_url} target="_blank" rel="noreferrer">
              Rouvrir la page Google <Icon name="external" size={12} />
            </a>
          {/if}
          <details class="mt-3 text-muted" open={Boolean(oauth.error)}>
            <summary class="cursor-pointer font-medium text-ink">Google affiche « Erreur 403 : access_denied » ?</summary>
            <ol class="mt-2 list-decimal space-y-1 pl-5">
              <li>Ton projet Google Cloud est en mode <strong>Test</strong> : seuls les comptes listés comme testeurs peuvent se connecter.</li>
              <li>
                Ouvre
                <a class="font-medium text-accent hover:underline" href="https://console.cloud.google.com/auth/audience" target="_blank" rel="noreferrer">Google Auth Platform › Audience</a>,
                section <strong>Utilisateurs test</strong>, et ajoute l'adresse Gmail propriétaire de ta chaîne YouTube.
              </li>
              <li>Clique sur Annuler ici, puis sur « Connecter une chaîne YouTube ».</li>
            </ol>
            <p class="mt-2">« Cette application n'a pas été validée par Google » : clique sur Paramètres avancés, puis « Accéder à AutoClip ». C'est normal pour ton propre projet.</p>
          </details>
          {#if oauth.error}
            <button class="btn btn-sm mt-3" onclick={() => (oauth = null)}>Fermer</button>
          {/if}
        </div>
      {/if}
    </Section>

    {#if status}
      <p class="px-1 text-xs text-faint">AutoClip {status.version} · données : {status.home}</p>
    {/if}
  </div>
</div>
