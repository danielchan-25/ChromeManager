from pathlib import Path

from chrome_manager.config import ensure_data_directories, load_settings, write_default_settings


def test_settings_creates_documented_layout(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "manager")
    ensure_data_directories(settings)
    write_default_settings(settings)
    assert settings.database_path.parent.is_dir()
    assert (settings.data_root / "profiles").is_dir()
    assert (settings.data_root / "logs" / "profiles").is_dir()
    assert settings.config_path.is_file()
