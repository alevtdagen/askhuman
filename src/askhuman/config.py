"""Human-owned routing and delivery configuration; agents cannot change it."""

import os
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

ChannelType = Literal[
    "web",
    "slack",
    "teams",
    "discord",
    "telegram",
    "email",
    "sms",
    "whatsapp",
    "webhook",
]
FIELDS = {
    "web": [],
    "slack": ["url"],
    "teams": ["url"],
    "discord": ["url"],
    "telegram": ["bot_token", "chat_id"],
    "email": ["host", "port", "username", "password", "from", "to", "security"],
    "sms": ["account_sid", "auth_token", "from", "to"],
    "whatsapp": ["account_sid", "auth_token", "from", "to"],
    "webhook": ["url", "secret"],
}
OPTIONAL = {"port", "username", "password", "security"}


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    type: ChannelType
    enabled: bool = True
    settings: dict[str, str] = Field(default_factory=dict, max_length=12)

    @model_validator(mode="after")
    def validate_settings(self):
        allowed = FIELDS[self.type]
        if set(self.settings) - set(allowed):
            raise ValueError(f"Supported settings for {self.type}: {', '.join(allowed)}")
        if not self.enabled:
            return self
        for key in allowed:
            if key not in OPTIONAL and not self.settings.get(key, "").strip():
                raise ValueError(f"{self.type} requires {key}")
        for key, value in self.settings.items():
            if len(value) > 4096 or "\n" in value or "\r" in value:
                raise ValueError(f"Invalid value for {key}")
        url = self.settings.get("url", "")
        if url and not url.startswith("env:"):
            validate_destination(url)
        if self.type == "email":
            security = self.settings.get("security", "starttls")
            if not security.startswith("env:") and security not in ("starttls", "tls"):
                raise ValueError("Email security must be starttls or tls")
            port = self.settings.get("port", "587")
            if not port.startswith("env:") and (not port.isdigit() or not 1 <= int(port) <= 65535):
                raise ValueError("Invalid SMTP port")
        return self

    def resolved(self) -> dict[str, str]:
        values = {}
        for key, value in self.settings.items():
            if value.startswith("env:"):
                variable = value[4:]
                if not os.environ.get(variable):
                    raise ValueError(f"Missing environment variable for {key}")
                value = os.environ[variable]
            values[key] = value
        # Validate resolved values too, without logging them.
        Channel(id=self.id, type=self.type, enabled=True, settings=values)
        return values


def validate_destination(value: str):
    url = urlparse(value)
    if url.scheme != "https" or not url.hostname or url.username or url.fragment:
        raise ValueError("Delivery URLs must use HTTPS and cannot contain credentials or fragments")


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: list[Channel] = Field(
        default_factory=lambda: [Channel(id="inbox", type="web")],
        min_length=1,
        max_length=50,
    )
    default_channels: list[str] = Field(default_factory=lambda: ["inbox"], min_length=1)
    routes: dict[str, list[str]] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def valid_routes(self):
        ids = {channel.id for channel in self.channels}
        enabled = {channel.id for channel in self.channels if channel.enabled}
        if len(ids) != len(self.channels):
            raise ValueError("Channel IDs must be unique")
        for route in [self.default_channels, *self.routes.values()]:
            if not route or len(set(route)) != len(route) or set(route) - enabled:
                raise ValueError("Routes must contain unique, enabled channel IDs")
        if any(not name.strip() or len(name) > 120 for name in self.routes):
            raise ValueError("Recipient names must contain 1–120 characters")
        return self

    def targets(self, recipient: str | None) -> list[str]:
        # A typo in an explicit recipient must not disclose a question to the default route.
        if recipient is not None and recipient not in self.routes:
            raise ValueError(f"No human-configured route for recipient: {recipient}")
        return self.routes[recipient] if recipient else self.default_channels
