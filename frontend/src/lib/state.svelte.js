// État global de l'interface (runes Svelte 5).
import { api } from './api.js';

export const app = $state({
  bots: [],
  selectedId: null,
  view: 'bot', // 'bot' | 'settings'
  tab: 'config', // 'config' | 'discovery' | 'queue' | 'channel' | 'stats'
  settings: null,
  inWindow: false, // true dans la fenêtre pywebview (sélecteur de fichiers natif)
  toasts: [],
  confirm: null,
});

// -- notifications -------------------------------------------------------------
let toastId = 0;

export function toast(message, kind = 'info') {
  const id = ++toastId;
  app.toasts.push({ id, message, kind });
  setTimeout(() => {
    app.toasts = app.toasts.filter((t) => t.id !== id);
  }, kind === 'error' ? 7000 : 3200);
}

/** Exécute un appel d'API ; affiche l'erreur et renvoie null en cas d'échec. */
export async function run(promise, successMessage) {
  try {
    const result = await promise;
    if (successMessage) toast(successMessage, 'success');
    return result ?? true;
  } catch (err) {
    toast(err.message, 'error');
    return null;
  }
}

// -- confirmation ---------------------------------------------------------------
export function askConfirm({ title, message, confirmLabel = 'Confirmer', danger = false }) {
  return new Promise((resolve) => {
    app.confirm = {
      title,
      message,
      confirmLabel,
      danger,
      resolve: (value) => {
        app.confirm = null;
        resolve(value);
      },
    };
  });
}

// -- données -----------------------------------------------------------------------
export async function refreshBots() {
  app.bots = await api.get('/bots');
  if (app.selectedId != null && !app.bots.some((b) => b.id === app.selectedId)) {
    app.selectedId = app.bots[0]?.id ?? null;
  }
  if (app.selectedId == null && app.bots.length) app.selectedId = app.bots[0].id;
}

export async function loadSettings() {
  app.settings = await api.get('/settings');
  applyTheme();
}

// -- thème ------------------------------------------------------------------------
const media = window.matchMedia('(prefers-color-scheme: dark)');

export function applyTheme() {
  const theme = app.settings?.ui_theme ?? 'system';
  const dark = theme === 'dark' || (theme === 'system' && media.matches);
  document.documentElement.classList.toggle('dark', dark);
}

media.addEventListener('change', applyTheme);

export async function setTheme(theme) {
  app.settings.ui_theme = theme;
  applyTheme();
  await run(api.put('/settings', { ui_theme: theme }));
}

export function selectBot(id, tab) {
  app.selectedId = id;
  app.view = 'bot';
  if (tab) app.tab = tab;
}
