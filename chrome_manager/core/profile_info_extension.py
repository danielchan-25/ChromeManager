"""Generate a local, per-profile Chrome new-tab information page."""

from __future__ import annotations

import html
import json
from pathlib import Path

from chrome_manager.db.repository import Profile


class ProfileInfoExtension:
    """Keeps Profile metadata visible in Chrome without external network access."""

    @staticmethod
    def prepare(profile: Profile) -> Path:
        extension_dir = Path(profile.user_data_dir).parent / "ChromeManager Profile Info"
        extension_dir.mkdir(parents=True, exist_ok=True)
        (extension_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "Chrome Manager Profile Info",
                    "version": "1.0.0",
                    "chrome_url_overrides": {"newtab": "newtab.html"},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (extension_dir / "newtab.html").write_text(ProfileInfoExtension._page(profile), encoding="utf-8")
        return extension_dir

    @staticmethod
    def _page(profile: Profile) -> str:
        display_name = f"{profile.project_name or '未命名项目'} + {profile.platform or '未指定平台'}"
        fields = (
            ("实例", display_name),
            ("项目", profile.project_name or "—"),
            ("平台", profile.platform or "—"),
            ("账号", profile.account_name or "—"),
            ("描述", profile.description or "—"),
            ("标签", profile.tags or "—"),
            ("CDP 端口", str(profile.cdp_port or "—")),
            ("默认打开网址", profile.default_url or "—"),
            ("代理", profile.proxy_url or "直连"),
            ("用户数据目录", profile.user_data_dir),
        )
        rows = "".join(
            f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>" for label, value in fields
        )
        title = html.escape(f"Chrome Manager · {display_name}")
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>:root{{font-family:'Segoe UI',system-ui,sans-serif;color:#182125;background:#eef3f1}}body{{margin:0;padding:48px;max-width:960px}}main{{background:#fff;border:1px solid #d8e2de;border-radius:16px;padding:34px;box-shadow:0 12px 38px #26443812}}p{{color:#177c6c;font-size:12px;font-weight:700;letter-spacing:.12em}}h1{{font:700 36px/1.1 Georgia,serif;margin:8px 0 28px;color:#163f39}}dl{{display:grid;grid-template-columns:180px 1fr;gap:14px;margin:0}}dt{{color:#68756f}}dd{{margin:0;overflow-wrap:anywhere}}footer{{margin-top:32px;color:#68756f;font-size:13px}}</style>
</head><body><main><p>CHROME MANAGER · PROFILE INFO</p><h1>{html.escape(display_name)}</h1><dl>{rows}</dl><footer>此页面由 Chrome Manager 自动生成，仅展示本 Profile 的本地管理信息。</footer></main></body></html>"""
