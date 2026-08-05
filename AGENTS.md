# Internal Upload and Network Check Codex Instructions

## Scope

This file applies to the `사내업로드` repository.

Keep this `AGENTS.md` tracked in Git. It is part of the source handoff so the
same project rules follow GitHub clones, MacBook work, and future Windows
workstations.

## Project Summary

This repository is an internal incident-response file transfer and network
measurement tool. It runs as a Python Flask web app on a Windows PC, stores
uploaded files under a configured storage root, records upload metadata in CSV,
and generates direct browser download links. It also provides bounded quick and
duration-based HTTP measurements, a separate TCP probe client/server flow, and
a read-only operations summary of server health and recent measurement samples.

This is not a general-purpose file transfer, device-management, or incident
ticketing service. Preserve the existing upload and measurement workflows and
keep new behavior small and independently testable.

## Default Workflow

- Inspect `git status --short --branch` before editing or committing.
- Keep changes scoped to this repository.
- Prefer simple CSV and file-system behavior over database-backed features.
- Do not add login, role management, recipient selection, expiry, or admin pages
  unless the user explicitly changes the project scope.
- Keep generated upload files, virtual environments, caches, logs, and private
  operating data out of Git.
- Before any GitHub push or Release work, check whether `README.md`,
  `RELEASE_NOTES.md`, and `CHANGELOG.md` still match the current behavior.
- When a version is ready for sharing, create or update the matching Git tag,
  GitHub Release, and Windows ZIP asset. Keep the Release body aligned with
  `RELEASE_NOTES.md` and `CHANGELOG.md`.

## Important Areas

- `app.py`: Flask routes, config loading, upload/download/delete logic, and CSV
  handling.
- `network_sustained.py`: duration-based HTTP measurement sessions, statistics,
  cancellation, and CSV/JSON result persistence.
- `sustained_excel.py`: in-memory Excel workbook generation for saved sustained
  HTTP measurement results.
- `network_measurement.py`: shared single-measurement gate for HTTP and TCP checks.
- `upload_transactions.py`: durable upload/delete transaction markers and
  startup recovery.
- `measurement_transactions.py`: durable HTTP sustained/TCP result intent
  markers, JSON-to-CSV reconciliation, and fail-closed startup recovery.
- `result_storage.py`: durable temporary-file, fsync, and atomic JSON result writes.
- `runtime_stability.py`: CSV tail recovery, data-directory instance locking,
  rotating diagnostics, and storage health checks.
- `bounded_server.py`: bounded HTTP request workers and inactive-connection
  timeout handling for the portable web server.
- `network_probe/`: TCP protocol, agent, server, statistics, Windows telemetry,
  generated client ZIP, Excel reporting, Flask API, and loopback self-check.
- `probe_client.py`: the dedicated Windows TCP measurement client entrypoint.
- `templates/` and `static/`: the single-page upload UI and network check mode.
- `static/operations_dashboard.js`: read-only health and recent-measurement
  summary rendering. It must not be presented as device inventory or incident
  tracking.
- `tests/`: deterministic tests for upload, download, deletion, paths, links,
  measurement validation, concurrency, fault recovery, UI structure, and CSV
  behavior.
- `docs/PROJECT_DIAGNOSTIC_AND_IMPROVEMENT_PLAN_KO.md`: source-backed Korean
  usability, stability, and phased-improvement assessment.
- `config.ini`: sample/default operational settings. Do not store real secrets.
- `data/upload_log.csv`: tracked initial upload CSV header only; operational
  records should not be treated as source history.
- `data/network_check_log.csv`: tracked initial network-check CSV header only;
  operational speed-test records should not be treated as source history.
- `data/network_check_session_log.csv`: tracked sustained-check CSV header only.
- `data/network_check_results/`: tracked README only; operational JSON results
  must remain untracked.
- `data/network_probe_log.csv`: tracked TCP probe CSV header only.
- `data/network_probe_results/`: tracked README only; operational TCP result
  JSON files must remain untracked.
- `requirements-windows.lock`: hash-pinned Windows release build dependencies.
- `tools/`: Windows Release ZIP build, security artifact, version resource,
  verification helpers, the resource-instrumented Windows stability soak, and
  its contract/anomaly analyzer.
