"""Pydantic models describing what to launch, how to browse, and what came back.

:class:`BrowserProfile` is the identity presented to a site (viewport, locale,
headers); :class:`SessionConfig` is the machinery behind it (channel, proxy,
stealth switches, timeouts). Both translate themselves into Chromium flags and
CDP commands, which is where their per-field comments matter.
"""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
#: How much of a BrowserProfile pages see; see :attr:`SessionConfig.profile_mode`.
ProfileMode = Literal["native", "locale", "full"]


class ResourcePolicy(StrEnum):
    """Which resource types a session is allowed to load.

    ``LITE`` blocks images, fonts and media; ``DOCUMENTS`` also blocks
    stylesheets. Both cut bandwidth and latency, and both are visible to a site
    that checks whether its assets loaded.
    """

    ALL = "all"
    LITE = "lite"
    DOCUMENTS = "documents"


class GpuBackend(StrEnum):
    """Which GL implementation WebGL renders with; see :attr:`SessionConfig.gpu`."""

    AUTO = "auto"
    MESA = "mesa"


class SearchEngine(StrEnum):
    """Which search engine's results page a search loads.

    ``BING`` is the default because DuckDuckGo frequently serves bot challenges.
    ``DDG`` selects DuckDuckGo's no-JavaScript HTML endpoint. Google is absent
    on purpose: from a fresh headless session it serves a consent wall or a CAPTCHA, and its
    markup changes too often to keep an extractor honest.
    """

    DDG = "ddg"
    BING = "bing"


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


#: Launch-flag prefixes a caller may not pass via ``launch_args``. These weaken
#: renderer isolation, load outside code, or open a remote-debugging socket.
#: The sandbox opt-out has its own explicit switch (``chromium_sandbox`` /
#: ``--no-sandbox``); smuggling it through ``--launch-arg`` is rejected so the
#: choice stays visible.
BLOCKED_LAUNCH_ARGS: tuple[str, ...] = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu-sandbox",
    "--no-zygote",
    "--load-extension",
    "--load-component-extension",
    "--unsafely-",
    "--remote-debugging-",
    "--remote-allow-origins",
)

#: Response headers kept in the shaped CLI/MCP fetch payload. Everything else
#: -- notably ``set-cookie``, ``cookie``, ``authorization`` and ``proxy-*`` --
#: is dropped so a model-facing payload does not carry session secrets.
SHAPED_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "last-modified",
        "etag",
        "cache-control",
        "expires",
        "location",
        "server",
        "x-request-id",
    }
)


#: SessionConfig fields removed in 3.0, with what replaces each one.
REMOVED_CONFIG_FIELDS: dict[str, str] = {
    "driver": "WebSkrap 3 always drives Chromium over its own CDP client",
    "browser": "WebSkrap 3 drives Chromium only",
    "slow_mo_ms": "add your own delays between actions",
    "patchright_focus_control": "it was a Patchright option",
    "patchright_context_profile": "use profile_mode='locale'",
}


