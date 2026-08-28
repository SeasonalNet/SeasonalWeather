from __future__ import annotations

SCHEMA_VERSION = 11

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS active_alerts (
            alert_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            event TEXT NOT NULL,
            code TEXT NOT NULL,
            headline TEXT NOT NULL,
            script_text TEXT NOT NULL,
            audio_path TEXT,
            expires_at TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            cycle_only INTEGER NOT NULL DEFAULT 0,
            watch_number INTEGER,
            first_aired_at TEXT,
            last_aired_at TEXT,
            airing_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_active_alerts_expires_at ON active_alerts (expires_at)",
        """
        CREATE TABLE IF NOT EXISTS active_alert_vtec (
            alert_id TEXT NOT NULL,
            vtec TEXT NOT NULL,
            PRIMARY KEY (alert_id, vtec),
            FOREIGN KEY (alert_id) REFERENCES active_alerts(alert_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS active_alert_same (
            alert_id TEXT NOT NULL,
            same_code TEXT NOT NULL,
            PRIMARY KEY (alert_id, same_code),
            FOREIGN KEY (alert_id) REFERENCES active_alerts(alert_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cap_seen_ledger (
            dedupe_key TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_cap_seen_ledger_seen_at ON cap_seen_ledger (seen_at)",
        """
        CREATE TABLE IF NOT EXISTS api_commands (
            command_id TEXT PRIMARY KEY,
            command_type TEXT NOT NULL,
            status TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            request_id TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            idempotent_replay_count INTEGER NOT NULL DEFAULT 0,
            result_json TEXT,
            error_json TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_api_commands_accepted_at ON api_commands (accepted_at)",
        """
        CREATE TABLE IF NOT EXISTS cycle_segments (
            segment_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            audio_path TEXT NOT NULL,
            duration_s REAL NOT NULL,
            last_updated_ts REAL NOT NULL,
            refresh_interval_s INTEGER NOT NULL,
            is_placeholder INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scheduler_state (
            scheduler_name TEXT PRIMARY KEY,
            last_run_at TEXT,
            next_run_at TEXT,
            state_json TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audio_assets (
            asset_id TEXT PRIMARY KEY,
            wav_path TEXT NOT NULL,
            original_filename TEXT,
            content_type TEXT,
            sha256 TEXT,
            headline TEXT,
            event_code TEXT,
            actor TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            meta_json TEXT
        )
        """,
    ),
    2: (
        "CREATE INDEX IF NOT EXISTS idx_api_commands_status_finished_at ON api_commands (status, finished_at)",
        "CREATE INDEX IF NOT EXISTS idx_audio_assets_expires_at ON audio_assets (expires_at)",
    ),
    3: (
        """
        CREATE TABLE IF NOT EXISTS station_feed_alerts (
            alert_id TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_station_feed_alerts_expires_at ON station_feed_alerts (expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_station_feed_alerts_updated_at ON station_feed_alerts (updated_at)",
    ),
    4: (
        """
        CREATE TABLE IF NOT EXISTS cycle_inserts (
            insert_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('text', 'audio')),
            title TEXT NOT NULL,
            text TEXT,
            audio_path TEXT,
            audio_asset_id TEXT,
            placement TEXT NOT NULL CHECK (placement IN ('after_time', 'after_status', 'end_of_rotation')),
            start_after TEXT,
            expires_at TEXT NOT NULL,
            repeat_mode TEXT NOT NULL CHECK (repeat_mode IN ('once', 'every_n_rotations')),
            repeat_every_rotations INTEGER NOT NULL DEFAULT 1,
            max_airings INTEGER NOT NULL DEFAULT 1,
            defer_during_active_alerts INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled', 'completed', 'expired')),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_aired_at TEXT,
            airing_count INTEGER NOT NULL DEFAULT 0,
            last_aired_rotation INTEGER,
            duration_seconds REAL NOT NULL DEFAULT 0.0,
            meta_json TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_cycle_inserts_status_expires ON cycle_inserts (status, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_cycle_inserts_due ON cycle_inserts (status, placement, start_after, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_cycle_inserts_audio_path ON cycle_inserts (audio_path)",
    ),
    5: (
        """
        CREATE TABLE IF NOT EXISTS auth_clients (
            client_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            verifier_algorithm TEXT NOT NULL,
            verifier_digest TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            route_prefixes_json TEXT NOT NULL,
            unrestricted_routes INTEGER NOT NULL CHECK (unrestricted_routes IN (0, 1)),
            cidrs_json TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            last_used_at TEXT,
            generation INTEGER NOT NULL CHECK (generation > 0)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_access_tokens (
            token_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            verifier_algorithm TEXT NOT NULL,
            verifier_digest TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            last_used_at TEXT,
            write_capable INTEGER NOT NULL CHECK (write_capable IN (0, 1)),
            client_generation INTEGER NOT NULL CHECK (client_generation > 0),
            FOREIGN KEY (client_id) REFERENCES auth_clients(client_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_auth_access_tokens_client ON auth_access_tokens (client_id)",
        "CREATE INDEX IF NOT EXISTS idx_auth_access_tokens_expires ON auth_access_tokens (expires_at)",
        """
        CREATE TABLE IF NOT EXISTS auth_audit_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            outcome TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            client_id TEXT NOT NULL,
            token_id TEXT,
            actor TEXT NOT NULL,
            source_ip TEXT,
            request_id TEXT,
            reason_code TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_auth_audit_occurred ON auth_audit_events (occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_auth_audit_client ON auth_audit_events (client_id, occurred_at)",
    ),
    6: (
        "ALTER TABLE api_commands ADD COLUMN created_at TEXT",
        "ALTER TABLE api_commands ADD COLUMN reason TEXT",
        "ALTER TABLE api_commands ADD COLUMN correlation_id TEXT",
        "ALTER TABLE api_commands ADD COLUMN cancel_requested_at TEXT",
        "ALTER TABLE api_commands ADD COLUMN audit_context_json TEXT NOT NULL DEFAULT '{}'",
        "UPDATE api_commands SET created_at = accepted_at WHERE created_at IS NULL",
        "UPDATE api_commands SET status = 'accepted' WHERE status = 'pending'",
    ),
    7: (
        """
        CREATE TABLE IF NOT EXISTS configuration_reload_active (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            generation INTEGER NOT NULL CHECK (generation >= 0),
            candidate_reference TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            candidate_identity_sha256 TEXT NOT NULL,
            report_sha256 TEXT,
            diff_sha256 TEXT,
            audit_reference TEXT,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS configuration_reload_attempts (
            attempt_id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL UNIQUE,
            phase TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT,
            dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
            old_generation INTEGER NOT NULL CHECK (old_generation >= 0),
            expected_generation INTEGER,
            final_generation INTEGER,
            candidate_reference TEXT,
            source_sha256 TEXT,
            candidate_sha256 TEXT,
            candidate_identity_sha256 TEXT,
            report_reference TEXT,
            report_sha256 TEXT,
            diff_sha256 TEXT,
            disposition TEXT,
            outcome TEXT,
            intent_json TEXT,
            audit_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_configuration_reload_attempts_updated ON configuration_reload_attempts (updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_configuration_reload_attempts_phase ON configuration_reload_attempts (phase)",
    ),
    8: (
        "ALTER TABLE configuration_reload_attempts ADD COLUMN request_json TEXT",
        "ALTER TABLE configuration_reload_attempts ADD COLUMN idempotency_identity TEXT",
        "ALTER TABLE configuration_reload_attempts ADD COLUMN source_byte_length INTEGER",
        "ALTER TABLE configuration_reload_attempts ADD COLUMN source_manifest_sha256 TEXT",
        "ALTER TABLE configuration_reload_attempts ADD COLUMN old_candidate_reference TEXT",
        "ALTER TABLE configuration_reload_attempts ADD COLUMN old_source_sha256 TEXT",
        "ALTER TABLE configuration_reload_attempts ADD COLUMN old_candidate_identity_sha256 TEXT",
        "CREATE INDEX IF NOT EXISTS idx_configuration_reload_attempts_command_finish ON configuration_reload_attempts (command_id, finished_at)",
    ),
    9: (
        """
        CREATE TABLE IF NOT EXISTS configuration_reload_admissions (
            idempotency_key TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE,
            command_id TEXT UNIQUE,
            actor TEXT NOT NULL,
            reason TEXT,
            request_json TEXT NOT NULL,
            candidate_reference TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_byte_length INTEGER NOT NULL,
            source_manifest_sha256 TEXT NOT NULL,
            candidate_sha256 TEXT NOT NULL,
            candidate_identity_sha256 TEXT NOT NULL,
            old_generation INTEGER NOT NULL,
            old_candidate_reference TEXT NOT NULL,
            old_source_sha256 TEXT NOT NULL,
            old_candidate_identity_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "ALTER TABLE configuration_reload_attempts ADD COLUMN retirement_json TEXT",
        "ALTER TABLE configuration_reload_attempts ADD COLUMN retirement_evidence_json TEXT",
        "ALTER TABLE configuration_reload_attempts ADD COLUMN recovery_json TEXT",
    ),
    10: (
        "ALTER TABLE cycle_segments ADD COLUMN max_age_s INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE cycle_segments ADD COLUMN source_name TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN product_identifier TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN product_type TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN issuing_office TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN issuance_time TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN fetch_time TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN last_successful_synthesis TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN content_hash TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN source_reference TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN last_error TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE cycle_segments ADD COLUMN last_aired TEXT",
        "ALTER TABLE cycle_segments ADD COLUMN next_eligible_airtime TEXT",
    ),
    11: (
        """
        CREATE TABLE IF NOT EXISTS observation_pressure_history (
            station_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            pressure_inhg REAL NOT NULL,
            PRIMARY KEY (station_id, observed_at)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_observation_pressure_history_time ON observation_pressure_history (observed_at)",
        """
        CREATE TABLE IF NOT EXISTS runtime_process_markers (
            marker_name TEXT PRIMARY KEY,
            marker_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS configuration_candidates (
            candidate_reference TEXT PRIMARY KEY,
            metadata_json TEXT NOT NULL,
            captured_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS configuration_candidate_reports (
            candidate_reference TEXT NOT NULL,
            report_reference TEXT NOT NULL,
            report_sha256 TEXT NOT NULL,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (candidate_reference, report_reference),
            FOREIGN KEY (candidate_reference) REFERENCES configuration_candidates(candidate_reference) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_configuration_candidate_reports_digest ON configuration_candidate_reports (report_sha256)",
        """
        CREATE TABLE IF NOT EXISTS segment_commit_journals (
            operation_id TEXT PRIMARY KEY,
            segment_key TEXT NOT NULL,
            target_path TEXT NOT NULL,
            previous_path TEXT,
            command_id TEXT,
            committed INTEGER NOT NULL CHECK (committed IN (0, 1)),
            publication_won INTEGER NOT NULL CHECK (publication_won IN (0, 1)),
            previous_entry_json TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_segment_commit_journals_key ON segment_commit_journals (segment_key, command_id)",
        """
        CREATE TABLE IF NOT EXISTS segment_commit_receipts (
            segment_key TEXT NOT NULL,
            command_id TEXT NOT NULL,
            target_path TEXT NOT NULL,
            PRIMARY KEY (segment_key, command_id)
        )
        """,
    ),
}
