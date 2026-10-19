"""Keep persistent Windows LAN forwards aligned with registered CDP instances.
Run through the elevated scheduled task installed by enable-cdp-lan.ps1.
"""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import subprocess
import time
import winreg

ROOT = Path(__file__).resolve().parents[1]
KEY = r'SYSTEM\CurrentControlSet\Services\PortProxy\v4tov4\tcp'


def forwarding_exists(port):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY) as key:
            return winreg.QueryValueEx(key, f'0.0.0.0/{port}')[0] == f'127.0.0.1/{port}'
    except OSError:
        return False


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr or result.stdout}')


def sync():
    with sqlite3.connect((ROOT / 'data/chrome_manager.db').as_uri() + '?mode=ro', uri=True) as db:
        ports = sorted({row[0] for row in db.execute('SELECT c.port FROM cdp_ports c JOIN profiles p ON p.id=c.profile_id WHERE p.is_deleted=0 AND p.status="running"')})
    if any(not isinstance(p, int) or not 9500 <= p <= 65535 for p in ports):
        raise ValueError('Invalid registered CDP port')
    state_path = ROOT / 'data/cdp-lan-ports.json'
    previous = json.loads(state_path.read_text()) if state_path.exists() else []
    for port in ports:
        if not forwarding_exists(port):
            run('netsh', 'interface', 'portproxy', 'add', 'v4tov4', 'listenaddress=0.0.0.0', f'listenport={port}', 'connectaddress=127.0.0.1', f'connectport={port}')
            logging.info('Enabled 0.0.0.0:%s -> 127.0.0.1:%s', port, port)
    for port in set(previous) - set(ports):
        if forwarding_exists(port):
            run('netsh', 'interface', 'portproxy', 'delete', 'v4tov4', 'listenaddress=0.0.0.0', f'listenport={int(port)}')
            logging.info('Removed obsolete forwarding for %s', port)
    if ports != previous:
        port_list = ','.join(str(p) for p in ports)
        script = "$ErrorActionPreference='Stop'; $r=Get-NetFirewallRule -Name 'ChromeManager-CDP-All-LAN' -ErrorAction SilentlyContinue; "
        if ports:
            script += f"if ($r) {{ $r | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort {port_list} }} else {{ New-NetFirewallRule -Name 'ChromeManager-CDP-All-LAN' -DisplayName 'ChromeManager all CDP instances LAN' -Direction Inbound -Action Allow -Protocol TCP -LocalPort {port_list} -RemoteAddress LocalSubnet -Profile Any | Out-Null }}"
        else:
            script += 'if ($r) { $r | Remove-NetFirewallRule }'
        run('powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script)
        state_path.write_text(json.dumps(ports), encoding='utf-8')
        logging.info('Firewall synchronized: %s', ports)


if __name__ == '__main__':
    handler = RotatingFileHandler(ROOT / 'logs/cdp-lan.log', maxBytes=2*1024*1024, backupCount=3, encoding='utf-8')
    logging.basicConfig(handlers=[handler], level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    while True:
        try:
            sync()
        except Exception:
            logging.exception('LAN forwarding synchronization failed; retrying')
        time.sleep(5)