class SessionConfig(BaseModel):
    """How a session's browser is launched and how its pages behave.

    Defaults are deliberately plain: headless Chromium with its native
    identity, no proxy, everything loaded, and every stealth feature off. The
    stealth switches are documented at their fields, since each has a cost as
    well as a benefit.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    channel: str | None = None
    headless: bool = True
    # Keep Chromium's OS sandbox for one-shot fetches, matching persistent
    # sessions. False appends --no-sandbox; only use where it cannot start.
    # The CLI and MCP server resolve WEBSKRAP_CHROMIUM_SANDBOX=0 into this
    # field; the Python default is always sandboxed unless set explicitly.
    chromium_sandbox: bool = True
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
    launch_args: list[str] = Field(default_factory=list)
    # Simulated screen for headless chromium. Headless Chrome has no physical
    # display, so screen/window metrics (screen.width, outerWidth, ...) leak as
    # headless tells. A virtual screen of this size is configured at launch via
    # browser flags (not JS spoofing). The window leaves 80 px at the right
    # and bottom of this screen. Set to None to disable.
    headless_screen: Viewport | None = Field(
        default_factory=lambda: Viewport(width=1920, height=1080)
    )
    # Headless Chrome stamps "HeadlessChrome" into navigator.userAgent (and the
    # worker UA). When True for a headless chromium run, WebSkrap probes the
    # real UA and re-applies it with "HeadlessChrome" rewritten to "Chrome" via
    # the --user-agent launch flag. Chromium then reports only low-entropy
    # client hints: getHighEntropyValues() and Sec-CH-UA-Full-Version-List,
    # -Arch, -Bitness and -Platform-Version come back empty, which no real
    # Chrome does. Prefer virtual_display, which needs no override. Ignored
    # when virtual_display is set.
    mask_headless_user_agent: bool = False
    # Run a headless session as a headed browser on a private Xvfb display
    # (Linux, Xvfb installed) instead of in Chromium's headless mode. Nothing
    # is overridden: no HeadlessChrome token, full client hints, real
    # scrollbars and pointer media, and a real X screen sized by
    # headless_screen (1920x1080 when that is None). Only applies when
    # headless is True; the window stays invisible either way.
    virtual_display: bool = False
    # Which GL implementation WebGL renders with (chromium only).
    # "auto" leaves it to Chromium: a real GPU where one is usable. Without
    # one, headless Chrome still falls back to SwiftShader, whose renderer
    # string ("SwiftShader Device") real Chrome stopped reporting when
    # Chrome 139 removed the automatic fallback, so it now reads as
    # automation. Headed Chrome (including virtual_display) instead has no
    # WebGL, as a real GPU-less desktop does. "mesa" renders through Mesa's
    # software Vulkan driver (lavapipe), reporting "llvmpipe": uncommon,
    # since stock Chrome blocklists software drivers, but not specific to
    # automation. It needs Linux with the lavapipe Vulkan ICD (Debian/Ubuntu:
    # mesa-vulkan-drivers). Keep "auto" on a machine with a real GPU.
    gpu: GpuBackend = GpuBackend.AUTO
    # Native Chromium rendering reduction. When enabled, canvas readback and
    # WebGL are disabled through browser flags. This avoids high-entropy
    # rendering fingerprints without JavaScript monkeypatching, but can break
    # pages that require canvas exports or WebGL, and a browser with neither
    # is itself rare: fingerprinting audits report it as blocking. To hide
    # SwiftShader without losing WebGL, use gpu="mesa" instead.
    reduce_fingerprint_surface: bool = False
    # Supply Chromium's synthetic camera and microphone when a page needs
    # media devices. Permission prompts still follow the normal browser flow.
    fake_media_devices: bool = False
    # Chromium WebRTC IP handling policy. Use "disable_non_proxied_udp" to
    # prevent non-proxied UDP ICE candidates, which avoids WebRTC exposing local
    # or direct public IP candidates on leak-test pages without patching the
    # RTCPeerConnection API. None means "disable_non_proxied_udp" when a proxy
    # is set (otherwise WebRTC would reveal the real address behind it) and
    # Chromium's default otherwise; pass "default" to keep Chromium's default
    # behind a proxy.
    webrtc_ip_handling_policy: WebRtcIPHandlingPolicy | None = None
    # How much of the BrowserProfile pages see. "native" (the default)
    # applies nothing: the browser presents its own identity, which is what
    # detectors find hardest to fault. "locale" applies what Chrome can own
    # natively: locale, timezone, color scheme, reduced motion and the
    # profile's extra headers; on Linux the locale and timezone are set
    # through the browser's environment, elsewhere through CDP emulation.
    # "full" also emulates the viewport, screen, device scale, touch and the
    # profile's user agent, as WebSkrap 2.x did with the Playwright driver;
    # every override is a surface a page can cross-check.
    profile_mode: ProfileMode = "native"
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

    @model_validator(mode="before")
    @classmethod
    def reject_removed_fields(cls, data: Any) -> Any:
        """Name what replaced a 2.x field, rather than a generic extra-field error.

        Raises:
            ValueError: If ``data`` sets a field listed in
                :data:`REMOVED_CONFIG_FIELDS`.
        """
        if isinstance(data, dict):
            for name, replacement in REMOVED_CONFIG_FIELDS.items():
                if name in data:
                    msg = f"SessionConfig.{name} was removed in WebSkrap 3.0: {replacement}"
                    raise ValueError(msg)
        return data

    @field_validator("launch_args")
    @classmethod
    def validate_launch_args(_cls, value: list[str]) -> list[str]:
        """Reject launch flags that weaken isolation or open new surfaces.

        Raises:
            ValueError: If any flag matches :data:`BLOCKED_LAUNCH_ARGS`.
        """
        for arg in value:
            for blocked in BLOCKED_LAUNCH_ARGS:
                if blocked.endswith("-"):
                    matched = arg.startswith(blocked)
                else:
                    matched = arg == blocked or arg.startswith(f"{blocked}=")
                if matched:
                    msg = (
                        f"launch argument '{arg}' is blocked: it weakens isolation, "
                        "loads outside code, or opens remote debugging. Use the "
                        "explicit chromium_sandbox switch instead."
                    )
                    raise ValueError(msg)
        return value

    @property
    def launch_headless(self) -> bool:
        """True when Chromium runs in headless mode (not on a virtual display)."""
        return self.headless and not self.virtual_display

    def browser_args(self, profile: BrowserProfile | None = None) -> list[str]:
        """Return the Chromium flags for this config, without the CDP channel.

        Caller-supplied ``launch_args`` win: a flag already present there is
        not added again by the automation, screen, WebRTC, fingerprint or
        language helpers. ``--no-sandbox`` is added only when
        ``chromium_sandbox`` is False. Given a ``profile`` that
        :meth:`uses_native_profile`, its languages come from
        ``--accept-lang``. Proxy and certificate flags are included.
        """
        native_profile = profile if profile is not None and self.uses_native_profile() else None
        args = (
            self._automation_args()
            + self._accept_lang_args(native_profile)
            + self._screen_args()
            + self._headless_pointer_args()
            + self._sandbox_args()
            + self._reduced_fingerprint_surface_args()
            + self._fake_media_args()
            + self._gpu_args()
            + self._webrtc_ip_handling_args()
            + list(self.launch_args)
        )
        if self.proxy is not None:
            args.append(f"--proxy-server={self.proxy.server}")
            if self.proxy.bypass:
                args.append(f"--proxy-bypass-list={self.proxy.bypass}")
        if self.ignore_https_errors:
            args.append("--ignore-certificate-errors")
        return args

    def browser_env(self, profile: BrowserProfile | None = None) -> dict[str, str] | None:
        """Return the browser environment, or None to inherit this process's.

        Only a profile applied natively (see :meth:`uses_native_profile`)
        changes it: its timezone and locale are set through ``TZ`` and the
        locale variables.

        Raises:
            ValueError: If that profile names an unknown timezone; Chromium
                would report ``Etc/Unknown`` instead.
        """
        if profile is not None and self.uses_native_profile():
            return native_profile_env(profile)
        return None

    def uses_native_profile(self) -> bool:
        """True when the profile's timezone and languages are set at launch.

        That is ``profile_mode="locale"`` on Linux, where Chromium reads
        ``TZ`` and the locale variables. There, nothing is overridden over
        CDP: ``navigator.languages`` keeps the profile's whole list and
        Chromium builds the ``Accept-Language`` q-values itself. macOS and
        Windows take the locale from system settings instead, so they use the
        CDP overrides.
        """
        return self.profile_mode == "locale" and sys.platform.startswith("linux")

    def emulation_commands(self, profile: BrowserProfile) -> list[tuple[str, dict[str, Any]]]:
        """Return the CDP commands that apply ``profile`` to one page.

        Empty for ``profile_mode="native"``. The commands are page-scoped, so
        every new page of a session runs them before it navigates.
        """
        if self.profile_mode == "native":
            return []
        commands: list[tuple[str, dict[str, Any]]] = []
        full = self.profile_mode == "full"
        if full:
            commands.append(
                (
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": profile.viewport.width,
                        "height": profile.viewport.height,
                        "deviceScaleFactor": profile.device_scale_factor,
                        "mobile": profile.is_mobile,
                        "screenWidth": profile.screen.width,
                        "screenHeight": profile.screen.height,
                    },
                )
            )
            commands.append(("Emulation.setTouchEmulationEnabled", {"enabled": profile.has_touch}))
            if profile.user_agent:
                commands.append(
                    (
                        "Emulation.setUserAgentOverride",
                        {
                            "userAgent": profile.user_agent,
                            "acceptLanguage": profile.accept_language(),
                        },
                    )
                )
        if full or not self.uses_native_profile():
            commands.append(("Emulation.setLocaleOverride", {"locale": profile.locale}))
            commands.append(("Emulation.setTimezoneOverride", {"timezoneId": profile.timezone_id}))
        features = []
        if profile.color_scheme != "null":
            features.append({"name": "prefers-color-scheme", "value": profile.color_scheme})
        if profile.reduced_motion != "null":
            features.append({"name": "prefers-reduced-motion", "value": profile.reduced_motion})
        if features:
            commands.append(("Emulation.setEmulatedMedia", {"features": features}))
        headers = profile.headers() if full else dict(profile.extra_http_headers)
        if not full and not self.uses_native_profile():
            # Chromium sends the locale's languages itself on Linux; elsewhere
            # the header has to follow the emulated locale.
            headers.setdefault("Accept-Language", profile.accept_language())
        if headers:
            commands.append(("Network.setExtraHTTPHeaders", {"headers": headers}))
        return commands

    def _accept_lang_args(self, profile: BrowserProfile | None) -> list[str]:
        if profile is None or any(a.startswith("--accept-lang") for a in self.launch_args):
            return []
        return [f"--accept-lang={','.join(profile.navigator_languages)}"]

    def _headless_pointer_args(self) -> list[str]:
        # Headless Chromium has no input devices, so it reports hover "none"
        # and a coarse pointer, which no desktop browser does. Declare a mouse,
        # as Playwright did, unless the caller set its own blink settings.
        if not self.launch_headless or any(
            a.startswith("--blink-settings") for a in self.launch_args
        ):
            return []
        return [
            "--blink-settings=primaryHoverType=2,availableHoverTypes=2,"
            "primaryPointerType=4,availablePointerTypes=4"
        ]

    def _sandbox_args(self) -> list[str]:
        """Return the sandbox opt-out flag when sandboxing is disabled."""
        if self.chromium_sandbox:
            return []
        if any(a == "--no-sandbox" or a.startswith("--no-sandbox=") for a in self.launch_args):
            return []
        return ["--no-sandbox"]

    def _automation_args(self) -> list[str]:
        # A debugging channel makes Chromium report navigator.webdriver=true,
        # an instant tell for detectors like DataDome. Skipped if the caller
        # already passed a --disable-blink-features flag.
        flag = "--disable-blink-features=AutomationControlled"
        if any(a.startswith("--disable-blink-features") for a in self.launch_args):
            return []
        return [flag]

    def uses_virtual_display(self) -> bool:
        """True when this session runs headed on a private Xvfb display."""
        return self.virtual_display and self.headless

    def virtual_screen(self) -> Viewport:
        """Return the screen size a virtual display is started with."""
        return self.headless_screen or Viewport(width=1920, height=1080)

    def _screen_args(self) -> list[str]:
        # Configure a virtual screen for headless chromium so the browser
        # reports real display metrics instead of headless defaults. Skips any
        # flag the caller already set in launch_args (their value wins).
        if not (self.headless and self.headless_screen):
            return []
        width, height = self.headless_screen.width, self.headless_screen.height
        # Leave space at the right and bottom, as a normal desktop window
        # does. The screen itself keeps its full configured dimensions.
        bottom_gap = min(80, height - 1)
        window_width, window_height = max(1, width - 80), height - bottom_gap
        candidates = {
            "--window-size": f"--window-size={window_width},{window_height}",
            "--window-position": "--window-position=0,0",
        }
        if not self.virtual_display:
            # Headless-only flag defining a screen at 0,0. Under a virtual
            # display the X server is the screen.
            candidates["--screen-info"] = (
                f"--screen-info={{{width}x{height} workAreaBottom={bottom_gap}}}"
            )
        return [
            arg
            for prefix, arg in candidates.items()
            if not any(a.startswith(prefix) for a in self.launch_args)
        ]

    def effective_webrtc_ip_handling_policy(self) -> WebRtcIPHandlingPolicy | None:
        """Return the WebRTC policy the browser gets, filling in the proxy default.

        An explicit :attr:`webrtc_ip_handling_policy` always wins. Left unset
        behind a proxy, ICE would still gather the host's LAN addresses and
        its direct public address over UDP, which is the leak a proxy is meant
        to prevent, so non-proxied UDP is disabled.
        """
        if self.webrtc_ip_handling_policy is not None:
            return self.webrtc_ip_handling_policy
        if self.proxy is not None:
            return "disable_non_proxied_udp"
        return None

    def _webrtc_ip_handling_args(self) -> list[str]:
        # Chromium requires the policy value and an explicit force flag for the
        # process-level WebRTC IP handling override to apply in Chrome.
        policy = self.effective_webrtc_ip_handling_policy()
        if policy is None:
            return []
        candidates = {
            "--webrtc-ip-handling-policy": f"--webrtc-ip-handling-policy={policy}",
            "--force-webrtc-ip-handling-policy": "--force-webrtc-ip-handling-policy",
        }
        return [
            arg
            for prefix, arg in candidates.items()
            if not any(a == prefix or a.startswith(f"{prefix}=") for a in self.launch_args)
        ]

    def _gpu_args(self) -> list[str]:
        # A caller choosing their own GL implementation keeps it.
        if any(a.startswith(("--use-gl", "--use-angle")) for a in self.launch_args):
            return []
        if self.gpu is GpuBackend.MESA:
            # --ignore-gpu-blocklist: Chromium blocklists software Vulkan
            # drivers and would otherwise leave WebGL unavailable.
            return ["--use-gl=angle", "--use-angle=vulkan", "--ignore-gpu-blocklist"]
        return []

    def _reduced_fingerprint_surface_args(self) -> list[str]:
        if not self.reduce_fingerprint_surface:
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

    def _fake_media_args(self) -> list[str]:
        flag = "--use-fake-device-for-media-stream"
        if not self.fake_media_devices:
            return []
        if any(a == flag or a.startswith(f"{flag}=") for a in self.launch_args):
            return []
        return [flag]


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


class SearchHit(BaseModel):
    """One organic result: where it points, what it is called, and its blurb.

    ``url`` is the destination itself, with the engine's click-tracking
    redirect already unwrapped. ``snippet`` is empty when the engine showed
    none.
    """

    title: str
    url: str
    snippet: str = ""


class SearchResult(BaseModel):
    """What one search produced.

    ``url`` is the results page that was loaded; ``ok`` and ``status`` judge
    that page's HTTP response, not whether anything was found. ``hits`` is
    capped by the caller's ``max_results`` and ``hits_total`` counts what the
    page held before the cap, so a short list is distinguishable from a short
    page.
    """

    query: str
    engine: SearchEngine
    url: str
    final_url: str
    status: int | None
    ok: bool
    hits: list[SearchHit] = Field(default_factory=list)
    hits_total: int = 0
    timings: dict[str, float] = Field(default_factory=dict)
    # Strategy that dismissed a cookie consent notice on the results page, or
    # None when nothing was declined.
    cookie_notice_declined: str | None = None


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
    unless the fetch collected them. Cookies are left out, and headers are
    filtered to :data:`SHAPED_RESPONSE_HEADERS` so session secrets such as
    ``set-cookie`` never reach a model-facing payload.
    """
    window = text_window(result.text or "", max_chars, offset)
    shaped_headers = {
        name: value
        for name, value in result.headers.items()
        if name.lower() in SHAPED_RESPONSE_HEADERS
    }
    return {
        "url": result.url,
        "final_url": result.final_url,
        "status": result.status,
        "ok": result.ok,
        "title": result.title,
        "headers": shaped_headers,
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


def shape_search_result(result: SearchResult) -> dict[str, Any]:
    """Flatten a search into the JSON payload the CLI and MCP tools return.

    Hits are already capped by the search itself; ``hits_truncated`` says
    whether the cap hid any, so a caller knows a larger ``max_results`` would
    return more without loading the page again to find out.
    """
    return {
        "query": result.query,
        "engine": result.engine.value,
        "url": result.url,
        "final_url": result.final_url,
        "status": result.status,
        "ok": result.ok,
        "hits": [hit.model_dump() for hit in result.hits],
        "hits_total": result.hits_total,
        "hits_truncated": result.hits_total > len(result.hits),
        "elapsed_ms": round(result.timings.get("elapsed_ms", 0.0), 1),
        "cookie_notice_declined": result.cookie_notice_declined,
    }