- `.github/workflows/release.yml`: Windows runner workflow that builds and
  uploads the executable ZIP asset.
- `.github/workflows/stability-windows.yml`: weekly 45-minute Windows upload,
  TCP, and restart soak workflow.

## Validation Commands

Use the narrowest relevant check while developing, then run the full baseline
before calling work complete.

```powershell
python -m compileall app_version.py app.py bounded_server.py probe_client.py startup_ports.py runtime_stability.py upload_transactions.py measurement_transactions.py network_sustained.py sustained_excel.py excel_report.py network_measurement.py result_storage.py network_probe tests tools
node --check static/network_check.js
node --check static/network_sustained.js
node --check static/network_probe.js
node --check static/throughput_chart.js
node --check static/operations_dashboard.js
python -m pytest -q
python tools/run_windows_stability_soak.py --duration-minutes 45 --summary-path windows-soak-summary.json
python tools/analyze_windows_soak_summary.py windows-soak-summary.json --minimum-duration-minutes 45 --output windows-soak-analysis.json
```

On macOS in this workspace, use:

```bash
.venv/bin/python -m compileall app_version.py app.py bounded_server.py probe_client.py startup_ports.py runtime_stability.py upload_transactions.py measurement_transactions.py network_sustained.py sustained_excel.py excel_report.py network_measurement.py result_storage.py network_probe tests tools
node --check static/operations_dashboard.js
.venv/bin/python -m pytest -q
.venv/bin/python tools/run_stability_fault_suite.py
.venv/bin/python tools/run_windows_stability_soak.py --duration-minutes 0.01 --max-cycles 1
.venv/bin/python tools/run_windows_stability_soak.py --duration-minutes 45 --summary-path windows-soak-summary.json
.venv/bin/python tools/analyze_windows_soak_summary.py windows-soak-summary.json --minimum-duration-minutes 45 --output windows-soak-analysis.json
```

## README / Release Document Rules

- If features are added, changed, or removed, update `README.md` in the same
  change when the user-facing behavior changes.
- If install, run, setup, port, config keys, storage layout, CSV fields,
  deletion rules, download-link behavior, firewall notes, or limitations change,
  update `README.md` in the same change.
- If a change is release-facing, check `README.md`, `RELEASE_NOTES.md`, and
  `CHANGELOG.md` together.
- Update `CHANGELOG.md` with user-facing changes before pushing to GitHub.
- Keep `RELEASE_NOTES.md` aligned with the current release checklist and any
  future GitHub Release asset contract.
- Record the same user-facing behavior, validation commands, limitations, ZIP
  asset name, and SHA256 policy in `RELEASE_NOTES.md`.
- Do not document features that are not implemented. If a feature is planned but
  not implemented, label it as not implemented.
- Write README steps for users who are not comfortable with GitHub or
  development tooling yet. Prefer numbered, copyable steps over assumed
  background knowledge.
- Use sample values only. Never place real internal IPs, host names, accounts,
  passwords, customer data, uploaded files, private notes, or raw operational
  logs in README, release notes, changelog entries, commits, or final reports.

## Safety Rules

- Keep the app limited to internal trusted-network use.
- Treat upload data and memo text as operational data.
- Do not commit uploaded files or populated CSV records.
- Keep deletion behavior restricted by configured allowed IPs unless the user
  explicitly changes that policy.
- Keep server and TCP client entrypoints separate in Windows releases.
- Read a measurement result JSON only once per download request. Keep file
  access, JSON validation, and sustained-result IP ownership in one helper, and
  map read, encoding, or JSON failures to a sanitized domain error without raw
  paths or tracebacks. Preserve existing missing-result 404 and sustained
  cross-IP 403 behavior.
- Do not add automatic firewall changes, PowerShell launch helpers, persistence,
  privilege elevation, or uploaded-file execution.
- Keep direct executable, script, macro-document, and disk-image uploads blocked.
- Preserve release security artifacts and clearly document the residual risks:
  unsigned binaries, unauthenticated intranet access, unlimited file size,
  uninspected archive contents, and the TCP client's long polling.
