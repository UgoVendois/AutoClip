<script>
  import { api } from '../api.js';

  // Champ libre + suggestions : la liste vient de l'API du fournisseur (clé enregistrée),
  // car les noms de modèles changent souvent.
  let { id, provider, kind = 'text', value = null, defaultModel = null, onchange } = $props();

  let models = $state([]);
  let error = $state('');
  let loading = $state(false);
  const listId = $derived(`${id}-models`);
  const remote = $derived(!['svg', 'fake', 'none'].includes(provider));

  $effect(() => {
    const p = provider;
    models = [];
    error = '';
    if (!remote) return;
    loading = true;
    api
      .get(`/ai/models/${p}?kind=${kind}`)
      .then((data) => {
        if (p === provider) models = data.models;
      })
      .catch((err) => {
        if (p === provider) error = err.message;
      })
      .finally(() => (loading = false));
  });
</script>

{#if remote}
  <input
    {id}
    class="input"
    list={listId}
    placeholder={defaultModel ? `Par défaut : ${defaultModel}` : 'Modèle par défaut'}
    value={value ?? ''}
    onchange={(e) => onchange(e.target.value.trim() || null)}
  />
  <datalist id={listId}>
    {#each models as m}<option value={m}></option>{/each}
  </datalist>
  {#if loading}
    <p class="hint">Chargement des modèles disponibles…</p>
  {:else if error}
    <p class="hint">Liste indisponible ({error}). Tu peux saisir le nom du modèle à la main.</p>
  {:else if models.length}
    <p class="hint">{models.length} modèle{models.length > 1 ? 's' : ''} disponible{models.length > 1 ? 's' : ''} avec ta clé.</p>
  {/if}
{:else}
  <input {id} class="input" disabled value={provider === 'svg' ? 'Même modèle que le texte' : '—'} />
{/if}
