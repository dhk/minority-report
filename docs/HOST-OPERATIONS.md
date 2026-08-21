# Host operations and service registry

`deploy/pack.toml` declares the Minority Report service, loopback endpoint,
external route shape, health check, and registry entry. The normative registry
shape is `schemas/service-registry.schema.json` in this repository.

## Service contract

The default pack installs `alexandria-mcp.service`, binds the application to
loopback, and configures its external path through the declared tunnel mechanism.
Treat the actual hostname, capability token, usernames, and local host inventory
as private configuration; do not copy them into public examples.

Use the installed front panel, `install.py --check`, `alexandria-ctl status`, and
the registry's `check`/`reconcile` commands to distinguish declared, installed,
healthy, and stale state. A passing health endpoint is not evidence that provider
credentials, spend controls, corpus promotion, or publication work.

## Upgrading a host

```
alexandria-ctl upgrade
```

That is the whole procedure. It builds the current `main` of *this* repository
into a release bundle, verifies its checksum, extracts it, installs it, restarts
the user units, and polls `/health`. No sudo, and safe to re-run: if the
deployed commit already matches the source it prints `Already current` and stops
before building anything.

`alexandria-ctl status` names the build that is answering — bundle id and source
commit, read from the installed distribution's PEP 610 provenance rather than
from the package version, which never changes. Compare it against
`git rev-parse --short=12 main` to see whether a host is behind.

**What it refuses, and why.** Deploying the wrong tree is the failure this
command exists to prevent.

- A source checkout that declares no `alexandria-mcp` / `alexandria-web` /
  `alexandria-ctl` entry point. The corpus repository is also called
  `alexandria` at the same version, so the name proves nothing and the entry
  points are what the units invoke.
- A tree that is not on `main`, or has uncommitted changes. Pass `--ref <ref>`
  to deploy something else deliberately, or `--force` to deploy the working tree
  as it stands.

`ALEXANDRIA_SOURCE` overrides where the tooling is checked out; it defaults to
`~/src/minority-report`. It is **not** `ALEXANDRIA_REPO`, which names the corpus
this service reads.

**Why no sudo.** The steps needing root — reserving the service registry entry
and asserting the tailscale route — only apply to a first install; an upgrade
re-uses entries already reserved. So this takes `install.py --skip-service` and
restarts the user units itself. That path skips the installer's service checks,
which is why this command polls `/health` and says so if the server does not
come back.

**First install, or changed routing.** Use the bundle directly, which does the
registry and tailscale work and will ask for a password:

```
python3 -m scripts.pack --output-dir ~/alexandria-deploy
cd ~/alexandria-deploy && sha256sum -c <bundle>.sha256
tar -xzf <bundle>.tar.gz && ./<bundle>/install.py --yes
```

`--dry-run` prints the plan without touching anything. A failed install rolls
back on its own: the symlink is reverted, the previous release reinstalled, and
systemd reloaded.

## Registry operations

```text
service-registry list
sudo service-registry reserve <service> --port N ...
sudo service-registry reserve <service> --allocate ...
sudo service-registry reserve-route <service> --https-port 443 --path /path ...
service-registry check [service]
sudo service-registry reconcile [service]
sudo service-registry release <service> --yes
```

Mutations lock and atomically replace the root-owned registry. Reservations
survive upgrades and rollbacks. Release is explicit; do not delete the registry
or choose a new port to work around damage.

## Recovery

1. Stop concurrent pack installs.
2. Inspect the registry and its backup without publishing their host inventory.
3. Validate the chosen copy against the schema and compare it with listeners,
   systemd user units, health identities, and tunnel status.
4. Restore atomically with the documented owner and mode, then run
   `sudo service-registry reconcile`.
5. Rebuild only from reviewed pack declarations if neither copy is usable; adopt
   a listener only after verifying its real owner.

The registry coordinates cooperating installers; it cannot prevent an unrelated
process from binding a port.
