"""Remove retired telemetry storage from existing installations."""

VERSION = 8


def apply(connection):
    connection.execute('PRAGMA secure_delete = ON')
    connection.execute('DROP TABLE IF EXISTS resource_samples')
    connection.execute('UPDATE schema_version SET version = ?', (VERSION,))
