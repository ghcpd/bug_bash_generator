import { defineConfig } from 'vite';

export default defineConfig(({ mode }) => ({
  root: '.',
  base: mode === 'production' ? '/caseGenerator/' : '/',
  build: {
    outDir: 'dist',
  },
}));
