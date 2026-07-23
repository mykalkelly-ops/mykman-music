import os
import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import jwt


TEAM_ID_ENV = "APPLE_TEAM_ID"
KEY_ID_ENV = "APPLE_KEY_ID"
PRIVATE_KEY_ENV = "APPLE_PRIVATE_KEY"
PRIVATE_KEY_PATH_ENV = "APPLE_PRIVATE_KEY_PATH"


@dataclass(frozen=True)
class AppleMusicConfig:
    team_id: str | None
    key_id: str | None
    private_key: str | None

    @property
    def configured(self) -> bool:
        return bool(self.team_id and self.key_id and self.private_key)

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.team_id:
            missing.append(TEAM_ID_ENV)
        if not self.key_id:
            missing.append(KEY_ID_ENV)
        if not self.private_key:
            missing.append(f"{PRIVATE_KEY_ENV} or {PRIVATE_KEY_PATH_ENV}")
        return missing


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1].strip()
    return value or None


def _private_key_from_env(value: str | None) -> str | None:
    value = _clean_env(value)
    if not value:
        return None
    return value.replace("\\n", "\n")


def _private_key_from_path(value: str | None) -> str | None:
    value = _clean_env(value)
    if not value:
        return None
    try:
        return Path(value).expanduser().read_text(encoding="utf-8").strip()
    except Exception:
        return None


def get_config() -> AppleMusicConfig:
    private_key = _private_key_from_env(os.environ.get(PRIVATE_KEY_ENV))
    if not private_key:
        private_key = _private_key_from_path(os.environ.get(PRIVATE_KEY_PATH_ENV))
    return AppleMusicConfig(
        team_id=_clean_env(os.environ.get(TEAM_ID_ENV)),
        key_id=_clean_env(os.environ.get(KEY_ID_ENV)),
        private_key=private_key,
    )


def generate_developer_token(
    config: AppleMusicConfig | None = None,
    ttl_seconds: int = 3600,
    origins: list[str] | None = None,
) -> str:
    """Create an Apple Music developer token signed with the MusicKit private key."""
    config = config or get_config()
    if not config.configured:
        missing = ", ".join(config.missing)
        raise RuntimeError(f"Apple Music credentials are missing: {missing}")

    now = int(time.time())
    payload = {
        "iss": config.team_id,
        "iat": now,
        "exp": now + max(60, int(ttl_seconds)),
    }
    clean_origins = [origin.strip() for origin in (origins or []) if origin and origin.strip()]
    if clean_origins:
        payload["origin"] = clean_origins
    headers = {
        "alg": "ES256",
        "kid": config.key_id,
    }
    return jwt.encode(payload, config.private_key, algorithm="ES256", headers=headers)


def _apple_get(path: str, params: dict | None = None) -> dict:
    token = generate_developer_token(ttl_seconds=3600)
    query = f"?{urlencode(params or {}, doseq=True)}" if params else ""
    url = f"https://api.music.apple.com{path}{query}"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def search_catalog_artists(term: str, storefront: str = "us", limit: int = 5) -> list[dict]:
    data = _apple_get(
        f"/v1/catalog/{storefront}/search",
        {"term": term, "types": "artists", "limit": max(1, min(limit, 25))},
    )
    return ((data.get("results") or {}).get("artists") or {}).get("data") or []


def get_catalog_artist(artist_id: str, storefront: str = "us") -> dict | None:
    data = _apple_get(f"/v1/catalog/{storefront}/artists/{artist_id}")
    rows = data.get("data") or []
    return rows[0] if rows else None


def catalog_artist_albums(artist_id: str, storefront: str = "us", limit: int = 100) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        data = _apple_get(
            f"/v1/catalog/{storefront}/artists/{artist_id}/albums",
            {"limit": max(1, min(limit, 100)), "offset": offset},
        )
        rows = data.get("data") or []
        out.extend(rows)
        if not rows or not data.get("next"):
            break
        offset += len(rows)
    return out
