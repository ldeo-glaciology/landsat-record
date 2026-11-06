"""The Google service-account key used to publish, kept where only you can read it.

The key's contents are never taken on the command line, printed, logged or written anywhere but the one stored copy.
Only paths to it are passed around.
"""
import json
import os
from pathlib import Path

PERMISSIONS = ["storage.objects.list", "storage.objects.create", "storage.objects.delete"]  # delete: to overwrite


class KeyFileError(ValueError):
    """A key file that is missing or isn't a service-account key."""


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "landsat-record"


def stored_key_path():
    return config_dir() / "gcs-key.json"


def read_service_account(path):
    """The parsed key, if path is a service-account key file. Errors name what's wrong, never what's in the file."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise KeyFileError(f"no key file at {path}")
    try:
        key = json.loads(path.read_text())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise KeyFileError(f"{path} isn't a JSON key file") from None
    if not isinstance(key, dict) or key.get("type") != "service_account":
        raise KeyFileError(f"{path} isn't a Google service-account key (its \"type\" should be \"service_account\")")
    missing = [field for field in ("client_email", "private_key") if not key.get(field)]
    if missing:
        raise KeyFileError(f"{path} is missing {', '.join(missing)}")
    return key


def client_email(path):
    return read_service_account(path)["client_email"]


def inside_git_repo(path):
    """The git working tree a file sits in, or None."""
    for folder in Path(path).expanduser().resolve().parents:
        if (folder / ".git").exists():
            return folder
    return None


def store_key(source):
    """Copy a key to the config folder, readable only by you. Returns the stored path."""
    read_service_account(source)  # check it before keeping it
    folder = config_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    target = stored_key_path()
    temporary = target.with_suffix(".tmp")
    if temporary.exists():
        temporary.unlink()
    # Created with 0600 from the start, so it is never readable by others, even briefly
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as f:
        f.write(Path(source).expanduser().read_bytes())
    os.replace(temporary, target)
    return target


def remove_stored_key():
    """Delete the stored key. Returns whether there was one."""
    target = stored_key_path()
    if target.exists():
        target.unlink()
        return True
    return False


def find_key(explicit=None):
    """The key to publish with: an explicit path, then the stored key, then GOOGLE_APPLICATION_CREDENTIALS. None if
    there isn't one."""
    if explicit:
        read_service_account(explicit)
        return Path(explicit).expanduser()
    if stored_key_path().exists():
        return stored_key_path()
    from_environment = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if from_environment:
        read_service_account(from_environment)
        return Path(from_environment).expanduser()
    return None


def require_key(explicit=None):
    """find_key, or an error saying how to store a key."""
    key = find_key(explicit)
    if key is None:
        raise KeyFileError("no key to publish with. Run `landsat-record login KEYFILE` once, or pass --key-file")
    return key


def bucket_of(url):
    return url.removeprefix("gs://").split("/")[0]


def missing_permissions(key_path, bucket):
    """The permissions to publish that this key lacks on a bucket, from GCS's read-only testIamPermissions call.
    An empty list means it can publish. Nothing is written to the bucket."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        str(key_path), scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
    )
    response = AuthorizedSession(credentials).get(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/iam/testPermissions",
        params={"permissions": PERMISSIONS},
        timeout=30,
    )
    if response.status_code == 404:
        raise KeyFileError(f"there's no bucket called {bucket}")
    response.raise_for_status()
    granted = set(response.json().get("permissions", []))
    return [permission for permission in PERMISSIONS if permission not in granted]
