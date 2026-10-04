"""Where plexlists keeps its files, and storage for the login secrets.

    <config dir>/config.json     device ID, chosen server, username (not secret)
    <config dir>/artwork.json    hashes of uploaded posters (not secret)
    <config dir>/playlists.json  when each playlist was created in Plex, per server (a cache)
    <config dir>/secrets.json    device key + token, ONLY with `login --store file`
    <config dir>/shows/*.toml    your own show definitions
    <config dir>/posters/<show>/ poster and background images

<config dir> is ~/Library/Application Support/plexlists on macOS and
~/.config/plexlists on Linux, or $PLEXLISTS_CONFIG_DIR if set. By default the
secrets live in the OS keychain (macOS Keychain / Linux Secret Service).
"""

from __future__ import annotations

import contextlib
import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer

APP_NAME = "plexlists"
KEYRING_SERVICE, KEYRING_USER = "plexlists", "device"

# The original single-file scripts (xfiles.py / tng.py). Logins are imported once.
LEGACY_APP_NAME = "xfiles"
LEGACY_KEYRING_SERVICE = "xfiles-plex"


class Store(StrEnum):
    keyring = "keyring"
    file = "file"


def app_dir() -> Path:
    override = os.environ.get("PLEXLISTS_CONFIG_DIR")
    return Path(override).expanduser() if override else Path(typer.get_app_dir(APP_NAME))


def config_file() -> Path:
    return app_dir() / "config.json"


def secrets_file() -> Path:
    return app_dir() / "secrets.json"


def artwork_state_file() -> Path:
    return app_dir() / "artwork.json"


def playlists_state_file() -> Path:
    return app_dir() / "playlists.json"


def user_shows_dir() -> Path:
    return app_dir() / "shows"


def posters_dir(slug: str) -> Path:
    return app_dir() / "posters" / slug


def ensure_private_dir(path: Path | None = None) -> Path:
    d = path or app_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    path.write_text(json.dumps(data, indent=2))


def load_config() -> dict[str, Any]:
    return read_json(config_file())


def save_config(cfg: dict[str, Any]) -> None:
    write_json(config_file(), cfg)


# --------------------------------------------------------------------- secrets


def keyring_backend() -> tuple[bool, str]:
    """(usable, description) for the OS keyring on this machine."""
    import keyring

    kr = keyring.get_keyring()
    mod = type(kr).__module__
    name = getattr(kr, "name", type(kr).__name__)
    usable = not any(bad in mod for bad in ("keyring.backends.fail", "keyring.backends.null"))
    return usable, str(name)


def _write_secret_file(path: Path, blob: str) -> None:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(blob)
    path.chmod(0o600)


def save_secrets(store: str, data: dict[str, Any]) -> None:
    blob = json.dumps(data)
    if store == Store.keyring:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, blob)
    else:
        _write_secret_file(secrets_file(), blob)


def load_secrets(store: str) -> dict[str, Any] | None:
    if store == Store.keyring:
        import keyring

        blob = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    else:
        path = secrets_file()
        blob = path.read_text() if path.exists() else None
    return json.loads(blob) if blob else None


def delete_secrets(store: str) -> None:
    if store == Store.keyring:
        import keyring
        from keyring.errors import PasswordDeleteError

        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    secrets_file().unlink(missing_ok=True)


# ------------------------------------------------------------------- migration


def import_legacy_login() -> bool:
    """Copy a login made by the old xfiles.py / tng.py scripts, once.

    Only runs when plexlists has no login of its own. The old entries are left
    in place, so the old scripts keep working until you delete them.
    """
    if config_file().exists() or os.environ.get("PLEXLISTS_CONFIG_DIR"):
        return False
    legacy_dir = Path(typer.get_app_dir(LEGACY_APP_NAME))
    legacy_cfg = read_json(legacy_dir / "config.json")
    if not legacy_cfg.get("client_id"):
        return False
    store = legacy_cfg.get("store", Store.keyring)
    try:
        if store == Store.keyring:
            import keyring

            blob = keyring.get_password(LEGACY_KEYRING_SERVICE, KEYRING_USER)
        else:
            legacy_secrets = legacy_dir / "secrets.json"
            blob = legacy_secrets.read_text() if legacy_secrets.exists() else None
    except Exception:
        return False
    if not blob:
        return False
    save_secrets(store, json.loads(blob))
    save_config(legacy_cfg)
    art = read_json(legacy_dir / "artwork.json")
    if art:
        write_json(artwork_state_file(), art)
    return True
