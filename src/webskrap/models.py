"""Pydantic models describing what to launch, how to browse, and what came back.

:class:`BrowserProfile` is the identity presented to a site (viewport, locale,
headers); :class:`SessionConfig` is the machinery behind it (driver, browser,
proxy, stealth switches, timeouts). Both translate themselves into Playwright
launch and context options, which is where their per-field comments matter.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Chromium's WebRTC ICE candidate policy; see
#: :attr:`SessionConfig.webrtc_ip_handling_policy`.
WebRtcIPHandlingPolicy = Literal[
    "default",
    "default_public_and_private_interfaces",
    "default_public_interface_only",
    "disable_non_proxied_udp",
]
#: Playwright load state a navigation waits for before returning.
WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
#: Load state an already-navigated page can be waited on. ``commit`` is absent
#: on purpose: it describes the start of a navigation, so there is nothing left
#: to wait for once the page exists.
LoadState = Literal["domcontentloaded", "load", "networkidle"]
#: Element state :func:`~webskrap.browser_session.wait_for` can wait on.
ElementState = Literal["attached", "detached", "visible", "hidden"]


class ResourcePolicy(StrEnum):
    """Which resource types a session is allowed to load.

    ``LITE`` blocks images, fonts and media; ``DOCUMENTS`` also blocks
    stylesheets. Both cut bandwidth and latency, and both are visible to a site
    that checks whether its assets loaded.
    """

    ALL = "all"
    LITE = "lite"
    DOCUMENTS = "documents"


class Viewport(BaseModel):
    """A pixel size, used for both the page viewport and the virtual screen."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)

    def to_playwright(self) -> dict[str, int]:
        """Return the ``{"width", "height"}`` mapping Playwright expects."""
        return {"width": self.width, "height": self.height}


class ProxyConfig(BaseModel):
    """Proxy settings for a session.

    ``username``/``password`` are held in memory and handed to Playwright at
    context creation. They are never written to disk by WebSkrap, but they do
    appear in this model's ``repr`` and ``model_dump``, so avoid logging it.
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
            ValueError: If the server has no http/https/socks4/socks5 scheme.
        """
        allowed_prefixes = ("http://", "https://", "socks4://", "socks5://")
        if not value.startswith(allowed_prefixes):
            msg = "proxy server must start with http://, https://, socks4://, or socks5://"
            raise ValueError(msg)
        return value

    def to_playwright(self) -> dict[str, str]:
        """Return Playwright proxy options, omitting unset fields."""
        payload = {"server": self.server}
        if self.bypass:
            payload["bypass"] = self.bypass
        if self.username:
            payload["username"] = self.username
        if self.password:
            payload["password"] = self.password
        return payload


