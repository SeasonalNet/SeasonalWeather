from __future__ import annotations

from pathlib import Path

from seasonalweather.database.core import SeasonalDatabase
from seasonalweather.database.publications import PublicationLeaseRepository


def test_publication_lease_protects_until_output_and_keeps_metadata(tmp_path: Path) -> None:
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite3"))
    repo = PublicationLeaseRepository(db)
    repo.create(
        publication_id="pub-1",
        artifact_path=str(tmp_path / "cycle_pub_1.wav"),
        plane="cycle",
        request_id=7,
        created_at="2026-11-17T12:00:00+00:00",
        metadata={"sw_insert_id": "insert-1", "sw_rotation_count": "3"},
    )

    assert repo.protected_paths(
        now="2026-11-17T12:01:00+00:00",
        grace_cutoff="2026-11-17T11:00:00+00:00",
    ) == {str(tmp_path / "cycle_pub_1.wav")}
    observed = repo.reconcile_output("pub-1", "2026-11-17T12:02:00+00:00")
    assert observed[0]["metadata"] == {"sw_insert_id": "insert-1", "sw_rotation_count": "3"}
    assert repo.protected_paths(
        now="2026-11-17T13:00:00+00:00",
        grace_cutoff="2026-11-17T12:03:00+00:00",
    ) == {str(tmp_path / "cycle_pub_1.wav")}

    repo.set_state("pub-1", "finished", "2026-11-17T12:04:00+00:00", released_at="2026-11-17T12:04:00+00:00")
    assert repo.protected_paths(
        now="2026-11-17T13:00:00+00:00",
        grace_cutoff="2026-11-17T12:03:00+00:00",
    ) == {str(tmp_path / "cycle_pub_1.wav")}
    assert (
        repo.protected_paths(
            now="2026-11-17T13:00:00+00:00",
            grace_cutoff="2026-11-17T12:05:00+00:00",
        )
        == set()
    )


def test_publication_inventory_reconciles_states_and_player_restart(tmp_path: Path) -> None:
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite3"))
    repo = PublicationLeaseRepository(db)
    for publication_id, request_id in (("pub-ready", 7), ("pub-air", 8), ("pub-missing", 9)):
        repo.create(
            publication_id=publication_id,
            artifact_path=str(tmp_path / f"{publication_id}.wav"),
            plane="cycle",
            request_id=request_id,
            created_at="2026-11-17T12:00:00+00:00",
        )

    snapshot = {
        "epoch": "epoch-1",
        "all": {7, 8},
        "on_air": {8},
        "statuses": {7: "ready", 8: "playing"},
    }
    assert repo.runtime_epoch() is None
    assert repo.reconcile_inventory(snapshot, "2026-11-17T12:01:00+00:00") == 3
    assert repo.runtime_epoch() == "epoch-1"
    with db.connect() as conn:
        states = dict(conn.execute("SELECT publication_id, state FROM publication_leases"))
    assert states == {"pub-ready": "ready", "pub-air": "on_air", "pub-missing": "unknown"}
    assert repo.reconcile_inventory(snapshot, "2026-11-17T12:02:00+00:00") == 1
    with db.connect() as conn:
        assert (
            conn.execute("SELECT state FROM publication_leases WHERE publication_id = 'pub-missing'").fetchone()[0]
            == "failed"
        )

    repo.create(
        publication_id="pub-restart",
        artifact_path=str(tmp_path / "pub-restart.wav"),
        plane="cycle",
        request_id=11,
        created_at="2026-11-17T12:02:00+00:00",
    )
    assert (
        repo.reconcile_inventory(
            {"epoch": "epoch-2", "all": set(), "on_air": set(), "statuses": {}},
            "2026-11-17T12:03:00+00:00",
        )
        == 3
    )
    with db.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT state FROM publication_leases WHERE state = 'unknown'")} == {
            "unknown"
        }


def test_publication_cancellation_is_terminal_and_returns_request_ids(tmp_path: Path) -> None:
    db = SeasonalDatabase(path=str(tmp_path / "state.sqlite3"))
    repo = PublicationLeaseRepository(db)
    repo.create(
        publication_id="pub-1",
        artifact_path=str(tmp_path / "pub-1.wav"),
        plane="voice_alert",
        request_id=17,
        created_at="2026-11-17T12:00:00+00:00",
    )

    assert repo.active_request_ids(plane="voice_alert") == {17}
    assert repo.cancel_plane("voice_alert", "2026-11-17T12:01:00+00:00") == {17}
    assert repo.active_request_ids(plane="voice_alert") == set()
    assert repo.protected_paths(
        now="2026-11-17T12:02:00+00:00",
        grace_cutoff="2026-11-17T11:00:00+00:00",
    ) == {str(tmp_path / "pub-1.wav")}
    assert repo.reconcile_inventory(
        {"epoch": "epoch-1", "all": {17}, "on_air": set(), "statuses": {17: "ready"}},
        "2026-11-17T12:02:00+00:00",
    ) == 0
    assert repo.reconcile_inventory(
        {"epoch": "epoch-1", "all": set(), "on_air": set(), "statuses": {}},
        "2026-11-17T12:03:00+00:00",
    ) == 1
    assert repo.protected_paths(
        now="2026-11-17T13:00:00+00:00",
        grace_cutoff="2026-11-17T12:04:00+00:00",
    ) == set()
    assert repo.reconcile_output("pub-1", "2026-11-17T12:02:00+00:00") == []
    with db.connect() as conn:
        assert (
            conn.execute("SELECT state FROM publication_leases WHERE publication_id = 'pub-1'").fetchone()[0]
            == "cancelled"
        )
