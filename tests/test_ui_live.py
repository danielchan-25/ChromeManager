"""Opt-in real-browser regression tests against an intercepted isolated console."""
import json
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from chrome_manager.core.backup_manager import BackupManager
from chrome_manager.core.profile_manager import ProfileManager
from chrome_manager.db.database import Database
from chrome_manager.web import create_app

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or os.environ.get('CHROME_MANAGER_LIVE_TEST') != '1',
                                reason='Opt-in real Linux Chrome UI tests')


@pytest.fixture
def console(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    app = create_app()
    db = Database(tmp_path / 'data/chrome_manager.db')
    manager = ProfileManager(db, tmp_path / 'profiles')
    first = manager.create('one', cdp_port=19545, project_name='<script>unsafe()</script>')
    second = manager.create('two', cdp_port=19546)
    backups = BackupManager(db, tmp_path / 'profiles', tmp_path / 'backups')
    backups.backup(first)
    backups.backup(first)
    client = TestClient(app)
    root = Path(__file__).resolve().parents[1]
    api = {'checked_at': int(time.time()),
           'instances': {str(p.id): {'status': 'stopped'} for p in [first, second]},
           'runtime': {}, 'cdp': {}, 'warning': None}
    actions, errors = [], []
    def route(request):
        path = urlsplit(request.request.url).path
        if path == '/api/status':
            request.fulfill(json=api)
        elif path.startswith('/static/'):
            asset = root / 'chrome_manager' / path.lstrip('/')
            if asset.name == 'app.js' and os.environ.get('CM_UI_ASSET_ROOT'):
                asset = Path(os.environ['CM_UI_ASSET_ROOT']) / 'app.js'
            request.fulfill(path=asset, content_type='text/javascript' if asset.suffix == '.js' else 'text/css')
        elif request.request.method == 'POST':
            actions.append(path)
            request.fulfill(status=200, body='action recorded')
        else:
            response = client.get(path)
            request.fulfill(status=response.status_code, body=response.content, content_type='text/html')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=shutil.which('google-chrome-stable'),
                                     chromium_sandbox=os.environ.get('CM_TEST_NO_SANDBOX') != '1',
                                     args=['--no-proxy-server'])
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('http://console.test/**', route)
        page.route('https://api.ipify.org/**', lambda r: r.fulfill(json={'ip': '203.0.113.99'}))
        page.clock.install()
        page.goto('http://console.test/')
        yield page, actions, errors, first, second
        browser.close()
    client.close()


def test_restore_confirmation_survives_status_poll(console):
    from playwright.sync_api import expect
    page, _, errors, _, _ = console
    select = page.locator('#restore-backup-select')
    second_option = select.locator('option').nth(1).get_attribute('value')
    select.select_option(second_option)
    consent = page.locator('#restore-form [name=confirmed]')
    consent.check()
    with page.expect_response('**/api/status'):
        page.clock.fast_forward(10000)
    expect(consent).to_be_checked()
    expect(select).to_have_value(second_option)
    assert errors == []


def test_delete_confirmation_targets_original_profile(console):
    page, actions, errors, first, second = console
    page.locator('#delete-form').evaluate('(form) => form.requestSubmit()')
    page.locator(f'.instance-row[data-profile-id="{second.id}"]').evaluate('(row) => row.click()')
    page.locator('#confirm-delete').click()
    assert actions == [f'/profiles/{first.name}/delete']
    assert errors == []


def test_console_and_profile_info_have_no_javascript_errors(console):
    from playwright.sync_api import expect
    page, _, errors, first, _ = console
    expect(page.locator('#detail-title')).to_have_text('<script>unsafe()</script> + 未指定平台')
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(page.locator('#new-profile-button')).to_be_visible()
    page.goto(f'http://console.test/profiles/{first.name}')
    expect(page.locator('#exit-ip')).to_have_text('203.0.113.99')
    assert errors == []


def test_table_keyboard_selection_updates_detail_and_action_target(console):
    from playwright.sync_api import expect
    page, _, errors, first, second = console
    row = page.locator(f'.instance-row[data-profile-id="{second.id}"]')
    row.locator('.instance-select').focus()
    page.keyboard.press('Enter')
    expect(row.locator('.instance-select')).to_have_attribute('aria-pressed', 'true')
    expect(page.locator(f'.instance-row[data-profile-id="{first.id}"] .instance-select')).to_have_attribute('aria-pressed', 'false')
    expect(page.locator('#detail-title')).to_have_text('未命名项目 + 未指定平台')
    expect(page.locator('#edit-link')).to_have_attribute('href', f'/profiles/{second.name}/edit')
    expect(page.locator('#profile-link')).to_have_attribute('href', f'/profiles/{second.name}')
    assert f'/profiles/{second.name}/start?' in page.locator('#start-form').get_attribute('action')
    assert errors == []


def test_poll_updates_table_detail_and_summary_together(console):
    from playwright.sync_api import expect
    page, _, errors, first, second = console
    payload = {'checked_at': int(time.time()),
        'instances': {str(first.id): {'status': 'running'}, str(second.id): {'status': 'stopped'}},
        'runtime': {str(first.id): {'pid': 12345, 'started_at': '2026-10-28 00:00:00'}},
        'cdp': {str(first.id): {'pid': 12345, 'state': 'failed', 'reason': '测试连接失败'}}}
    page.route('**/api/status', lambda route: route.fulfill(json=payload))
    with page.expect_response('**/api/status'):
        page.clock.fast_forward(10000)
    row = page.locator(f'.instance-row[data-profile-id="{first.id}"]')
    expect(row.locator('.row-status')).to_have_text('运行中')
    expect(row.locator('.row-cdp')).to_have_text('CDP 连接失败')
    expect(page.locator('#summary-running')).to_have_text('1')
    expect(page.locator('#summary-stopped')).to_have_text('1')
    expect(page.locator('#detail-pid')).to_have_text('12345')
    expect(page.locator('#focus-form')).to_be_visible()
    expect(page.locator('#focus-form')).to_have_attribute('target', '_blank')
    expect(page.locator('#detail-cdp-reason')).to_have_text('测试连接失败')
    assert errors == []


def test_mobile_dialog_keyboard_focus_and_layout(console):
    from playwright.sync_api import expect
    page, _, errors, _, _ = console
    for width in (1440, 1024, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#new-profile-button').click()
    expect(page.locator('#new-profile input[name=port]')).to_be_focused()
    page.keyboard.press('Shift+Tab')
    expect(page.locator('#close-new-profile')).to_be_focused()
    page.keyboard.press('Tab')
    expect(page.locator('#new-profile input[name=port]')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#new-profile')).to_be_hidden()
    expect(page.locator('#new-profile-button')).to_be_focused()
    assert errors == []
