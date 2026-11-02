import json
import stat

import pytest

import landsat_record.cli as cli
from landsat_record.keys import KeyFileError, find_key, read_service_account, store_key, stored_key_path

from .conftest import FAKE_PRIVATE_KEY

SECRET_FRAGMENT = "c2VjcmV0LW1hdGVyaWFs"


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_stored_key_is_private(config_home, fake_key):
    stored = store_key(fake_key)
    assert stored == stored_key_path() and stored.read_bytes() == fake_key.read_bytes()
    assert mode(stored) == 0o600
    assert mode(stored.parent) == 0o700


def test_restoring_replaces_the_key_and_stays_private(config_home, fake_key):
    store_key(fake_key)
    stored_key_path().chmod(0o644)
    store_key(fake_key)
    assert mode(stored_key_path()) == 0o600


def test_rejects_files_that_are_not_service_account_keys(tmp_path):
    user_credentials = tmp_path / "user.json"
    user_credentials.write_text(json.dumps({"type": "authorized_user", "refresh_token": SECRET_FRAGMENT}))
    with pytest.raises(KeyFileError, match="service-account") as caught:
        read_service_account(user_credentials)
    assert SECRET_FRAGMENT not in str(caught.value)

    not_json = tmp_path / "key.json"
    not_json.write_text(f"private_key: {SECRET_FRAGMENT}")
    with pytest.raises(KeyFileError, match="isn't a JSON key file") as caught:
        read_service_account(not_json)
    assert SECRET_FRAGMENT not in str(caught.value)

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"type": "service_account", "private_key": FAKE_PRIVATE_KEY}))
    with pytest.raises(KeyFileError, match="missing client_email") as caught:
        read_service_account(incomplete)
    assert SECRET_FRAGMENT not in str(caught.value)


def test_key_lookup_order(config_home, fake_key, tmp_path, monkeypatch):
    assert find_key() is None
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_key))
    assert find_key() == fake_key
    store_key(fake_key)
    assert find_key() == stored_key_path()
    other = tmp_path / "other-key.json"
    other.write_bytes(fake_key.read_bytes())
    assert find_key(str(other)) == other


def test_login_and_logout(config_home, fake_key, capsys, monkeypatch):
    monkeypatch.setattr(cli, "missing_permissions", lambda key, bucket: [])
    assert cli.main(["login", str(fake_key), "--remove-original"]) == 0
    out = capsys.readouterr().out
    assert "publisher@example.iam.gserviceaccount.com" in out and "can publish to gs://ldeo-glaciology" in out
    assert not fake_key.exists() and stored_key_path().exists()
    assert SECRET_FRAGMENT not in out

    assert cli.main(["logout"]) == 0
    assert not stored_key_path().exists()


def test_login_warns_about_keys_in_a_git_repo_and_missing_permissions(config_home, fake_key, capsys, monkeypatch):
    (fake_key.parent / ".git").mkdir()
    monkeypatch.setattr(cli, "missing_permissions", lambda key, bucket: ["storage.objects.delete"])
    assert cli.main(["login", str(fake_key)]) == 1
    output = capsys.readouterr()
    assert "inside the git repo" in output.out and "lacks storage.objects.delete" in output.out
    assert fake_key.exists()
    assert SECRET_FRAGMENT not in output.out + output.err


def test_login_rejects_a_bad_file_without_showing_it(config_home, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"type": "authorized_user", "refresh_token": SECRET_FRAGMENT}))
    assert cli.main(["login", str(bad)]) == 2
    output = capsys.readouterr()
    assert "service-account" in output.err and SECRET_FRAGMENT not in output.out + output.err
    assert not stored_key_path().exists()
