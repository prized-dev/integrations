import { defineConfig } from 'tsdown';

// ESM only, like @ai-hero/sandcastle itself (its exports have no `require` condition).
export default defineConfig({
  entry: ['src/index.ts'],
  format: ['esm'],
  dts: true,
  sourcemap: true,
  clean: true,
  fixedExtension: false,
});
