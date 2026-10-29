#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" == 0 ]]; then
  echo 'Run as the dedicated ordinary service user, not root.' >&2
  exit 1
fi
PROJECT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DATA_ROOT=${CHROME_MANAGER_DATA_ROOT:-"$HOME/.local/share/chromemanager/data-root"}
case "$PROJECT$DATA_ROOT" in *$'\n'*|*'%'*) echo 'Paths must not contain newline or systemd specifiers.' >&2; exit 1;; esac
command -v python3 >/dev/null
command -v systemctl >/dev/null
command -v xauth >/dev/null
command -v Xvfb >/dev/null
command -v openbox >/dev/null
command -v x11vnc >/dev/null
command -v xclip >/dev/null
test -f /usr/share/novnc/core/rfb.js
systemctl --user show-environment >/dev/null
umask 077
python3 -m venv "$PROJECT/.venv"
"$PROJECT/.venv/bin/pip" install --upgrade "pip>=26.2,<27"
"$PROJECT/.venv/bin/pip" install -e "$PROJECT"
mkdir -p "$DATA_ROOT/config" "$HOME/.config/chromemanager" "$HOME/.config/systemd/user"
DATA_ROOT=$(cd -- "$DATA_ROOT" && pwd)
if [[ ! -f "$DATA_ROOT/config/settings.toml" ]]; then
  cp "$PROJECT/config/settings.example.toml" "$DATA_ROOT/config/settings.toml"
fi
export CHROME_MANAGER_DATA_ROOT="$DATA_ROOT"
"$PROJECT/.venv/bin/python" -m chrome_manager.cli init
export CM_INSTALL_PROJECT="$PROJECT"
"$PROJECT/.venv/bin/python" - <<'PY'
from pathlib import Path
import os, json
project = Path(os.environ['CM_INSTALL_PROJECT']).resolve()
config = Path.home() / '.config/chromemanager'
previous = config / 'install.json'
if previous.exists() and Path(json.loads(previous.read_text())['data_root']).resolve() != Path(os.environ['CHROME_MANAGER_DATA_ROOT']).resolve():
    raise SystemExit('Existing installation uses another data root; migrate it explicitly before reinstalling.')
values = {'CHROME_MANAGER_DATA_ROOT': os.environ['CHROME_MANAGER_DATA_ROOT'],
          'DISPLAY': ':99', 'XAUTHORITY': str(config / 'Xauthority'),
          'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus',
          'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}
environment = config / 'environment'
if not environment.exists():
    environment.write_text(''.join(f'{k}={json.dumps(v)}\n' for k,v in values.items()), encoding='utf-8')
    environment.chmod(0o600)
for source in (project / 'deploy/systemd').glob('*.service'):
    text = source.read_text().replace('@@PROJECT@@', str(project)).replace('@@PYTHON@@', json.dumps(str(project / '.venv/bin/python'), ensure_ascii=False))
    (Path.home() / '.config/systemd/user' / source.name).write_text(text, encoding='utf-8')
(config / 'install.json').write_text(json.dumps({'data_root': os.environ['CHROME_MANAGER_DATA_ROOT'], 'project': str(project)}), encoding='utf-8')
PY
if [[ ! -f "$HOME/.config/chromemanager/Xauthority" ]]; then
  touch "$HOME/.config/chromemanager/Xauthority"
  xauth -f "$HOME/.config/chromemanager/Xauthority" add :99 . "$(mcookie)"
fi
systemctl --user daemon-reload
systemctl --user enable --now chromemanager-display.service chromemanager-window-manager.service chromemanager-desktop.service chromemanager-relay.service chromemanager-web.service
echo "Installed. Data: $DATA_ROOT"
echo "Enable boot without login once as administrator: sudo loginctl enable-linger $(id -un)"
echo 'Create/import instances, then explicitly enable selected profiles using profile-autostart.'
echo 'DISPLAY/XAUTHORITY and data root are in ~/.config/chromemanager/environment.'
