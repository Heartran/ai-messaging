# AGENTS.md — working in this sandbox (Base44)

Notes for whoever picks this repository up in the Base44 sandbox. Only the
non-obvious: what the stack is, why it is wired this way, and how to check it.

## What runs here

`docker-compose.base44.yml` runs **one** service: the central server
(`server/`), from the cloned source, with uvicorn `--reload`. Its web UI is on
**host port 3000** (`/ui`; `/` redirects there) — that is the sandbox preview,
and the only previewable component. The other two components are not web apps
and are not started:

- `mcp/` is the `aim-mcp` **stdio** MCP server that runs next to each agent
  (no port, no page).
- `desktop/` is the **Windows-only** tray/notification shell (pywebview +
  Windows toasts) that loads the server's `/ui`; it cannot run on Linux.

## Why the loopback bind and the port bridge

The server's security model is enforced in code: `AIM_HOST` must be a literal
address inside the Tailscale ranges, `0.0.0.0` is refused, and loopback is
allowed only with `AIM_ALLOW_LOOPBACK=1` (`server/aim_server/config.py`). This
sandbox has no tailnet, so the container uses the project's own documented
dev/test switch: `AIM_HOST=127.0.0.1` with `AIM_ALLOW_LOOPBACK=1`.

A loopback bind is unreachable through a Docker port mapping, so
`.base44/base44_dev_server.py` (sandbox-only glue, not app code) starts
uvicorn with `--reload` **and** forwards `0.0.0.0:3000` → `127.0.0.1:8422`
over plain TCP. The preview proxy therefore reaches the app unchanged, and
nothing in the app is relaxed: config validation, `X-AIM-Token` enforcement
and the `/admin/*` operator gate behave exactly as in production.

`AIM_ALLOW_LOOPBACK` is set in the Base44 compose file only — it is never
written into `.env`, `.env.example` or any shared default.

## Database

SQLite in the named volume `aim-data` (`AIM_DB_PATH=/data/aim.db`), **not** in
the repo's `data/` directory. It survives `docker compose down`; to wipe it:
`docker compose -f docker-compose.base44.yml down -v` (destroys all chats and
participants).

## Editing loop

The install is editable (`pip install -e .` runs on container start), so a
`git pull` or an edit under `server/` takes effect without a rebuild — the
README's "after updating the repo, reinstall" warning is already handled.
uvicorn `--reload` watches `/app/server` (stat polling) and restarts the app
process on change; the UI file `server/aim_server/static/ui.html` is read at
app creation, so UI edits need that reload too. The port bridge is a separate
thread in the launcher and is unaffected by reloads.

## How to verify

```bash
curl -s localhost:3000/health      # {"status":"ok","version":"0.15.1",...}
curl -s -o /dev/null -w '%{http_code}\n' localhost:3000/ui   # 200
```

Health end to end is an API flow: `POST /register` (returns
`participant_token`) → `POST /chats` (with `X-AIM-Token`) → `POST
/chats/{id}/messages` → `GET /chats/{id}/messages`. A write without the token
must answer `401 token_missing`.

The project's own suites run inside the container (pytest is not installed by
the compose command):

```bash
docker compose -f docker-compose.base44.yml exec -T server \
  sh -ec 'pip install -e .[dev] && python -m pytest -q'      # server suite
```

## Credentials

None are required to run or preview: the app needs no external service, and
the container needs no secret to boot. Two optional knobs:

- `AIM_OPERATOR_KEY` (min 12 chars) unlocks hand-editing of stored metadata
  from the web UI's Settings and the `/admin/*` endpoints. Unset here, so
  `GET /health` reports `editing_enabled: false` by design. To turn it on, add
  it to the `server` service's environment in `.base44` compose and recreate.
- `AIM_RETENTION_DAYS` is unset → all messages are kept forever.
