<script>
  import { untrack } from 'svelte';
  import { api } from '../api.js';
  import { fmtDate } from '../format.js';
  import Icon from './Icon.svelte';
  import Section from './Section.svelte';

  let { bot, tick } = $props();

  const DAYS = 14;
  let stats = $state(null);
  let width = $state(640);
  let hovered = $state(null);

  $effect(() => {
    tick;
    untrack(async () => {
      try {
        stats = await api.get(`/bots/${bot.id}/stats?days=${DAYS}`);
      } catch {
        /* réessai au prochain rafraîchissement */
      }
    });
  });

  const tiles = $derived(
    stats && [
      { label: 'À valider', value: stats.clips.review ?? 0 },
      { label: 'Planifiés', value: (stats.clips.scheduled ?? 0) + (stats.clips.publishing ?? 0) },
      { label: 'Publiés', value: stats.clips.published ?? 0 },
      { label: 'Rejetés', value: stats.clips.rejected ?? 0 },
      { label: 'Échecs', value: stats.clips.failed ?? 0, alert: (stats.clips.failed ?? 0) > 0 },
    ],
  );

  // -- graphique : publications par jour (une seule série, pas de légende) -----
  const H = 200;
  const PAD = { top: 12, right: 8, bottom: 26, left: 30 };
  const dayLabel = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short' });
  const dayLong = new Intl.DateTimeFormat('fr-FR', { weekday: 'long', day: 'numeric', month: 'long' });

  const chart = $derived.by(() => {
    if (!stats) return null;
    const data = stats.published_per_day.map((d) => ({ ...d, date: new Date(`${d.day}T12:00:00`) }));
    const max = Math.max(...data.map((d) => d.count), 0);
    const step = max <= 4 ? 1 : Math.ceil(max / 4);
    const top = Math.max(step * Math.ceil(max / step), 1);
    const ticks = [];
    for (let v = 0; v <= top; v += step) ticks.push(v);
    const plotW = Math.max(width - PAD.left - PAD.right, 100);
    const plotH = H - PAD.top - PAD.bottom;
    const slot = plotW / data.length;
    const barW = Math.max(Math.min(28, slot * 0.62), 4);
    const y = (v) => PAD.top + plotH - (v / top) * plotH;
    const bars = data.map((d, i) => {
      const x = PAD.left + i * slot + (slot - barW) / 2;
      const yTop = y(d.count);
      const h = PAD.top + plotH - yTop;
      const r = Math.min(4, barW / 2, h);
      // Bord supérieur arrondi, base posée sur l'axe.
      const path = h > 0
        ? `M${x},${yTop + h}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + barW - r}Q${x + barW},${yTop} ${x + barW},${yTop + r}V${yTop + h}Z`
        : '';
      return { ...d, i, x, slotX: PAD.left + i * slot, slot, path, cx: x + barW / 2, yTop };
    });
    return { bars, ticks, y, plotH, total: data.reduce((n, d) => n + d.count, 0) };
  });
</script>

{#if !stats}
  <div class="grid h-40 place-items-center text-muted"><span class="spinner"></span></div>
{:else}
  <div class="space-y-5">
    <div class="grid grid-cols-2 gap-3 md:grid-cols-5">
      {#each tiles as t}
        <div class="card px-5 py-4">
          <p class="text-sm text-muted">{t.label}</p>
          <p class="mt-1 font-serif text-3xl tabular-nums {t.alert ? 'text-danger' : ''}">{t.value}</p>
        </div>
      {/each}
    </div>

    <div class="card flex items-center gap-4 px-5 py-4">
      <div class="grid h-9 w-9 place-items-center rounded-full bg-accent-soft text-accent">
        <Icon name="clock" size={17} />
      </div>
      <div>
        <p class="text-sm text-muted">Prochaine publication</p>
        <p class="font-medium">
          {stats.next_publication ? fmtDate(stats.next_publication) : 'Aucune programmée'}
        </p>
      </div>
      <p class="ml-auto text-sm text-muted">{stats.sources} vidéo{stats.sources > 1 ? 's' : ''} source{stats.sources > 1 ? 's' : ''}</p>
    </div>

    <Section
      title="Publications par jour"
      description="{chart.total} publication{chart.total > 1 ? 's' : ''} sur les {DAYS} derniers jours{bot.dry_run ? ' (simulées en dry run)' : ''}"
    >
      <div class="relative" bind:clientWidth={width}>
        <svg {width} height={H} role="img" aria-label="Publications par jour sur les {DAYS} derniers jours">
          {#each chart.ticks as v}
            <line x1={PAD.left} x2={width - PAD.right} y1={chart.y(v)} y2={chart.y(v)} stroke="var(--chart-grid)" stroke-width="1" />
            <text x={PAD.left - 8} y={chart.y(v)} dy="0.32em" text-anchor="end" font-size="11" fill="var(--faint)" class="tabular-nums">{v}</text>
          {/each}
          {#each chart.bars as b}
            {#if hovered === b.i}
              <rect x={b.slotX} y={PAD.top} width={b.slot} height={chart.plotH} fill="var(--surface-2)" rx="6" />
            {/if}
            {#if b.path}<path d={b.path} fill="var(--chart-bar)" />{/if}
            {#if b.i % 2 === (DAYS - 1) % 2}
              <text x={b.cx} y={H - 8} text-anchor="middle" font-size="11" fill="var(--faint)">{dayLabel.format(b.date)}</text>
            {/if}
            <!-- zone de survol plus large que la barre -->
            <rect
              role="presentation"
              x={b.slotX}
              y={PAD.top}
              width={b.slot}
              height={chart.plotH}
              fill="transparent"
              onmouseenter={() => (hovered = b.i)}
              onmouseleave={() => (hovered = null)}
            />
          {/each}
        </svg>
        {#if hovered !== null}
          {@const b = chart.bars[hovered]}
          <div
            class="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded-lg border border-line bg-surface px-3 py-2 text-sm shadow-lg"
            style="left:{b.cx}px; top:{Math.min(b.yTop, PAD.top + chart.plotH) - 8}px"
          >
            <p class="text-xs text-muted first-letter:uppercase">{dayLong.format(b.date)}</p>
            <p class="font-medium tabular-nums">{b.count} publication{b.count > 1 ? 's' : ''}</p>
          </div>
        {/if}
      </div>
      <details class="mt-3 text-sm">
        <summary class="cursor-pointer text-muted hover:text-ink">Voir les données</summary>
        <table class="mt-2 w-full max-w-sm text-left">
          <thead><tr class="text-muted"><th class="py-1 font-medium">Jour</th><th class="py-1 text-right font-medium">Publications</th></tr></thead>
          <tbody>
            {#each chart.bars as b}
              <tr class="border-t border-line"><td class="py-1 first-letter:uppercase">{dayLong.format(b.date)}</td><td class="py-1 text-right tabular-nums">{b.count}</td></tr>
            {/each}
          </tbody>
        </table>
      </details>
    </Section>
  </div>
{/if}
