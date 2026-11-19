// Libellés et formats affichés à l'utilisateur.

export const CLIP_STATUS = {
  render: { label: 'Rendu en cours', tone: 'neutral' },
  review: { label: 'À valider', tone: 'accent' },
  rejected: { label: 'Rejeté', tone: 'neutral' },
  scheduled: { label: 'Planifié', tone: 'warning' },
  publishing: { label: 'Publication…', tone: 'warning' },
  published: { label: 'Publié', tone: 'success' },
  failed: { label: 'Échec', tone: 'danger' },
};

export const SOURCE_STATUS = {
  ingest: { label: 'Téléchargement', tone: 'warning', busy: true },
  transcribe: { label: 'Transcription', tone: 'warning', busy: true },
  analyze: { label: 'Analyse', tone: 'warning', busy: true },
  done: { label: 'Terminé', tone: 'success' },
  failed: { label: 'Échec', tone: 'danger' },
  cancelled: { label: 'Annulé', tone: 'neutral' },
};

export const PUBLISH_STATUS = {
  pending: { label: 'Programmée', tone: 'neutral' },
  queued: { label: 'En file', tone: 'warning', busy: true },
  publishing: { label: 'Envoi…', tone: 'warning', busy: true },
  published: { label: 'Publiée', tone: 'success' },
  failed: { label: 'Échec', tone: 'danger' },
};

export const JOB_KIND = {
  ingest: 'Téléchargement',
  transcribe: 'Transcription',
  analyze: 'Analyse',
  render: 'Rendu',
  publish: 'Publication',
  discover: 'Découverte',
  presort: 'Tri des moments forts',
  prepare_window: "Préparation d'un extrait",
  ai_score: 'Notation IA',
  identity_generate: 'Identité : proposition',
  identity_images: 'Identité : visuels',
  identity_apply: 'Identité : application',
  playlist_add: 'Ajout en playlist',
};

const dateFmt = new Intl.DateTimeFormat('fr-FR', {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
});

export function fmtDate(ts) {
  return ts ? dateFmt.format(new Date(ts * 1000)) : '—';
}

export function fmtClock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

export function fmtDuration(ms) {
  return `${Math.round(ms / 1000)} s`;
}

export function fileName(path) {
  return path.split(/[\\/]/).pop();
}

// Temps restant estimé par le démon (secondes) -> « ≈ 12 min ».
export function fmtEta(seconds) {
  if (seconds == null) return '';
  if (seconds < 60) return '< 1 min';
  const min = Math.round(seconds / 60);
  if (min < 60) return `≈ ${min} min`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m ? `≈ ${h} h ${String(m).padStart(2, '0')}` : `≈ ${h} h`;
}
