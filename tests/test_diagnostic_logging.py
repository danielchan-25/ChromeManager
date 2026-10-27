import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from chrome_manager.utils.audit import event, operation, step
from chrome_manager.utils.logger import configure_logging
from chrome_manager.web import create_app


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_failure_traceback_context_and_sensitive_details(tmp_path):
    logger = configure_logging(tmp_path, 1, 1)
    try:
        with pytest.raises(RuntimeError):
            with operation('browser.start', request_id='request-a', profile_id=4, port=9600):
                with step('cdp.probe'):
                    event('sensitive', token='private-token', headers={'Authorization': 'Bearer private-auth'},
                          url='https://user:private-password@host/path?secret=private-query',
                          arguments=['--proxy-server=http://user:private-proxy@host:8080'])
                    try:
                        raise ValueError('password=private-error')
                    except ValueError as exc:
                        raise RuntimeError('probe failed') from exc
        output = (tmp_path / 'diagnostics.jsonl').read_text(encoding='utf-8')
        assert 'private-' not in output
        records = read_events(tmp_path / 'diagnostics.jsonl')
        failed = [r for r in records if r['event'].endswith('.failed')]
        assert len(failed) == 2
        assert all(r['request_id'] == 'request-a' and r['port'] == 9600 for r in failed)
        assert len({r['operation_id'] for r in failed}) == 1
        assert all('ValueError' in r['traceback'] and 'RuntimeError' in r['traceback'] for r in failed)
        assert all(r['duration_ms'] >= 0 for r in failed)
        assert 'probe failed' in (tmp_path / 'error.log').read_text(encoding='utf-8')
        event('after_operation')
        assert 'operation_id' not in read_events(tmp_path / 'diagnostics.jsonl')[-1]
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_concurrent_context_survives_threads_and_nested_operations(tmp_path):
    logger = configure_logging(tmp_path, 1, 1)
    async def check(profile_id):
        with operation('cdp.check', profile_id=profile_id):
            await asyncio.sleep(0)
            with operation('cdp.child'):
                await asyncio.to_thread(event, 'thread_probe')
    async def exercise():
        await asyncio.gather(check(4), check(5))
    try:
        asyncio.run(exercise())
        records = read_events(tmp_path / 'diagnostics.jsonl')
        probes = [r for r in records if r['event'] == 'thread_probe']
        assert {r['profile_id'] for r in probes} == {4, 5}
        assert len({r['operation_id'] for r in probes}) == 2
        for probe in probes:
            parent = next(r for r in records if r.get('operation_id') == probe['parent_operation_id'])
            assert parent['profile_id'] == probe['profile_id']
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_http_correlation_records_errors_without_request_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('CHROME_MANAGER_DATA_ROOT', str(tmp_path))
    app = create_app()
    @app.get('/test-error')
    def fail():
        raise RuntimeError('diagnostic failure')
    with TestClient(app) as client:
        response = client.get('/test-error?token=private-query',
                              headers={'Authorization': 'Bearer private-header'})
        assert response.status_code == 500
        request_id = response.headers['x-request-id']
        assert response.json()['request_id'] == request_id
    output = (tmp_path / 'logs/diagnostics.jsonl').read_text(encoding='utf-8')
    assert 'private-query' not in output and 'private-header' not in output
    records = [r for r in read_events(tmp_path / 'logs/diagnostics.jsonl') if r.get('request_id') == request_id]
    assert any(r['event'] == 'http.request.error' and 'RuntimeError' in r['traceback'] for r in records)
    assert records[-1]['status_code'] == 500


def test_diagnostics_rotate_at_configured_size(tmp_path):
    logger = configure_logging(tmp_path, 1, 1)
    try:
        for _ in range(3):
            event('large', detail='x' * 600_000)
        assert (tmp_path / 'diagnostics.jsonl.1').exists()
        assert not (tmp_path / 'diagnostics.jsonl.2').exists()
        assert (tmp_path / 'diagnostics.jsonl').stat().st_size < 1024 * 1024
        read_events(tmp_path / 'diagnostics.jsonl')
        read_events(tmp_path / 'diagnostics.jsonl.1')
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
