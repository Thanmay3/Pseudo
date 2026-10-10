import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/github': 'http://127.0.0.1:8000',
      '/profiles': 'http://127.0.0.1:8000',
      '/auth': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/projects': 'http://127.0.0.1:8000',
      '/sessions': 'http://127.0.0.1:8000',
    },
  },
});
