"""Browser profile, viewport, and proxy settings."""

from __future__ import annotations

import os
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Viewport(BaseModel):
    """A pixel size, used for both the page viewport and the virtual screen."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ProxyConfig(BaseModel):
    """Proxy settings for a session.

    ``username``/``password`` are held in memory and answered to the proxy's
    authentication challenge over CDP. They are never written to disk by
    WebSkrap, never passed on the command line, and the
    ``repr``/``str`` of this model redact them, so avoid logging even the
    redacted form alongside the server URL where possible.
    """

    server: str
    bypass: str | None = None
    username: str | None = None
    password: str | None = None

    @field_validator("server")
    @classmethod
    def validate_server(_cls, value: str) -> str:
        """Require a scheme Chromium accepts, so failures surface at config time.

        Raises:
            ValueError: If the server has no http/https/socks4/socks5 scheme,
                or embeds ``user:password@``. That would put the password on
                Chromium's command line, which other local users can read.
        """
        allowed_prefixes = ("http://", "https://", "socks4://", "socks5://")
        if not value.startswith(allowed_prefixes):
            msg = "proxy server must start with http://, https://, socks4://, or socks5://"
            raise ValueError(msg)
        if "@" in value.split("://", 1)[1].split("/", 1)[0]:
            msg = "proxy server must not embed user:password@; set username and password"
            raise ValueError(msg)
        return value

    def __repr__(self) -> str:
        """Return a repr with credentials redacted."""
        return (
            f"ProxyConfig(server={self.server!r}, bypass={self.bypass!r}, "
            "username='***', password='***')"
        )

    def __str__(self) -> str:
        """Return a string with credentials redacted."""
        return self.__repr__()

    def redacted(self) -> dict[str, str | None]:
        """Return a credential-redacted mapping for logs and diagnostics."""
        return {
            "server": self.server,
            "bypass": self.bypass,
            "username": "***" if self.username else None,
            "password": "***" if self.password else None,
        }


class BrowserProfile(BaseModel):
    """The identity a session presents: viewport, locale, timezone, headers.

    Bundled profiles come from :mod:`webskrap.profiles`. By default most of
    this is ignored on purpose, so the browser presents its real fingerprint;
    :attr:`SessionConfig.profile_mode` opts in to the locale parts or to all
    of it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    user_agent: str | None = None
    viewport: Viewport = Field(default_factory=lambda: Viewport(width=1365, height=768))
    screen: Viewport = Field(default_factory=lambda: Viewport(width=1440, height=900))
    locale: str = "en-US"
    timezone_id: str = "Europe/Paris"
    device_scale_factor: float = Field(default=1.0, gt=0)
    is_mobile: bool = False
    has_touch: bool = False
    color_scheme: Literal["dark", "light", "no-preference", "null"] = "light"
    reduced_motion: Literal["reduce", "no-preference", "null"] = "no-preference"
    extra_http_headers: dict[str, str] = Field(default_factory=dict)
    navigator_languages: list[str] = Field(default_factory=lambda: ["en-US", "en"])

    @field_validator("name")
    @classmethod
    def validate_name(_cls, value: str) -> str:
        """Reject a blank profile name.

        Raises:
            ValueError: If the name is empty or only whitespace.
        """
        if not value.strip():
            msg = "profile name cannot be empty"
            raise ValueError(msg)
        return value

    @field_validator("locale", "timezone_id")
    @classmethod
    def validate_non_empty(_cls, value: str) -> str:
        """Reject a blank locale or timezone.

        Raises:
            ValueError: If the value is empty or only whitespace.
        """
        if not value.strip():
            msg = "value cannot be empty"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def ensure_language_consistency(self) -> BrowserProfile:
        """Keep ``navigator_languages`` consistent with ``locale``.

        A locale missing from the language list is a fingerprint mismatch a
        detector can read, so the locale is inserted first when absent.
        """
        if not self.navigator_languages:
            self.navigator_languages = [self.locale]
        if self.locale not in self.navigator_languages:
            self.navigator_languages.insert(0, self.locale)
        return self

    def accept_language(self) -> str:
        """Return an ``Accept-Language`` header value with descending q-values."""
        languages = self.navigator_languages or [self.locale]
        weighted = [languages[0]]
        weighted.extend(
            f"{language};q={max(0.1, 1 - index * 0.1):.1f}"
            for index, language in enumerate(languages[1:], start=1)
        )
        return ",".join(weighted)

    def headers(self) -> dict[str, str]:
        """Return the default request headers, with ``extra_http_headers`` last."""
        headers = {
            "Accept-Language": self.accept_language(),
            "Upgrade-Insecure-Requests": "1",
        }
        headers.update(self.extra_http_headers)
        return headers


def native_profile_env(profile: BrowserProfile) -> dict[str, str]:
    """Return the browser environment that makes ``profile``'s locale native.

    ``TZ`` sets the timezone Chromium reports. ``LC_ALL``, ``LANG`` and
    ``LANGUAGE`` set its application locale, which is the default ``Intl``
    locale; ``LC_ALL`` is set too because a host value would otherwise win.
    The locale need not be installed on the host. The rest of the
    environment is inherited, since a given environment replaces the whole.

    Raises:
        ValueError: If ``profile.timezone_id`` is not a known IANA timezone.
    """
    try:
        ZoneInfo(profile.timezone_id)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = f"unknown timezone '{profile.timezone_id}': use an IANA name like Europe/Paris"
        raise ValueError(msg) from exc
    posix_locale = f"{profile.locale.replace('-', '_')}.UTF-8"
    languages = [language.replace("-", "_") for language in profile.navigator_languages]
    return {
        **os.environ,
        "TZ": profile.timezone_id,
        "LC_ALL": posix_locale,
        "LANG": posix_locale,
        "LANGUAGE": ":".join(languages),
    }
