"""Initial V1 schema."""

from __future__ import annotations

import sqlite3

VERSION = 1


def apply(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version(version) VALUES (1);

        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            project_name TEXT,
            platform TEXT,
            account_name TEXT,
            description TEXT,
            tags TEXT,
            user_data_dir TEXT UNIQUE NOT NULL,
            chrome_path TEXT,
            cdp_port_id INTEGER,
            proxy_id INTEGER,
            status TEXT NOT NULL DEFAULT 'stopped',
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE cdp_ports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            port INTEGER UNIQUE NOT NULL,
            purpose TEXT,
            project_name TEXT,
            platform TEXT,
            account_name TEXT,
            profile_id INTEGER REFERENCES profiles(id),
            reserved INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            last_pid INTEGER,
            last_started_at DATETIME,
            last_stopped_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE proxies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            type TEXT NOT NULL,
            host TEXT,
            port INTEGER,
            username TEXT,
            password TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE runtime_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            pid INTEGER,
            process_create_time REAL,
            cdp_port INTEGER,
            chrome_version TEXT,
            started_at DATETIME,
            stopped_at DATETIME,
            exit_code INTEGER,
            stop_reason TEXT
        );

        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER REFERENCES profiles(id),
            event_type TEXT NOT NULL,
            message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
