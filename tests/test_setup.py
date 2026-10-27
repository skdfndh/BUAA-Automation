import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoboya.storage import AutoBoyaStore
from autoboya.models import UserRecord
from auto_td.storage import AppStorage

spec = importlib.util.spec_from_file_location("configure_td", Path(__file__).resolve().parents[1] / "scripts/configure_td.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOBOYA_HOME", str(tmp_path / "home"))
    store = AutoBoyaStore()
    store.init()
    store.upsert_user(UserRecord(username="10000001"))
    store.save_json("settings.json", {"auto_select_categories": ["美育"], "class_signin_enabled": True})
    image = tmp_path / "test.jpg"
    image.write_bytes(b"synthetic-image-test-only")
    args = SimpleNamespace(user="10000001", server="td.example.invalid", port=8888, term="TEST-TERM", start_date="2099-01-01",
                           end_date=None, confirmed_count=0, target_count=32, entrance=2, exit=6,
                           entrance_image=str(image), exit_image=str(image))
    return store, args


def test_fresh_setup_is_local_and_preserves_boya_settings(setup):
    store, args = setup
    module.configure(args)
    td = AppStorage(store.path("td"))
    assert td.get_last_count(args.user) == 0
    assert td.load_settings()["integration"]["start_date"] == "2099-01-01"
    assert store.load_json("settings.json")["auto_select_categories"] == ["美育"]
    assert store.load_json("settings.json")["class_signin_enabled"] is True
    assert store.load_json("settings.json")["td_enabled"] is True
    assert not td.telemetry_queue_path.exists()


def test_existing_progress_is_not_overwritten(setup):
    store, args = setup
    module.configure(args)
    state = store.path("td/state.json").read_bytes()
    with pytest.raises(ValueError, match="已有"):
        module.configure(args)
    assert store.path("td/state.json").read_bytes() == state


@pytest.mark.parametrize("field,value", [("confirmed_count", -1), ("entrance", 6), ("exit", 11), ("server", ""), ("port", 0)])
def test_invalid_setup_never_enables_td(setup, field, value):
    store, args = setup
    setattr(args, field, value)
    with pytest.raises(ValueError):
        module.configure(args)
    assert store.load_json("settings.json").get("td_enabled") is not True
