# SWE-bench Case Generator

Browser-based control panel for generating, monitoring, and auditing SWE-bench benchmark cases at scale using Azure Data Factory, Azure Batch, and Blob Storage.

## Features

- **Configure** — Define target repos, categories, difficulty levels, and case counts. Trigger ADF pipelines directly from the UI.
- **Monitor** — Unified pipeline run list across generate and audit pipelines with filter/sort/search. One-click **Open Batch** shows all active tasks in the selected pool. Full Batch Explorer with paginated job/task listing, search by task ID, stdout/stderr log viewer.
- **Results** — Browse and preview JSONL output files from Blob Storage.
- **Audit** — Select generated cases for quality audit (quick in-browser or pipeline-based).
- **Bug Bash** — Manage rubric generation, auto-run pipelines, and export prompts.

## Auth

Uses MSAL (Azure AD) for token acquisition. Supports three scopes: Azure Management, Blob Storage, and Azure Batch. Tokens can also be imported from clipboard via a PowerShell helper script.

## Getting Started

```bash
npm install
npm run dev
```

Open `http://localhost:5173` in the browser, fill in Azure settings, authenticate, and start generating cases.

## Build

```bash
npm run build    # outputs to dist/
npm run preview  # preview production build
```

## Project Structure

```
index.html          Main HTML (all tab panels)
src/
  main.js           Entry point, window._app bindings
  auth.js           MSAL authentication
  azure.js          Azure REST helpers (ADF, Blob, Batch)
  monitor.js        Unified pipeline run monitoring
  batch.js          Batch Explorer (jobs, tasks, search)
  generate.js       Plan generation & pipeline trigger
  results.js        Blob storage file browser
  audit.js          Case audit workflows
  bugbash.js        Bug bash management
  tasks.js          Task table UI
  modal.js          Log viewer modal
  persistence.js    LocalStorage state save/restore
  constants.js      Shared constants
  utils.js          Shared utilities
  styles/main.css   All styles
```
