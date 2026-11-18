<script>
  import { app } from '../state.svelte.js';
  import Icon from './Icon.svelte';

  let confirmButton = $state();

  $effect(() => {
    if (app.confirm && confirmButton) confirmButton.focus();
  });

  function onKey(e) {
    if (app.confirm && e.key === 'Escape') app.confirm.resolve(false);
  }

  const toneIcon = { success: 'check', error: 'alert', info: 'sparkle' };
</script>

<svelte:window onkeydown={onKey} />

{#if app.confirm}
  <div class="fixed inset-0 z-40 grid place-items-center bg-black/40 p-4">
    <div
      class="card w-full max-w-md p-6 shadow-2xl"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
    >
      <h2 id="confirm-title" class="font-serif text-xl">{app.confirm.title}</h2>
      <p class="mt-2 text-muted leading-relaxed">{app.confirm.message}</p>
      <div class="mt-6 flex justify-end gap-2">
        <button class="btn btn-ghost" onclick={() => app.confirm.resolve(false)}>Annuler</button>
        <button
          bind:this={confirmButton}
          class="btn {app.confirm.danger ? 'btn-danger' : 'btn-primary'}"
          style={app.confirm.danger ? 'border-color: var(--danger)' : ''}
          onclick={() => app.confirm.resolve(true)}
        >
          {app.confirm.confirmLabel}
        </button>
      </div>
    </div>
  </div>
{/if}

<div class="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2" aria-live="polite">
  {#each app.toasts as t (t.id)}
    <div
      class="card flex items-start gap-2.5 px-4 py-3 shadow-lg"
      style={t.kind === 'error' ? 'border-color: var(--danger)' : ''}
    >
      <span class={t.kind === 'error' ? 'text-danger' : t.kind === 'success' ? 'text-success' : 'text-accent'}>
        <Icon name={toneIcon[t.kind]} size={16} class="mt-0.5" />
      </span>
      <p class="text-sm leading-snug">{t.message}</p>
    </div>
  {/each}
</div>
