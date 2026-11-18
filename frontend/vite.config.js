import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';

// `npm run build` écrit l'interface dans autoclip/web/, servie par le démon.
// `npm run dev` : rechargement à chaud, l'API est relayée vers le démon local.
export default defineConfig({
  plugins: [tailwindcss(), svelte()],
  build: { outDir: '../autoclip/web', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
});
