from pathlib import Path

import chrome_manager.config as config
from chrome_manager.config import ensure_data_directories, load_settings, write_default_settings


def test_settings_creates_documented_layout(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "manager")
    ensure_data_directories(settings)
    write_default_settings(settings)
    assert settings.database_path.parent.is_dir()
    assert (settings.data_root / "profiles").is_dir()
    assert (settings.data_root / "logs" / "profiles").is_dir()
    assert settings.config_path.is_file()
    assert "root =" not in settings.config_path.read_text(encoding="utf-8")


def test_old_generated_absolute_root_does_not_override_relocated_project(tmp_path: Path, monkeypatch) -> None:
    new_root = tmp_path / ".data"
    config_file = new_root / "config" / "settings.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('[data]\nroot = "D:/OldLocation/ChromeManager/.data"\n', encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_DATA_ROOT", new_root)
    monkeypatch.delenv(config.ENV_DATA_ROOT, raising=False)
    assert load_settings().data_root == new_root
