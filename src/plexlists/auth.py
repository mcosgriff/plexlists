"""Plex authentication and server connection.

Plex JWT device flow (https://developer.plex.tv/pms/#section/API-Info/Authenticating-with-Plex):

* `login` generates an Ed25519 keypair locally and registers the public key
  with plex.tv; you approve the device in your browser.
* Plex issues a JWT valid for 7 days. Renewing it requires signing a plex.tv
  nonce with the private key, so a leaked JWT dies within a week and can't be
  renewed without the key.
* The private key and current JWT are stored as one entry in the OS keychain.
* The server is discovered through your account and reached over HTTPS via
  its *.plex.direct certificate.
"""

from __future__ import annotations

import base64
import contextlib
import platform
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from plexlists import config

DEVICE_NAME = "plexlists"
JWT_SCOPES = ["username"]  # least privilege: we only display the username
LOCALHOST = {"localhost", "127.0.0.1", "::1"}


class AuthError(Exception):
    """Something the user can fix. The message is shown as-is (may contain rich markup)."""


def plex_headers(client_id: str) -> dict[str, str]:
    return {
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": "plexlists",
        "X-Plex-Version": "1.0",
        "X-Plex-Device": platform.system(),
        "X-Plex-Device-Name": f"{DEVICE_NAME} ({socket.gethostname()})",
        "X-Plex-Platform": platform.system(),
        "X-Plex-Platform-Version": platform.release(),
    }


def use_device(client_id: str) -> None:
    """Make every plexapi request identify as this device."""
    import plexapi

    plexapi.BASE_HEADERS.update(plex_headers(client_id))


