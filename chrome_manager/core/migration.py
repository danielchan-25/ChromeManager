"""Import Windows metadata into fresh Linux profiles, never browser credentials."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from contextlib import closing

from chrome_manager.config import Settings, ensure_data_directories
from chrome_manager.core.profile_manager import ProfileManager, ProfileError
from chrome_manager.db.database import Database
from chrome_manager.db.repository import ProfileRepository


def import_windows_metadata(source: Path, settings: Settings) -> list[str]:
    source = source.resolve(strict=True)
    if source == settings.database_path.resolve():
        raise ProfileError('源数据库与目标数据库不能相同')
    ensure_data_directories(settings)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as old:
        old.row_factory = sqlite3.Row
        records = old.execute(
            "SELECT p.*,c.port,CASE WHEN px.id IS NULL THEN NULL ELSE px.type || '://' || px.host || ':' || px.port END AS proxy "
            'FROM profiles p JOIN cdp_ports c ON c.profile_id=p.id LEFT JOIN proxies px ON px.id=p.proxy_id WHERE p.is_deleted=0 ORDER BY p.id').fetchall()
    database = Database(settings.database_path)
    database.initialize()
    manager = ProfileManager(database, settings.data_root / 'profiles', auto_port_start=settings.auto_port_start, auto_port_end=settings.auto_port_end, runtime=settings.runtime)
    repo = ProfileRepository(settings.data_root)
    made = []
    names = []
    try:
        with database.transaction() as db:
            for row in records:
                name, port = row['name'], row['port']
                manager._validate_name(name)
                if not isinstance(port, int) or not 9500 <= port <= 65535:
                    raise ProfileError(f'{name}: 公开 CDP 端口无效')
                if repo.get_any_by_name(db, name) or db.execute('SELECT 1 FROM cdp_ports WHERE port=? OR backend_port=?', (port, port)).fetchone():
                    raise ProfileError(f'{name}: 名称或端口已存在，导入已取消')
                url = manager._validate_default_url(row['default_url'] if 'default_url' in row.keys() else None)
                proxy = manager._parse_proxy(row['proxy'])
                directory = settings.data_root / 'profiles' / name
                directory.mkdir(parents=False, exist_ok=False)
                made.append(directory)
                (directory / 'User Data').mkdir()
                profile = repo.create(db, name=name, user_data_dir=f'profiles/{name}/User Data',
                                      project_name=row['project_name'], platform=row['platform'],
                                      account_name=row['account_name'], description=row['description'],
                                      tags=row['tags'], cdp_port=port, default_url=url, proxy=proxy)
                db.execute("INSERT INTO events(profile_id,event_type,message) VALUES (?,'metadata_imported','Imported metadata only; fresh Linux profile requires login')", (profile.id,))
                names.append(name)
    except Exception:
        for directory in reversed(made):
            # Only remove our empty directories; preserve unexpected/new files.
            for path in [directory / 'User Data', directory]:
                try:
                    path.rmdir()
                except OSError:
                    pass
        raise
    return names
