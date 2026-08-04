from __future__ import annotations

import json
import socket
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from deploy.service_registry import (
    RegistryError,
    RegistryStore,
    normalize_path,
    observe_service,
    release_service,
    reserve_route,
    reserve_service,
    unknown_listeners,
)


def _reserve_dynamic(path_text: str, index: int) -> int:
    store = RegistryStore(
        Path(path_text),
        static_range=(18700, 18799),
        dynamic_range=(18800, 18819),
    )
    entry = reserve_service(
        store,
        service_id=f"worker-{index}",
        display_name=f"Worker {index}",
        owner="test",
        address="127.0.0.1",
        port=None,
    )
    return int(entry["endpoint"]["port"])


def _store(tmp_path: Path) -> RegistryStore:
    return RegistryStore(
        tmp_path / "registry.json",
        static_range=(18700, 18799),
        dynamic_range=(18800, 18819),
    )


def test_static_reservation_is_durable_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = reserve_service(
        store,
        service_id="alexandria",
        display_name="Alexandria",
        owner="dhk",
        address="127.0.0.1",
        port=18797,
        unit="alexandria-mcp.service",
        health_url="http://127.0.0.1:18797/health",
        health_service="alexandria",
        source="dhk/alexandria",
    )
    second = reserve_service(
        store,
        service_id="alexandria",
        display_name="Alexandria",
        owner="dhk",
        address="127.0.0.1",
        port=18797,
        unit="alexandria-mcp.service",
        health_url="http://127.0.0.1:18797/health",
        health_service="alexandria",
        source="dhk/alexandria",
    )

    assert first["created_at"] == second["created_at"]
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["services"]["alexandria"]["endpoint"] == {
        "address": "127.0.0.1",
        "allocation": "static",
        "port": 18797,
        "protocol": "tcp",
    }
    assert store.path.with_suffix(".json.bak").is_file()


def test_collision_names_the_current_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    reserve_service(
        store,
        service_id="wingman",
        display_name="Wingman",
        owner="dhk",
        address="127.0.0.1",
        port=18787,
    )

    with pytest.raises(RegistryError, match="reserved by wingman"):
        reserve_service(
            store,
            service_id="alexandria",
            display_name="Alexandria",
            owner="dhk",
            address="127.0.0.1",
            port=18787,
        )


def test_concurrent_dynamic_reservations_are_unique(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"

    with ProcessPoolExecutor(max_workers=8) as pool:
        ports = list(pool.map(_reserve_dynamic, [str(registry)] * 8, range(8)))

    assert len(set(ports)) == 8
    assert all(18800 <= port <= 18819 for port in ports)
    persisted = RegistryStore(
        registry,
        static_range=(18700, 18799),
        dynamic_range=(18800, 18819),
    ).load()
    assert len(persisted["services"]) == 8


def test_dynamic_reservation_reuses_its_durable_assignment(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = reserve_service(
        store,
        service_id="worker",
        display_name="Worker",
        owner="test",
        address="127.0.0.1",
        port=None,
    )
    second = reserve_service(
        store,
        service_id="worker",
        display_name="Worker",
        owner="test",
        address="127.0.0.1",
        port=None,
    )

    assert first["endpoint"] == second["endpoint"]


def test_static_reservation_must_use_static_band(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="outside range"):
        reserve_service(
            _store(tmp_path),
            service_id="worker",
            display_name="Worker",
            owner="test",
            address="127.0.0.1",
            port=18801,
        )


def test_route_reservations_have_an_independent_collision_namespace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for service_id, port in (("wingman", 18787), ("alexandria", 18797)):
        reserve_service(
            store,
            service_id=service_id,
            display_name=service_id.title(),
            owner="dhk",
            address="127.0.0.1",
            port=port,
        )
    reserve_route(
        store,
        service_id="wingman",
        host="tailscale-self",
        https_port=443,
        path="/",
        mode="funnel",
        target="http://127.0.0.1:18787",
    )
    reserve_route(
        store,
        service_id="alexandria",
        host="tailscale-self",
        https_port=443,
        path="/alexandria/",
        mode="funnel",
        target="http://127.0.0.1:18797",
    )

    assert normalize_path("alexandria/") == "/alexandria"
    assert store.load()["services"]["alexandria"]["route"]["path"] == "/alexandria"

    with pytest.raises(RegistryError, match="reserved by alexandria"):
        reserve_route(
            store,
            service_id="wingman",
            host="tailscale-self",
            https_port=443,
            path="/alexandria",
            mode="funnel",
            target="http://127.0.0.1:18787",
        )


def test_release_is_explicit_and_preserves_a_backup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    reserve_service(
        store,
        service_id="temporary",
        display_name="Temporary",
        owner="test",
        address="127.0.0.1",
        port=18755,
    )

    released = release_service(store, "temporary")

    assert released["service_id"] == "temporary"
    assert store.load()["services"] == {}
    backup = json.loads(store.path.with_suffix(".json.bak").read_text(encoding="utf-8"))
    assert "temporary" in backup["services"]


def test_registry_refuses_range_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    reserve_service(
        store,
        service_id="one",
        display_name="One",
        owner="test",
        address="127.0.0.1",
        port=18701,
    )

    changed = RegistryStore(
        store.path,
        static_range=(19000, 19099),
        dynamic_range=(19100, 19199),
    )
    with pytest.raises(RegistryError, match="port ranges"):
        reserve_service(
            changed,
            service_id="two",
            display_name="Two",
            owner="test",
            address="127.0.0.1",
            port=19001,
        )


def test_registry_refuses_overlapping_allocation_bands(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="must not overlap"):
        RegistryStore(
            tmp_path / "registry.json",
            static_range=(18700, 18800),
            dynamic_range=(18800, 18900),
        )


def test_reconcile_finds_unknown_listener_in_managed_range(tmp_path: Path) -> None:
    store = RegistryStore(
        tmp_path / "registry.json",
        static_range=(18700, 18701),
        dynamic_range=(18800, 18801),
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 18801))
        listener.listen()

        found = unknown_listeners(store, {})

    assert found == [{"protocol": "tcp", "address": "127.0.0.1", "port": 18801}]


def test_observation_marks_stale_systemd_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deploy.service_registry._listener_ready", lambda _endpoint: True)
    monkeypatch.setattr(
        "deploy.service_registry._unit_status",
        lambda _owner, _unit: (False, "inactive"),
    )
    entry = {
        "service_id": "alexandria",
        "owner": "dhk",
        "unit": "alexandria-mcp.service",
        "endpoint": {"address": "127.0.0.1", "port": 8797},
        "health": None,
        "route": None,
    }

    observed = observe_service(entry)

    assert observed["state"] == "stale"
    assert observed["listener"] is True
    assert observed["unit"] is False
    assert observed["unit_detail"] == "inactive"


def test_observation_marks_running_service_without_health_as_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("deploy.service_registry._listener_ready", lambda _endpoint: True)
    monkeypatch.setattr(
        "deploy.service_registry._unit_status",
        lambda _owner, _unit: (True, "active"),
    )
    observed = observe_service(
        {
            "service_id": "wingman",
            "owner": "dhk",
            "unit": "wingman-mcp.service",
            "endpoint": {"address": "127.0.0.1", "port": 8787},
            "health": None,
            "route": None,
        }
    )

    assert observed["state"] == "installed"