class BrowserProfile(BaseModel):
    """The identity a session presents: viewport, locale, timezone, headers.

    Bundled profiles come from :mod:`webskrap.profiles`. Under the
    ``patchright`` driver most of this is ignored on purpose — the point of
    that driver is the browser's real fingerprint — unless
    :attr:`SessionConfig.patchright_context_profile` opts back in to the parts
    Chrome can own natively.
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
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": self.accept_language(),
            "Upgrade-Insecure-Requests": "1",
        }
        headers.update(self.extra_http_headers)
        return headers

    def to_context_options(self) -> dict[str, Any]:
        """Return this profile as Playwright ``new_context`` options."""
        options: dict[str, Any] = {
            "viewport": self.viewport.to_playwright(),
            "screen": self.screen.to_playwright(),
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "device_scale_factor": self.device_scale_factor,
            "is_mobile": self.is_mobile,
            "has_touch": self.has_touch,
            "color_scheme": self.color_scheme,
            "reduced_motion": self.reduced_motion,
            "extra_http_headers": self.headers(),
        }
        if self.user_agent:
            options["user_agent"] = self.user_agent
        return options


class SessionConfig(BaseModel):
    """How a session's browser is launched and how its context behaves.

    Defaults are deliberately plain: standard Playwright, headless Chromium, no
    proxy, everything loaded, and every stealth feature off. The stealth
    switches are documented at their fields, since each has a cost as well as a
    benefit.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    driver: Literal["playwright", "patchright"] = "playwright"
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    channel: str | None = None
    headless: bool = True
    user_data_dir: Path | None = None
    storage_state: Path | dict[str, Any] | None = None
    proxy: ProxyConfig | None = None
    resource_policy: ResourcePolicy = ResourcePolicy.ALL
    ignore_https_errors: bool = False
    java_script_enabled: bool = True
    service_workers: Literal["allow", "block"] = "allow"
    timeout_ms: float = Field(default=30_000, gt=0)
    navigation_timeout_ms: float = Field(default=30_000, gt=0)
    default_timeout_ms: float = Field(default=30_000, gt=0)
    slow_mo_ms: float | None = Field(default=None, ge=0)
    launch_args: list[str] = Field(default_factory=list)
    # Simulated screen for headless chromium. Headless Chrome has no physical
    # display, so screen/window metrics (screen.width, outerWidth, ...) leak as
    # headless tells. A virtual screen of this size is configured at launch via
    # browser flags (not JS spoofing), giving coherent metrics. Set to None to
    # disable.
    headless_screen: Viewport | None = Field(
        default_factory=lambda: Viewport(width=1920, height=1080)
    )
    # Headless Chrome stamps "HeadlessChrome" into navigator.userAgent (and the
    # worker UA), the one fingerprint tell that survives patchright. When True
    # for a headless chromium run, WebSkrap probes the real UA and re-applies it
    # with "HeadlessChrome" rewritten to "Chrome" via the browser's own UA
    # override (covering workers and client hints) — not JavaScript spoofing.
    # Off by default so headless stays honestly headless unless opted in.
    mask_headless_user_agent: bool = False
    # Native Chromium rendering reduction. When enabled, canvas readback and
    # WebGL are disabled through browser flags. This avoids high-entropy
    # rendering fingerprints without JavaScript monkeypatching, but can break
    # pages that require canvas exports or WebGL.
    reduce_fingerprint_surface: bool = False
    # Chromium WebRTC IP handling policy. Use "disable_non_proxied_udp" to
    # prevent non-proxied UDP ICE candidates, which avoids WebRTC exposing local
    # or direct public IP candidates on leak-test pages without patching the
    # RTCPeerConnection API.
    webrtc_ip_handling_policy: WebRtcIPHandlingPolicy | None = None
    # Patchright is strongest when it exposes the browser's native surfaces, so
    # profile settings are ignored by default. This opt-in applies only browser
    # context metadata that Chrome can own natively (locale, timezone, color
    # scheme, reduced motion, and caller-provided extra headers) while still
    # avoiding viewport, user-agent, and JavaScript fingerprint patches.
    patchright_context_profile: bool = False
    # Patchright-specific focus behavior control. Defaults to None so the option
    # is omitted entirely; Patchright builds that lack focus_control reject it.
    # Set True/False only when targeting a build that accepts the option.
    patchright_focus_control: bool | None = None
    # Click the reject control of a cookie consent notice after navigation, so
    # scraped text is not buried under a banner and no optional cookies are
    # accepted. Only applies to pages fetched through WebSkrapSession.fetch;
    # pages the caller drives itself are untouched.
    decline_cookies: bool = False
    # How long to wait for a consent notice to appear before giving up. CMP
    # scripts inject the notice after DOMContentLoaded, so an immediate check
    # misses most of them. This is the per-fetch cost on pages without a
    # notice; set 0 for a single immediate check.
    decline_cookies_timeout_ms: float = Field(default=2_000, ge=0)

    def launch_options(self) -> dict[str, Any]:
        """Return Playwright launch options, including assembled browser flags.

        Caller-supplied ``launch_args`` win: a flag already present there is
        not added again by the automation, screen, WebRTC or fingerprint
        helpers.
        """
        options: dict[str, Any] = {
            "headless": self.headless,
            "timeout": self.timeout_ms,
        }
        if self.channel:
            options["channel"] = self.channel
        if self.slow_mo_ms is not None:
            options["slow_mo"] = self.slow_mo_ms
        args = (
            self._automation_args()
            + self._screen_args()
            + self._reduced_fingerprint_surface_args()
            + self._webrtc_ip_handling_args()
            + list(self.launch_args)
        )
        if args:
            options["args"] = args
        return options

    def _automation_args(self) -> list[str]:
        # Standard Playwright Chrome exposes navigator.webdriver=true under
        # automation, an instant tell for detectors like DataDome. Patchright
        # owns this surface itself, and extra evasion flags can become signals.
        # Skipped if the caller already passed it.
        if self.browser != "chromium" or self.driver == "patchright":
            return []
        flag = "--disable-blink-features=AutomationControlled"
        if any(a.startswith("--disable-blink-features") for a in self.launch_args):
            return []
        return [flag]

    def _screen_args(self) -> list[str]:
        # Configure a virtual screen for headless chromium so the browser
        # reports real display metrics instead of headless defaults. Skips any
        # flag the caller already set in launch_args (their value wins).
        if not (self.headless and self.browser == "chromium" and self.headless_screen):
            return []
        width, height = self.headless_screen.width, self.headless_screen.height
        candidates = {
            "--window-size": f"--window-size={width},{height}",
            "--window-position": "--window-position=0,0",
            # Chrome headless virtual-display flag: defines a screen at 0,0.
            "--screen-info": f"--screen-info={{{width}x{height}}}",
        }
        return [
            arg
            for prefix, arg in candidates.items()
            if not any(a.startswith(prefix) for a in self.launch_args)
        ]

    def _webrtc_ip_handling_args(self) -> list[str]:
        # Chromium requires the policy value and an explicit force flag for the
        # process-level WebRTC IP handling override to apply in Chrome.
        if self.browser != "chromium" or self.webrtc_ip_handling_policy is None:
            return []
        candidates = {
            "--webrtc-ip-handling-policy": (
                f"--webrtc-ip-handling-policy={self.webrtc_ip_handling_policy}"
            ),
            "--force-webrtc-ip-handling-policy": "--force-webrtc-ip-handling-policy",
        }
        return [
            arg
            for prefix, arg in candidates.items()
            if not any(a == prefix or a.startswith(f"{prefix}=") for a in self.launch_args)
        ]

    def _reduced_fingerprint_surface_args(self) -> list[str]:
        if self.browser != "chromium" or not self.reduce_fingerprint_surface:
            return []
        candidates = {
            "--disable-webgl": "--disable-webgl",
            "--disable-reading-from-canvas": "--disable-reading-from-canvas",
        }
        return [
            arg
            for prefix, arg in candidates.items()
            if not any(a == prefix or a.startswith(f"{prefix}=") for a in self.launch_args)
        ]

    def context_options(self, profile: BrowserProfile) -> dict[str, Any]:
        """Return Playwright context options for ``profile`` under this config.

        The ``patchright`` driver keeps the browser's native fingerprint and
        applies almost nothing from the profile; every other driver applies it
        in full. ``storage_state`` is dropped when ``user_data_dir`` is set,
        because a persistent profile already carries that state.
        """
        if self.driver == "patchright":
            # patchright defeats CDP-aware detectors by presenting the browser's
            # real fingerprint. The default keeps the host/browser environment
            # visible. The opt-in context profile below only applies settings
            # Chrome can expose natively through BrowserContext options.
            options: dict[str, Any] = {"no_viewport": True}
            if self.patchright_focus_control is not None:
                options["focus_control"] = self.patchright_focus_control
            if self.patchright_context_profile:
                options.update(
                    {
                        "locale": profile.locale,
                        "timezone_id": profile.timezone_id,
                        "color_scheme": profile.color_scheme,
                        "reduced_motion": profile.reduced_motion,
                    }
                )
                if profile.extra_http_headers:
                    options["extra_http_headers"] = dict(profile.extra_http_headers)
        else:
            options = profile.to_context_options()
        options.update(
            {
                "ignore_https_errors": self.ignore_https_errors,
                "java_script_enabled": self.java_script_enabled,
                "service_workers": self.service_workers,
            }
        )
        if self.proxy:
            options["proxy"] = self.proxy.to_playwright()
        if self.storage_state is not None and self.user_data_dir is None:
            options["storage_state"] = self.storage_state
        return options


