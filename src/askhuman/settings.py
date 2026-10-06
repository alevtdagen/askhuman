"""Server credentials are generated once and kept outside source control."""

import json
import os
import secrets
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


def data_directory() -> Path:
    return Path(os.environ.get("ASKHUMAN_DATA_DIR", ".askhuman")).expanduser()


class Settings(BaseModel):
    data_dir: Path
    base_url: str = "http://127.0.0.1:8765"
    api_key: str = Field(min_length=24)
    admin_key: str = Field(min_length=24)
    signing_key: str = Field(min_length=24)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        from urllib.parse import urlparse

        url = urlparse(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.username:
            raise ValueError("Base URL must be an HTTP(S) origin, without credentials")
        if url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("Base URL must be an origin without path, query, or fragment")
        return value.rstrip("/")

    @classmethod
    def load(cls, directory: Path | None = None):
        directory = directory or data_directory()
        path = directory / "settings.json"
        if not path.exists():
            raise FileNotFoundError("Run `askhuman init` first, or set ASKHUMAN_DATA_DIR")
        data = json.loads(path.read_text())
        for name in ("base_url", "api_key", "admin_key", "signing_key"):
            data[name] = os.environ.get(f"ASKHUMAN_{name.upper()}", data[name])
        return cls(data_dir=directory, **data)

    @classmethod
    def initialize(cls, directory: Path, base_url: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings = cls(
            data_dir=directory,
            base_url=base_url,
            api_key=secrets.token_urlsafe(32),
            admin_key=secrets.token_urlsafe(32),
            signing_key=secrets.token_urlsafe(32),
        )
        path = directory / "settings.json"
        # Exclusive creation preserves existing installations and credentials.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(settings.model_dump_json(exclude={"data_dir"}, indent=2))
        return settings
