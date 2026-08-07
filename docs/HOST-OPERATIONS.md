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
