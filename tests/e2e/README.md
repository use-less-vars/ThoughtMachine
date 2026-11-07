# End-to-end browser tests (`tests/e2e`)

Playwright-driven tests that exercise the real web UI: a Vite dev server
(frontend) proxying to a real FastAPI backend subprocess (backend), driven
by a headless Chromium via `pytest-playwright`.

## Scope

These tests are **skipped by default**. They are marked `e2e` and only run
when pytest is invoked with `--run-e2e`, so a normal `pytest` run (CI, dev)
never needs a browser installation:

```bash
pytest tests/e2e                      # collects, all skipped
pytest --run-e2e tests/e2e            # actually runs the browser tests
```

## Prerequisites

* Python dependencies:
  ```bash
  pip install -r requirements-dev.txt   # adds playwright + pytest-playwright
  playwright install chromium            # downloads the browser binaries
  ```
* Node.js for the Vite dev server (the frontend's `node_modules` must be
  installed: `npm install` in `web_ui/frontend`).  `conftest.py` looks for
  `node` on `PATH` and falls back to `node-bin/bin/node` or
  `.tm-node/*/bin/node` inside the repo.

## Environment variables

Everything path-dependent is read from the environment (nothing is
hardcoded in `conftest.py`):

| Variable                  | Purpose                                        |
|---------------------------|------------------------------------------------|
| `PLAYWRIGHT_BROWSERS_PATH`| Where Playwright browsers are installed        |
| `LD_LIBRARY_PATH`         | Shared libraries for Chromium (containers)     |
| `FONTCONFIG_FILE`         | Font config for headless Chromium (containers) |
| `VITE_BACKEND_PORT`       | Optional; set automatically by the fixture     |

The dev container sets these to `/workspace/.ms-playwright`,
`/workspace/chrome-libs/usr/lib/x86_64-linux-gnu` and
`/workspace/.deb-cache/fonts.conf` respectively, with Node.js on `PATH`.

## Fixtures (`conftest.py`)

* `e2e_backend` (session): boots `python -m web_ui.backend.server` as a
  subprocess with an isolated temp vault (`HOME` +
  `THOUGHTMACHINE_VAULT_ROOT`), a free port, and API-key env vars removed.
  Waits for `GET /health` before yielding.
* `e2e_frontend` (session): boots the Vite dev server (`--host 127.0.0.1`)
  with `VITE_BACKEND_PORT` pointing at the backend.  Waits for HTTP 200.
* `workspace` (session): creates a `purpose=research` workspace inside the
  vault via `POST /api/workspace`.
* `restart_backend` (function): kills the backend and boots a fresh
  subprocess on the same vault and port (the Vite proxy keeps working), then
  waits for `/health`.
* `browser_type_launch_args` (session): adds `--no-sandbox` and
  `--disable-dev-shm-usage` so Chromium runs headless in containers/CI.

## Adding a test

* Mark the module/test with `@pytest.mark.e2e` so default runs skip it.
* Import Playwright lazily: `pytest.importorskip("playwright.sync_api")` at
  module level keeps collection working on machines without Playwright.
* Use the `page` fixture from `pytest-playwright` plus the fixtures above.