def jwt_login(client_id: str, secrets: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    from plexapi.myplex import MyPlexJWTLogin

    keypair: tuple[bytes | None, bytes | None] = (None, None)
    if secrets:
        keypair = (
            base64.b64decode(secrets["private_key"]),
            base64.b64decode(secrets["public_key"]),
        )
        kwargs.setdefault("jwtToken", secrets.get("jwt"))
    return MyPlexJWTLogin(
        headers=plex_headers(client_id), keypair=keypair, scopes=JWT_SCOPES, **kwargs
    )


def jwt_expiry(token: str) -> datetime | None:
    import jwt

    try:
        exp = jwt.decode(token, options={"verify_signature": False}).get("exp")
    except jwt.InvalidTokenError:
        return None
    return datetime.fromtimestamp(exp, UTC) if exp else None


def fresh_jwt(cfg: dict[str, Any], secrets: dict[str, Any]) -> str:
    """Return a valid JWT, renewing (and re-saving) it if it's expired or expires within a day."""
    jl = jwt_login(cfg["client_id"], secrets)
    if jl.verifyJWT(refreshWithinDays=1):
        return jl.jwtToken
    try:
        token = jl.refreshJWT()
    except Exception as exc:
        raise AuthError(
            "Couldn't renew the Plex token. The device may have been revoked at plex.tv.\n"
            "Run [cyan]plexlists login[/cyan] again."
        ) from exc
    secrets["jwt"] = token
    config.save_secrets(cfg["store"], secrets)
    return token


def logged_in() -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = config.load_config()
    secrets = config.load_secrets(cfg["store"]) if cfg.get("client_id") else None
    if not secrets:
        raise AuthError("Not logged in. Run [cyan]plexlists login[/cyan] first.")
    use_device(cfg["client_id"])
    return cfg, secrets


def server_resources(account: Any) -> list[Any]:
    return [r for r in account.resources() if "server" in (r.provides or "").split(",")]


def is_jwt(token: str | None) -> bool:
    return bool(token) and token is not None and token.startswith("eyJ") and token.count(".") == 2


def device_legacy_token(account: Any, client_id: str) -> str | None:
    """This device's legacy (non-JWT) token, looked up from the account's device list.

    Plex Media Server doesn't accept JWTs yet, and plex.tv's resources endpoint
    just echoes the JWT back as the server token when you authenticate with one.
    The devices endpoint always returns legacy tokens, so we look up *this*
    device's entry (never the server's own token). It's fetched fresh on each
    run, held only in memory, and dies when the device is revoked.
    See: https://forums.plex.tv/t/question-on-https-clients-plex-tv-api-v2-resources-and-jwt-authentication/934478
    """
    try:  # v1: https://plex.tv/devices.xml (via plexapi)
        tok = next((d.token for d in account.devices() if d.clientIdentifier == client_id), None)
        if tok and not is_jwt(tok):
            return tok
    except Exception:
        pass
    try:  # v2: https://clients.plex.tv/api/v2/devices (JSON or XML)
        data = account.query("https://clients.plex.tv/api/v2/devices")
        if isinstance(data, list):
            entries = data
        elif data is not None and not isinstance(data, dict):
            entries = [e.attrib for e in data]
        else:
            entries = []
        for e in entries:
            tok = e.get("token")
            if e.get("clientIdentifier") == client_id and tok and not is_jwt(tok):
                return tok
    except Exception:
        pass
    return None


def server_tokens(account: Any, resource: Any, jwt_token: str, client_id: str) -> list[str]:
    """Credentials to try against the server, in order.

    1. This device's legacy token. It's what current Plex Media Server versions accept.
    2. The server access token from plex.tv, if it's a real legacy token.
    3. The JWT itself, for future server versions that accept JWTs directly.

    Only the JWT is ever written to disk.
    """
    candidates = [
        device_legacy_token(account, client_id),
        None if is_jwt(resource.accessToken) else resource.accessToken,
        jwt_token,
    ]
    tokens: list[str] = []
    for t in candidates:
        if t and t not in tokens:
            tokens.append(t)
    return tokens


def open_server(uri: str, tokens: list[str]) -> Any:
    """Open a PlexServer at uri, trying each token until one isn't rejected."""
    from plexapi.exceptions import Unauthorized
    from plexapi.server import PlexServer

    for i, token in enumerate(tokens):
        try:
            return PlexServer(uri, token, timeout=10)
        except Unauthorized:
            if i == len(tokens) - 1:
                raise
    raise AuthError("No credentials to try.")


def connect_https(resource: Any, tokens: list[str]) -> Any:
    """Connect over HTTPS only (*.plex.direct certs), preferring local, then remote, then relay."""
    from plexapi.exceptions import Unauthorized

    conns = sorted(
        (c for c in resource.connections if c.protocol == "https"),
        key=lambda c: (bool(c.relay), not c.local),
    )
    if not conns:
        raise AuthError(
            f"'{resource.name}' doesn't advertise any HTTPS addresses.\n"
            "In Plex, set Settings → Network → Secure connections to Preferred or Required."
        )
    rejected, unreachable = [], []
    for c in conns:
        try:
            return open_server(c.uri, tokens)
        except Unauthorized:
            rejected.append(c.uri)
        except Exception as exc:
            unreachable.append(f"  • {c.uri}: {type(exc).__name__}")
    if rejected:
        kinds = ", ".join("JWT" if is_jwt(t) else "legacy device token" for t in tokens)
        hint = (
            "  • plex.tv didn't return a legacy token for this device, and this server "
            "doesn't accept JWTs yet.\n"
            if all(is_jwt(t) for t in tokens)
            else ""
        )
        raise AuthError(
            f"'{resource.name}' was reachable over HTTPS but rejected this device's credentials "
            f"with 401 Unauthorized. Tried: {kinds}.\n"
            + hint
            + "  • Run [cyan]plexlists login[/cyan] again to re-authorize.\n"
            "  • If the server is shared with you, check the owner hasn't removed your access."
        )
    raise AuthError(
        f"Couldn't reach '{resource.name}' over HTTPS:\n" + "\n".join(unreachable) + "\n"
        "Likely causes:\n"
        "  • The server is off or asleep.\n"
        "  • Your router's DNS rebinding protection blocks *.plex.direct names. "
        "Allow plex.direct in the router.\n"
        "  • As a workaround, pass the server address with --url."
    )


def plaintext_warning(url: str) -> str | None:
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme == "http" and u.hostname not in LOCALHOST:
        return (
            "Warning: connecting over plain HTTP, so your token is sent unencrypted.\n"
            "Omit --url to auto-connect over HTTPS, or use the server's "
            "https://….plex.direct:32400 address."
        )
    return None


def connect(url: str | None, token: str | None, warn: Any = print) -> Any:
    """Resolve how to reach Plex: legacy token if given, otherwise the login session."""
    from plexapi.myplex import MyPlexAccount
    from plexapi.server import PlexServer

    if token:
        if not url:
            raise AuthError(
                "A token was given via --token/PLEX_TOKEN but no server URL.\n"
                "Set PLEX_URL too, or unset PLEX_TOKEN and use [cyan]login[/cyan]."
            )
        warn("Using a legacy token from --token/PLEX_TOKEN. `plexlists login` is more secure.")
        if msg := plaintext_warning(url):
            warn(msg)
        return PlexServer(url, token, timeout=15)

    cfg, secrets = logged_in()
    jwt_token = fresh_jwt(cfg, secrets)
    account = MyPlexAccount(token=jwt_token)
    res = next(
        (r for r in server_resources(account) if r.clientIdentifier == cfg["server_id"]), None
    )
    if res is None:
        raise AuthError(
            f"Server '{cfg.get('server_name')}' is no longer on your account. "
            "Run [cyan]plexlists login[/cyan] again to pick a server."
        )
    tokens = server_tokens(account, res, jwt_token, cfg["client_id"])
    if url:
        if msg := plaintext_warning(url):
            warn(msg)
        return open_server(url, tokens)
    return connect_https(res, tokens)


def revoke_device(cfg: dict[str, Any], secrets: dict[str, Any]) -> bool:
    """Remove this device from the Plex account (Authorized Devices). Best effort."""
    from plexapi.myplex import MyPlexAccount

    try:
        use_device(cfg["client_id"])
        account = MyPlexAccount(token=fresh_jwt(cfg, secrets))
        account.device(clientId=cfg["client_id"]).delete()
        return True
    except Exception:
        return False


def new_keypair() -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {
        "private_key": base64.b64encode(priv).decode(),
        "public_key": base64.b64encode(pub).decode(),
    }


# ------------------------------------------------------------------ login flow
# Split into steps so the CLI and the TUI can each drive the interaction.


@dataclass
class PendingLogin:
    client_id: str
    secrets: dict[str, Any]
    jl: Any  # MyPlexJWTLogin, polling plex.tv in a background thread
    url: str


def forget_previous_login() -> None:
    """Revoke and delete any existing login so a new one replaces it cleanly."""
    old_cfg = config.load_config()
    if not old_cfg.get("client_id"):
        return
    old_store = old_cfg.get("store", config.Store.keyring)
    old_secrets = config.load_secrets(old_store)
    if old_secrets:
        revoke_device(old_cfg, old_secrets)
    config.delete_secrets(old_store)


def begin_login(timeout: int = 300) -> PendingLogin:
    """Register a new device key with plex.tv. Returns the URL to approve it at."""
    forget_previous_login()
    client_id = str(uuid.uuid4())
    use_device(client_id)
    secrets: dict[str, Any] = new_keypair()
    try:
        jl = jwt_login(client_id, secrets, oauth=True, jwtToken=None)
        jl.run(timeout=timeout)
        url = jl.oauthUrl()
    except Exception as exc:
        raise AuthError(f"Couldn't reach plex.tv: {exc}") from exc
    return PendingLogin(client_id, secrets, jl, url)


def wait_for_approval(pending: PendingLogin) -> tuple[Any, list[Any]]:
    """Block until the device is approved. Returns (account, servers on the account)."""
    from plexapi.myplex import MyPlexAccount

    if not pending.jl.waitForLogin():
        raise AuthError("Login wasn't approved in time. Try again.")
    pending.secrets["jwt"] = pending.jl.jwtToken
    account = MyPlexAccount(token=pending.jl.jwtToken)
    servers = server_resources(account)
    if not servers:
        raise AuthError("No Plex Media Servers found on this account.")
    return account, servers


def cancel_login(pending: PendingLogin) -> None:
    with contextlib.suppress(Exception):
        pending.jl.stop()


def complete_login(
    pending: PendingLogin, account: Any, resource: Any, store: str
) -> dict[str, Any]:
    """Save the approved login. Returns the new config."""
    config.save_secrets(store, pending.secrets)
    cfg = {
        "client_id": pending.client_id,
        "store": store,
        "server_id": resource.clientIdentifier,
        "server_name": resource.name,
        "username": account.username,
    }
    config.save_config(cfg)
    return cfg


def check_connection(pending: PendingLogin, account: Any, resource: Any) -> Any:
    """Connect to the chosen server over HTTPS (raises AuthError with advice if not)."""
    tokens = server_tokens(account, resource, pending.jl.jwtToken, pending.client_id)
    return connect_https(resource, tokens)


def logout(keep_device: bool = False) -> str:
    """Delete local credentials (and revoke the device unless keep_device). Returns a message."""
    cfg = config.load_config()
    if not cfg.get("client_id"):
        return "Not logged in."
    store = cfg.get("store", config.Store.keyring)
    secrets = config.load_secrets(store)
    msg = "Local credentials deleted."
    if secrets and not keep_device:
        if revoke_device(cfg, secrets):
            msg = "Device revoked at plex.tv and local credentials deleted."
        else:
            msg = (
                "Local credentials deleted, but revoking at plex.tv failed. Remove it in "
                "Plex: Account → Authorized Devices."
            )
    config.delete_secrets(store)
    config.config_file().unlink(missing_ok=True)
    return msg
