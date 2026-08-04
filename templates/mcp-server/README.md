# MCP server template

A starting point for making a new product's tools available as a
first-class MCP server — stdio for local Claude Desktop/Code, or a
loopback streamable-HTTP server behind a tunnel you run yourself. The
shape is lifted directly from `dhk/wingman`'s `mcp_server.py`, which has
been running in both modes in production; this template exists so the
next product doesn't reinvent that plumbing from an empty FastMCP app.

See [dhk/alexandria#7](https://github.com/dhk/alexandria/issues/7) for the
plan to eventually extract this into a real shared package instead of a
copy-paste template. Until that lands, forking this directory is the
supported path.

## What's included

| File | What it does |
|---|---|
| `src/example_service/mcp_server.py` | FastMCP app: stdio by default, `--http` for streamable HTTP with capability-token auth, DNS-rebinding-safe transport security, pidfile-based "already running" detection, ready-to-paste connector URLs. |
| `src/example_service/infrastructure/config.py` | Local-state directory resolution: an env var override, else the platform user data dir. |
| `src/example_service/infrastructure/keys.py` | Provider-key resolution ladder: environment → host key file (`~/.config/<service>.env`, the "wingman.env" pattern — one file every consumer on a host reads) → workspace key file. |
| `src/example_service/infrastructure/mcp_process.py` | Pidfile write/read/stop, so a control script or systemd unit can answer "is it running" without guessing. |
| `src/example_service/admin.py` | A token-gated, cross-instance "installations" dashboard: name, version, running/stopped, and a link into each instance's own surface — read-only and launcher-only, on purpose. |
| `docs/SERVER.md` | Deployment guide: systemd units, the host key file, tunneling, the admin page. |
| `tests/unit/` | Smoke tests for the token/config/key-resolution logic above. |

Deliberately **not** included: any actual product tools (the whole point
is that those are yours), a CLI (add one if the product needs one — the
MCP server does not require it), and the macOS Keychain integration
wingman's `keys.py` has (add it back if the forked project needs it; the
file ladder here is the part that matters on a shared Linux host).

## Forking this for a new project

1. Copy this directory out of Alexandria into the new project's repo (or
   just copy the files if the new project already exists).
2. Rename the package: `src/example_service/` → `src/<your_package>/`,
   and update every `example_service` import and `EXAMPLE_SERVICE_*` /
   `APP_NAME` / `ENV_DATA_DIR` constant to match. A single
   case-sensitive find-and-replace of `example_service` → `your_package`
   and `EXAMPLE_SERVICE` → `YOUR_PACKAGE` covers nearly everything.
3. Update `pyproject.toml`: `name`, `[project.scripts]` entry point.
4. Replace the `status` / `example_tool` placeholder tools in
   `mcp_server.py` with real ones — same `@server.tool()` pattern.
5. Fill in `.env.example` with the real provider keys the new tools need,
   and update `KNOWN_KEYS` in `infrastructure/keys.py` to match.
6. Follow `docs/SERVER.md` to run it as a systemd service, once there's
   something worth keeping always-on.

## Design notes carried over from wingman

- **The capability token IS the auth.** There's no separate login step —
  whoever holds the `/mcp/<token>` URL can use the service. Loopback bind
  plus a tunnel you control (Tailscale serve/funnel, or your own reverse
  proxy) is the trust boundary; rotate the token to revoke a leaked URL.
- **Provider secrets never flow through the MCP transport.** Setting a key
  is a host/terminal act (the key file ladder), never an MCP tool — a
  key-setting tool would route the secret through the model conversation,
  transcript, and provider logs, which is exactly where a credential must
  not travel.
- **The admin page is read-only and stays that way.** It shows whether an
  instance is up and links into it; it never mutates another instance's
  process. Any self-service action (e.g. restart) belongs on the
  instance's own surface, gated by that instance's own token.
