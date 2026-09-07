import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: resolve(__dirname, 'src/webui/dist'),
    emptyOutDir: false,
    rollupOptions: {
      input: resolve(__dirname, 'src/ohmymeme/presentation/frontend/main/app/main.ts'),
      output: {
        entryFileNames: 'ohmymeme.js',
        inlineDynamicImports: true,
        format: 'iife',
      },
    },
    minify: 'esbuild',
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    include: ['tests/frontend/**/*.test.{js,ts}'],
  },
})
