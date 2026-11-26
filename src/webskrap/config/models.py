"""Pydantic models for browser launch and session configuration.

:class:`BrowserProfile` is the identity presented to a site (viewport, locale,
headers); :class:`SessionConfig` is the machinery behind it (channel, proxy,
stealth switches, timeouts). Both translate themselves into Chromium flags and
CDP commands, which is where their per-field comments matter.
"""

from __future__ import annotations

import hashlib
import secrets
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from webskrap.config.profile import BrowserProfile, ProxyConfig, Viewport, native_profile_env
from webskrap.config.user_agent import _user_agent_platform

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
#: Element state :func:`~webskrap.browser.actions.wait_for` can wait on.
ElementState = Literal["attached", "detached", "visible", "hidden"]
#: How much of a BrowserProfile pages see; see :attr:`SessionConfig.profile_mode`.
ProfileMode = Literal["native", "locale", "full"]
#: Profile modes a persistent browser session accepts. "full" is CDP
#: emulation, which ends when its connection closes, and every session
#: command connects anew.
SessionProfileMode = Literal["native", "locale"]


class ResourcePolicy(StrEnum):
    """Which resource types a session is allowed to load.

    ``AUTO`` uses lite for text-only fetches without screenshots and all
    resources for other tasks. Explicit policies always take priority.
    ``LITE`` blocks images, fonts and media; ``DOCUMENTS`` also blocks
    stylesheets. Both cut bandwidth and latency, and both are visible to a site
    that checks whether its assets loaded.
    """

    AUTO = "auto"
    ALL = "all"
    LITE = "lite"
    DOCUMENTS = "documents"


class GpuBackend(StrEnum):
    """Which GL implementation WebGL renders with; see :attr:`SessionConfig.gpu`."""

    AUTO = "auto"
    MESA = "mesa"


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


#: SessionConfig fields removed in 3.0, with what replaces each one.
REMOVED_CONFIG_FIELDS: dict[str, str] = {
    "driver": "WebSkrap 3 always drives Chromium over its own CDP client",
    "browser": "WebSkrap 3 drives Chromium only",
    "slow_mo_ms": "add your own delays between actions",
    "patchright_focus_control": "it was a Patchright option",
    "patchright_context_profile": "use profile_mode='locale'",
}


#: Panels common Linux desktops reserve, as ``--screen-info`` work-area insets:
#: GNOME's top bar, Ubuntu's top bar and dock, Xfce's top panel, and Plasma's
#: bottom panel at two sizes.
_DESKTOP_PANELS: tuple[dict[str, int], ...] = (
    {"workAreaTop": 32},
    {"workAreaLeft": 64, "workAreaTop": 32},
    {"workAreaTop": 28},
    {"workAreaBottom": 44},
    {"workAreaBottom": 48},
)
#: Window geometry seed for runs without a persistent profile.
_PROCESS_GEOMETRY_SEED = secrets.token_bytes(32)


