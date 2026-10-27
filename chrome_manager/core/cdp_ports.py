"""Reserve separate Chrome listener ports for LAN-enabled deployments."""
import socket
from chrome_manager.utils.audit import event

def browser_port(database, profile, enabled=False):
    with database.transaction() as db:
        row = db.execute('SELECT backend_port FROM cdp_ports WHERE profile_id=?', (profile.id,)).fetchone()
        if row and row[0]:
            return row[0]
        if not enabled:
            return profile.cdp_port
        reserved = {r[0] for r in db.execute('SELECT port FROM cdp_ports UNION SELECT backend_port FROM cdp_ports')}
        for port in range(20000, 30000):
            if port in reserved:
                continue
            with socket.socket() as sock:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    sock.bind(('127.0.0.1', port))
                except OSError:
                    continue
            db.execute('UPDATE cdp_ports SET backend_port=? WHERE profile_id=?', (port, profile.id))
            event('browser.backend_port.allocated', profile_id=profile.id, port=profile.cdp_port, backend_port=port)
            return port
    raise RuntimeError('No free internal CDP port')
