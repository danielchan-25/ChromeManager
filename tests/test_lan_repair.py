import io
import json
from unittest.mock import Mock
from urllib.error import URLError
import pytest
from chrome_manager.core import cdp
from chrome_manager.core.cdp_ports import browser_port
from chrome_manager.core.profile_manager import ProfileManager, ProfileError
from chrome_manager.db.database import Database


def test_ipv6_only_browser_probe(monkeypatch):
    opener=Mock()
    opener.open.side_effect=[URLError('refused'),io.StringIO(json.dumps({'webSocketDebuggerUrl':'ws://[::1]:9610/devtools/browser/test'}))]
    monkeypatch.setattr(cdp,'build_opener',lambda *_:opener)
    monkeypatch.setattr(cdp,'command',Mock(return_value={'product':'Chrome/test','protocolVersion':'1.3'}))
    assert cdp.probe_browser(9610,2)['product']=='Chrome/test'
    assert '[::1]' in opener.open.call_args.args[0]


def test_internal_ports_are_unique_persistent_and_not_public(tmp_path):
    db=Database(tmp_path/'data/chrome_manager.db');db.initialize()
    m=ProfileManager(db,tmp_path/'profiles')
    a=m.create('a',cdp_port=9600);b=m.create('b',cdp_port=9601)
    assert browser_port(db,a)==9600
    internal=browser_port(db,a,True)
    assert internal!=9600
    assert browser_port(db,a,True)==internal
    assert browser_port(db,b,True)!=internal
    with pytest.raises(ProfileError):m.create('collision',cdp_port=internal)
