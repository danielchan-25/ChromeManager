"""Separate public CDP endpoints from Chrome listeners."""
VERSION = 7

def apply(connection):
    if 'backend_port' not in {r[1] for r in connection.execute('PRAGMA table_info(cdp_ports)')}:
        connection.execute('ALTER TABLE cdp_ports ADD COLUMN backend_port INTEGER')
    connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS cdp_backend_port ON cdp_ports(backend_port)')
    connection.execute('UPDATE schema_version SET version=7')
