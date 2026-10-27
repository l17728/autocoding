# Repository Guidelines

This is an implemented Windows-first local development collaboration platform. Treat the source and current tests as authoritative; design documents are proposals unless `docs/implementation-status.md` records implementation evidence.

## Project structure

- `src/autocoding/`: Python service, adapters, PM workflow, persistence, diagnostics, and project isolation.
- `ui/src/`: React and TypeScript web application.
- `tests/`: Python unit and integration tests.
- `ui/e2e/`: browser end-to-end tests.
- `tools/`: local service lifecycle, probes, workload tools, and reporting scripts.
- `docs/`: architecture, operator guidance, and dated implementation evidence.

## Build and run

Use the project virtual environment on Windows:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd ui
npm.cmd run build
npx.cmd playwright test
cd ..
.\tools\start-local.ps1
```

Stop the local service with `tools\stop-local.ps1`. Do not restart it while any registered project has an active run, in-flight command, or reconciliation in progress. Inspect project snapshots and liveness first.

## Conventions

- Preserve UTF-8 and existing Chinese product terminology.
- Keep public diagnostics bounded and redact credentials, prompts, and provider frame values.
- Persist task state and evidence before reporting completion. A provider response is not acceptance; independent checks are authoritative.
- Keep project data isolated by project ID and avoid modifying hosted project directories while working on the platform.
- Add behavior tests for code changes and update `docs/implementation-status.md` with dated evidence and remaining limitations.

## Verification boundaries

Unit tests and builds do not establish long-running production reliability or real-provider availability. Record command, exit code, and fresh output for every claimed verification. Keep real CLI/provider acceptance separate from simulated protocol tests.
