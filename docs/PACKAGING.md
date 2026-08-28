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

## Pack manager

The first pack installed from this release bootstraps a root-owned generic
manager at `/usr/local/bin/tool-pack-manager`. It is a Lobster-side inbox for
deployment packs: copy an archive and its `.sha256` into one directory and run
it there.

```bash
tool-pack-manager              # numbered inventory of the current directory
tool-pack-manager -C ~/incoming
tool-pack-manager list
tool-pack-manager --json list
tool-pack-manager run 1
tool-pack-manager delete 1
```

`run` reads the manifest without extracting, verifies the sidecar, rejects
absolute paths, traversal, links, devices, and archives over the unpacked
safety limit, then extracts atomically and invokes that pack's installer from
the transfer directory. A missing checksum requires the explicit
`run --allow-unverified <selector>` form; a malformed or mismatched checksum is
always refused.

`delete` is recoverable quarantine, not removal. It moves only the selected
archive, checksum, and matching unpacked transfer directory under
`~/.local/share/tool-pack-manager/trash/`, alongside a `restore.json` recording
their original paths. Installed releases, rollback copies, application data,
secrets, service units, and registry entries are never deletion targets. There
is no permanent purge; that stays a separate, explicit operator decision.

An earlier managed manager is backed up before replacement; an unknown
executable at that path is refused non-interactively.

## Sensitive state

Archives exclude Git data, virtual environments, build output, application data,
and run records. Never add secrets, capability tokens, private research inputs,
or machine-specific values to `deploy/pack.toml` or documentation. Keep concrete
hostnames, usernames, and routes in local configuration.
