# Running the forked service as an always-on server

Adapted from wingman's `docs/SERVER.md` (see the services-repo issue for
the plan to stop copy-pasting this per project). Replace `<service>`
throughout with the forked package's real name (matching `APP_NAME` in
`infrastructure/config.py`).

## 1. Install

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, if not present
git clone <your-repo-url> ~/<service>
cd ~/<service> && uv tool install .
<service>-mcp --version
```

## 2. Keys (the host file — the "wingman.env" pattern)

One canonical file per host, mode 600, read directly by every consumer
(CLI and MCP server alike) with zero shell exports needed:

```bash
install -m 600 /dev/null ~/.config/<service>.env
cat >> ~/.config/<service>.env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
EOF
```

Resolution order: **environment > `~/.config/<service>.env` > the
service's own workspace `keys.env`.** A systemd `EnvironmentFile=` or a
shell export still works exactly as before — either just becomes the
"environment" source, which always wins.

## 3. The MCP server as a systemd service

`~/.config/systemd/user/<service>-mcp.service`:

```ini
[Unit]
Description=<service> MCP server (streamable HTTP)
After=network.target

[Service]
EnvironmentFile=-%h/.config/<service>.env
ExecStart=%h/.local/bin/<service>-mcp --http
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now <service>-mcp.service
sudo loginctl enable-linger "$USER"   # services survive logout/reboot
```

This serves `http://127.0.0.1:8787/mcp/<token>` — loopback only, by
design. The token lives in `<data-dir>/mcp-http-token` (0600); it is the
credential, treat it like one. `<service>-mcp --http --rotate-token`
mints a new one (then restart the service and update any connector).

## 4. Reach it remotely via your own tunnel

```bash
sudo tailscale serve --bg 8787     # HTTPS inside your tailnet only
# or, only if you need it reachable off-tailnet:
sudo tailscale funnel --bg 8787    # public HTTPS, guarded by the token
```

The server keeps DNS-rebinding protection on and must therefore accept
the tunnel's Host header: pass `--allowed-host your.tunnel.example`
(repeatable) or set `<APP_NAME>_ALLOWED_HOSTS=a.example,b.example` in the
host key file.

In claude.ai: Settings → Connectors → Add custom connector → paste
`https://your.tunnel.example/mcp/<token>`.

## 5. Multiple instances: the admin installations page

`<data-dir>/installations.toml`:

```toml
[[instance]]
name = "prod"
port = 8787
token = "<its mcp-http-token>"
tunnel_host = "prod.example.ts.net"

[[instance]]
name = "staging"
port = 8788
token = "<its mcp-http-token>"
tunnel_host = "staging.example.ts.net"
```

Then `GET /admin/<installations-token>/installations` on whichever
instance is serving the page shows every configured instance's name,
version, running/stopped state, and a link into its own surface. The
installations token is separate from any single instance's own capability
token — generate one with `admin_token(config, rotate=True)` from a
Python shell, or add a small CLI command for it in the forked project.

## 6. Care and feeding

- **Updates:** `cd ~/<service> && git pull && uv tool install --reinstall .`
  then `systemctl --user restart <service>-mcp.service`.
- **Health:** `GET /health` (unauthenticated, minimal) is what the admin
  page polls — keep it that way; do not add workspace data to it.