class Link(BaseModel):
    """One outbound anchor: its resolved absolute URL and its visible label."""

    href: str
    text: str


class FetchResult(BaseModel):
    """What one fetch produced.

    ``text`` is page HTML unless the fetch asked for ``text_only``. ``ok`` is
    an HTTP-status judgement (2xx/3xx), not a promise that the page is what you
    wanted. ``cookies`` are the whole context's, not just this page's.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    final_url: str
    status: int | None
    ok: bool
    headers: dict[str, str]
    text: str
    title: str
    cookies: list[dict[str, Any]]
    timings: dict[str, float]
    screenshot_path: Path | None = None
    # Strategy that dismissed a cookie consent notice ("cmp" or "text"), or
    # None when nothing was declined.
    cookie_notice_declined: str | None = None
    # Outbound links, collected only when the fetch asked for them. Empty and
    # zero otherwise, so an empty list does not distinguish "no links" from
    # "not requested" -- ``links_total`` is the count before the cap applied.
    links: list[Link] = Field(default_factory=list)
    links_total: int = 0


class TextWindow(NamedTuple):
    """One bounded slice of a longer string, plus how to ask for the next.

    Attributes:
        text: The slice itself.
        length: Length of the whole string the slice came from.
        offset: Index the slice starts at.
        truncated: True when text remains after the slice.
        next_offset: Offset to pass back for the following slice, or None at
            the end. A caller reads a long page by following this until it is
            None, instead of re-fetching with a larger limit.
    """

    text: str
    length: int
    offset: int
    truncated: bool
    next_offset: int | None


def text_window(text: str, max_chars: int, offset: int = 0) -> TextWindow:
    """Return the ``max_chars`` characters of ``text`` starting at ``offset``.

    Both arguments are clamped rather than rejected: a negative limit reads as
    0, and an offset past the end yields an empty, non-truncated window. This
    is the paging behavior behind every bounded ``text`` and ``snapshot`` field
    the CLI and MCP tools return. A limit of 0 consumes nothing, so
    ``next_offset`` repeats ``offset`` rather than advancing.

    Args:
        text: The full string to slice.
        max_chars: Maximum characters to return.
        offset: Index to start at.
    """
    length = len(text)
    limit = max(0, max_chars)
    start = min(max(0, offset), length)
    end = min(start + limit, length)
    truncated = end < length
    return TextWindow(
        text=text[start:end],
        length=length,
        offset=start,
        truncated=truncated,
        next_offset=end if truncated else None,
    )


def shape_fetch_result(result: FetchResult, max_chars: int, offset: int = 0) -> dict[str, Any]:
    """Flatten a result into the JSON payload the CLI and MCP tools return.

    Text is windowed to ``max_chars`` characters from ``offset``.
    ``text_length``, ``text_offset`` and ``text_truncated`` report what was cut,
    and ``next_text_offset`` is what to pass back for the rest, so a clipped
    page can be read through rather than fetched again. ``links`` is empty
    unless the fetch collected them. Cookies are left out.
    """
    window = text_window(result.text or "", max_chars, offset)
    return {
        "url": result.url,
        "final_url": result.final_url,
        "status": result.status,
        "ok": result.ok,
        "title": result.title,
        "headers": result.headers,
        "text": window.text,
        "text_length": window.length,
        "text_offset": window.offset,
        "text_truncated": window.truncated,
        "next_text_offset": window.next_offset,
        "links": [link.model_dump() for link in result.links],
        "links_total": result.links_total,
        "links_truncated": result.links_total > len(result.links),
        "elapsed_ms": round(result.timings.get("elapsed_ms", 0.0), 1),
        "cookie_notice_declined": result.cookie_notice_declined,
    }
