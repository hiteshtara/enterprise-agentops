# AgentGuard console

React + TypeScript console for AgentGuard. See the [project README](../README.md)
for architecture, the demo flow, and screenshots.

```bash
npm install
npm run dev          # http://localhost:5173 (API must be running on :8000)
npm run test         # vitest; the API module is mocked
npm run typecheck
npm run lint
npm run build
```

Set `VITE_API_BASE_URL` to point at a different API (see `.env.example`).