class SessionConfig(BaseModel):
    """How a session's browser is launched and how its pages behave.

    Defaults are deliberately plain: headless Chromium with its native
    identity, no proxy, task-based resource loading, and every stealth feature
    off. The stealth switches are documented at their fields, since each has a cost as
    well as a benefit.
    """

    model_config = ConfigDict(extra="forbid")

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
    resource_policy: ResourcePolicy = ResourcePolicy.AUTO
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
    # browser flags (not JS spoofing). A Linux desktop panel and the window's
    # size and position come from the persistent profile directory, or one
    # process-wide seed without one. Set to None to disable.
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
    # mesa-vulkan-drivers). Under virtual_display, "auto" launches as "mesa"
    # when lavapipe is installed and no Intel, AMD or NVIDIA GPU is found.
    # Keep "auto" on a machine with a real GPU.
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
    # Render this many device pixels per CSS pixel through
    # --force-device-scale-factor, so screenshots and live views come out
    # sharper. Pages see it as devicePixelRatio, the resolution media query
    # and a screen of the same CSS size: a fingerprint change, off by
    # default. A virtual display grows by the same factor in physical pixels.
    device_scale_factor: float | None = Field(default=None, ge=1, le=4)
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
    solve_cloudflare: bool = False
    solve_datadome: bool = False
    solve_perimeterx: bool = False
    solve_recaptcha: bool = False

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
            + self._device_scale_args()
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

    def emulation_commands(
        self, profile: BrowserProfile, *, user_agent_metadata: dict[str, Any] | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return the CDP commands that apply ``profile`` to one page.

        Empty for ``profile_mode="native"``. The commands are page-scoped, so
        every new page of a session runs them before it navigates.
        ``user_agent_metadata`` (see :func:`user_agent_metadata`) goes with
        the user-agent override. Without it, Chromium reports empty client
        hints for an overridden user agent and sends no ``Sec-CH-UA``.
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
                override: dict[str, Any] = {
                    "userAgent": profile.user_agent,
                    # A plain list: Chromium copies it into navigator.languages
                    # and adds the header's q-values itself.
                    "acceptLanguage": ",".join(profile.navigator_languages),
                }
                if platform := _user_agent_platform(profile.user_agent):
                    override["platform"] = platform
                if user_agent_metadata is not None:
                    override["userAgentMetadata"] = user_agent_metadata
                commands.append(("Emulation.setUserAgentOverride", override))
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
        """Return the screen size a virtual display is started with, in physical pixels.

        :attr:`device_scale_factor` multiplies it, so the screen keeps its CSS size.
        """
        screen = self.headless_screen or Viewport(width=1920, height=1080)
        scale = self.device_scale_factor or 1
        return Viewport(width=round(screen.width * scale), height=round(screen.height * scale))

    def _screen_args(self) -> list[str]:
        # Configure a virtual screen for headless chromium so the browser
        # reports real display metrics instead of headless defaults. Skips any
        # flag the caller already set in launch_args (their value wins).
        if not (self.headless and self.headless_screen):
            return []
        width, height = self.headless_screen.width, self.headless_screen.height
        insets, (window_width, window_height, x, y) = self._window_geometry(width, height)
        candidates = {
            "--window-size": f"--window-size={window_width},{window_height}",
            "--window-position": f"--window-position={x},{y}",
        }
        if not self.virtual_display:
            # Headless-only flag defining a screen at 0,0, in physical pixels
            # while the window size stays in CSS pixels. Under a virtual
            # display the X server is the screen.
            scale = self.device_scale_factor or 1
            area = "".join(f" {key}={round(value * scale)}" for key, value in insets.items())
            size = f"{round(width * scale)}x{round(height * scale)}"
            candidates["--screen-info"] = f"--screen-info={{{size}{area}}}"
        return [
            arg
            for prefix, arg in candidates.items()
            if not any(a.startswith(prefix) for a in self.launch_args)
        ]

    def _window_geometry(
        self, width: int, height: int
    ) -> tuple[dict[str, int], tuple[int, int, int, int]]:
        """Return the screen's work-area insets and the window's size and position.

        A fixed geometry would be one more value shared by every WebSkrap
        run. A desktop keeps one panel layout, and a profile keeps its
        window, so both come from a seed: the persistent profile directory,
        or else one value for the whole process. A virtual display has no
        window manager, so no panel. The window is maximized in its work
        area or floats inside it.
        """
        seed = (
            hashlib.sha256(str(self.user_data_dir).encode()).digest()
            if self.user_data_dir is not None
            else _PROCESS_GEOMETRY_SEED
        )
        panel = {} if self.virtual_display else _DESKTOP_PANELS[seed[0] % len(_DESKTOP_PANELS)]
        left = min(panel.get("workAreaLeft", 0), width - 1)
        top = min(panel.get("workAreaTop", 0), height - 1)
        bottom = min(panel.get("workAreaBottom", 0), height - 1 - top)
        insets = {
            key: value
            for key, value in (
                ("workAreaLeft", left),
                ("workAreaTop", top),
                ("workAreaBottom", bottom),
            )
            if key in panel
        }
        area_width, area_height = width - left, height - top - bottom
        if seed[1] % 2:
            return insets, (area_width, area_height, left, top)
        window_width = max(1, area_width - 40 - seed[2] % 360)
        window_height = max(1, area_height - 30 - seed[3] % 210)
        x = left + (area_width - window_width) * seed[4] // 255
        y = top + (area_height - window_height) * seed[5] // 255
        return insets, (window_width, window_height, x, y)

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

    def _device_scale_args(self) -> list[str]:
        flag = "--force-device-scale-factor"
        if self.device_scale_factor is None or any(a.startswith(flag) for a in self.launch_args):
            return []
        return [f"{flag}={self.device_scale_factor:g}"]

    def _fake_media_args(self) -> list[str]:
        flag = "--use-fake-device-for-media-stream"
        if not self.fake_media_devices:
            return []
        if any(a == flag or a.startswith(f"{flag}=") for a in self.launch_args):
            return []
        return [flag]
