# Packaging and deployment

Minority Report owns the packer and standard-library installer in `scripts/` and
`deploy/`.

## Build and inspect

```bash
uv run --frozen python scripts/pack.py
python dist/<unpacked-bundle>/install.py --dry-run \
  --repo-path /absolute/path/to/alexandria
```

The packer writes a versioned archive and SHA-256 sidecar under `dist/`. Verify
the sidecar before extracting on a target host. `--repo-path` is mandatory when
configuration does not already name the live Alexandria corpus checkout; it must
not be the install root or Minority Report checkout.

## Install and validate

From the directory containing the unpacked bundle:

```bash
./<bundle>/install.py --repo-path /absolute/path/to/alexandria
./<bundle>/install.py --check
```

Use `--skip-service` for a tool-only install. `--yes` accepts safe defaults but
does not authorize adoption of unknown non-empty directories. The installer
preserves existing secrets and refuses ambiguous destructive changes. On failure
it reports what was and was not rolled back; service-registry reservations are
durable and are not silently released.

`launch-docs.py --no-browser --port 8000` serves the bundled support docs on
loopback. Use an SSH port forward rather than a public bind on a headless host.
The component front panel and `--check` are no-spend installation checks; they do
not call providers or prove an end-to-end research run.

## Sensitive state

Archives exclude Git data, virtual environments, build output, application data,
and run records. Never add secrets, capability tokens, private research inputs,
or machine-specific values to `deploy/pack.toml` or documentation. Keep concrete
hostnames, usernames, and routes in local configuration.
