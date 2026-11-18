"""Typer entry point for the ``webskrap`` command.

Subcommands cover installing browsers, listing profiles, checking the
install, fetching pages, and searching; persistent-session commands live in
:mod:`webskrap.browser_cli` under ``webskrap browser``. This layer parses
arguments, formats output, and turns failures into one-line errors with a
non-zero exit code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from webskrap.browser.browser_session import sandbox_enabled
from webskrap.browser.channels import with_channel_fallback
from webskrap.browser.diagnostics import diagnose
from webskrap.browser.install import install as install_browser
from webskrap.config.models import (
    GpuBackend,
    ResourcePolicy,
    SearchEngine,
    SessionConfig,
)
from webskrap.config.parsing import (
    parse_profile_mode,
    parse_wait_until,
    parse_webrtc_ip_handling_policy,
)
from webskrap.config.profiles import get_profile, list_profiles
from webskrap.errors import (
    ErrorCode,
    WebSkrapError,
    first_line,
    is_launch_failure,
    is_sandbox_failure,
)
from webskrap.interfaces.browser_cli import (
    GpuOption,
    ProfileOption,
    VirtualDisplayOption,
    browser_app,
)
from webskrap.interfaces.cli_output import (
    OutputFormat,
    fail,
    parse_cli_value,
    parse_output_format,
    print_json,
    stderr_console,
)
from webskrap.interfaces.cli_schema import describe_command as describe_command
from webskrap.scraping.client import WebSkrapClient
from webskrap.scraping.results import (
    FetchResult,
    SearchResult,
    shape_fetch_result,
    shape_search_result,
)

app = typer.Typer(help="WebSkrap browser scraping toolkit.")
app.add_typer(browser_app, name="browser")
console = Console()

T = TypeVar("T")

ChannelOption = Annotated[
    str | None,
    typer.Option("--channel", help="Browser: chrome, msedge, or chromium (Chrome for Testing)."),
]
UserDataDirOption = Annotated[
    Path | None,
    typer.Option("--user-data-dir", help="Persistent browser profile directory."),
]
OutputFormatOption = Annotated[
    str,
    typer.Option("--format", help="Output format: human or json."),
]
DeclineCookiesTimeoutMsOption = Annotated[
    float,
    typer.Option(
        "--decline-cookies-timeout-ms",
        min=0,
        help="How long to wait for a cookie consent notice to appear.",
    ),
]
ProfileModeOption = Annotated[
    str,
    typer.Option(
        "--profile-mode",
        help=(
            "How much of --profile pages see: native (none, the default), locale "
            "(locale, timezone, media, headers) or full (also viewport and user agent)."
        ),
    ),
]
ReduceFingerprintSurfaceOption = Annotated[
    bool,
    typer.Option(
        "--reduce-fingerprint-surface",
        help="Disable Chromium WebGL and canvas readback with native browser flags.",
    ),
]
MaskHeadlessUserAgentOption = Annotated[
    bool,
    typer.Option(
        "--mask-headless-user-agent",
        help=(
            "Rewrite HeadlessChrome to Chrome via Chromium's user-agent flag. "
            "Empties high-entropy client hints; prefer --virtual-display."
        ),
    ),
]
FakeMediaDevicesOption = Annotated[
    bool,
    typer.Option("--fake-media-devices", help="Use synthetic Chromium camera and microphone."),
]
LaunchArgsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--launch-arg",
        help="Additional browser launch argument. Repeat for multiple args.",
    ),
]
NoSandboxOption = Annotated[
    bool,
    typer.Option(
        "--no-sandbox",
        help="Disable Chromium's OS sandbox (weakens isolation; only where it cannot start).",
    ),
]
WebrtcIpHandlingPolicyOption = Annotated[
    str | None,
    typer.Option(
        "--webrtc-ip-handling-policy",
        help=(
            "Chromium WebRTC IP policy: default, default_public_and_private_interfaces, "
            "default_public_interface_only, or disable_non_proxied_udp."
        ),
    ),
]


@app.command("install")
def install_command(
    force: Annotated[
        bool,
        typer.Option("--force", help="Download again even when this version is installed."),
    ] = False,
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    """Download Chrome for Testing, the browser WebSkrap drives by default."""
    output_format = parse_output_format(format)
    try:
        result = install_browser(force=force)
    except WebSkrapError as exc:
        fail(exc, output_format)
    if output_format == "json":
        print_json({"ok": True, **result})
        return
    state = "Installed" if result["installed"] else "Already installed:"
    console.print(
        f"[green]{state} Chrome for Testing {result['version']}[/green] ({result['platform']})"
    )
    console.print(f"[bold]Executable:[/bold] {result['executable']}", soft_wrap=True)


@app.command("profiles")
def profiles_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    """List the bundled browser profiles."""
    output_format = parse_output_format(format)
    profiles = list_profiles()
    if output_format == "json":
        print_json({"profiles": [profile.model_dump(mode="json") for profile in profiles]})
        return

    table = Table(title="WebSkrap Profiles")
    table.add_column("Name")
    table.add_column("Viewport")
    table.add_column("Locale")
    table.add_column("Timezone")
    table.add_column("Mobile")

    for profile in profiles:
        table.add_row(
            profile.name,
            f"{profile.viewport.width}x{profile.viewport.height}",
            profile.locale,
            profile.timezone_id,
            "yes" if profile.is_mobile else "no",
        )

    console.print(table)


@app.command("schema")
def schema_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: json or human."),
    ] = "json",
) -> None:
    """Describe every command, option and default as one JSON document.

    Written for callers that generate invocations rather than read help text:
    one payload instead of a `--help` run per subcommand.
    """
    output_format = parse_output_format(format)
    schema = describe_command(typer.main.get_command(app), "webskrap")
    if output_format == "json":
        print_json(schema)
        return

    table = Table(title="WebSkrap Commands")
    table.add_column("Command")
    table.add_column("Summary")
    for path, summary in _walk_command_summaries(schema, ()):
        table.add_row(path, summary)
    console.print(table)


def _walk_command_summaries(
    schema: dict[str, Any], prefix: tuple[str, ...]
) -> list[tuple[str, str]]:
    """Flatten a schema into ``("browser open", "Start ...")`` rows."""
    path = (*prefix, str(schema["name"]))
    rows = [] if schema.get("commands") else [(" ".join(path[1:]) or path[0], str(schema["help"]))]
    for child in schema.get("commands", []):
        rows.extend(_walk_command_summaries(child, path))
    return rows


@app.command("mcp")
def mcp_command() -> None:
    """Run the MCP server over stdio, the same server as `webskrap-mcp`."""
    from webskrap.interfaces.mcp_server import main as run_mcp_server

    run_mcp_server()


@app.command("doctor")
def doctor_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    """Check that Chromium is installed and can launch."""
    output_format = parse_output_format(format)
    result = asyncio.run(_doctor())
    if output_format == "json":
        print_json(result)
    else:
        _print_doctor_result(result)
    if not result["ok"]:
        raise typer.Exit(code=1)


async def _doctor() -> dict[str, Any]:
    return await diagnose()


@app.command("fetch")
def fetch_command(
    url: Annotated[str, typer.Argument(help="URL to fetch.")],
    profile: ProfileOption = "desktop-chrome",
    channel: ChannelOption = "chrome",
    user_data_dir: UserDataDirOption = None,
    screenshot: Annotated[
        Path | None,
        typer.Option("--screenshot", help="Write a full-page screenshot to this path."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write fetched content to this file."),
    ] = None,
    output_format: OutputFormatOption = "human",
    max_chars: Annotated[
        int,
        typer.Option("--max-chars", min=0, help="Maximum JSON text characters."),
    ] = 20_000,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Start JSON text at this character; pass back next_text_offset to continue.",
        ),
    ] = 0,
    stdout: Annotated[
        bool,
        typer.Option("--stdout", help="Write fetched content to stdout."),
    ] = False,
    text_only: Annotated[
        bool,
        typer.Option("--text-only", help="Return readable body text instead of HTML."),
    ] = False,
    links: Annotated[
        bool,
        typer.Option("--links", help="Also collect the page's outbound links."),
    ] = False,
    max_links: Annotated[
        int,
        typer.Option("--max-links", min=0, help="Maximum links to collect."),
    ] = 50,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", help="Suppress human summary output."),
    ] = False,
    wait_until: Annotated[
        str,
        typer.Option("--wait-until", help="commit, domcontentloaded, load, or networkidle."),
    ] = "domcontentloaded",
    ready_selector: Annotated[
        str | None,
        typer.Option("--ready-selector", help="Wait for this CSS selector or ref to be visible."),
    ] = None,
    timeout_ms: Annotated[
        float,
        typer.Option("--timeout-ms", min=1, help="Timeout for navigation and each readiness wait."),
    ] = 30_000,
    resource_policy: Annotated[
        ResourcePolicy,
        typer.Option(
            "--resource-policy",
            help="auto: lite for text without screenshots; all otherwise. Or all, lite, documents.",
        ),
    ] = ResourcePolicy.AUTO,
    decline_cookies: Annotated[
        bool,
        typer.Option(
            "--decline-cookies/--no-decline-cookies",
            help="Click the reject button of a cookie consent notice after load.",
        ),
    ] = True,
    decline_cookies_timeout_ms: DeclineCookiesTimeoutMsOption = 2_000,
    solve_cloudflare: Annotated[
        bool,
        typer.Option(
            "--solve-cloudflare",
            help="Click Cloudflare's Turnstile checkbox and wait up to 60 s for the real page.",
        ),
    ] = False,
    solve_datadome: Annotated[
        bool,
        typer.Option(
            "--solve-datadome",
            help="Drag DataDome's slider CAPTCHA and wait up to 60 s for the real page.",
        ),
    ] = False,
    solve_perimeterx: Annotated[
        bool,
        typer.Option(
            "--solve-perimeterx",
            help="Hold HUMAN's Press & Hold button and wait up to 60 s for the real page.",
        ),
    ] = False,
    solve_recaptcha: Annotated[
        bool,
        typer.Option(
            "--solve-recaptcha",
            help="Click a reCAPTCHA v2 or hCaptcha checkbox on a blocked page; wait up to 60 s.",
        ),
    ] = False,
    profile_mode: ProfileModeOption = "native",
    reduce_fingerprint_surface: ReduceFingerprintSurfaceOption = False,
    mask_headless_user_agent: MaskHeadlessUserAgentOption = False,
    virtual_display: VirtualDisplayOption = False,
    gpu: GpuOption = GpuBackend.AUTO,
    fake_media_devices: FakeMediaDevicesOption = False,
    launch_args: LaunchArgsOption = None,
    no_sandbox: NoSandboxOption = False,
    webrtc_ip_handling_policy: WebrtcIpHandlingPolicyOption = None,
) -> None:
    """Fetch a URL in headless Chromium and report the result."""
    asyncio.run(
        _fetch(
            url=url,
            profile=profile,
            channel=channel,
            user_data_dir=user_data_dir,
            screenshot=screenshot,
            output=output,
            output_format=output_format,
            max_chars=max_chars,
            offset=offset,
            stdout=stdout,
            text_only=text_only,
            links=links,
            max_links=max_links,
            quiet=quiet,
            wait_until=wait_until,
            ready_selector=ready_selector,
            timeout_ms=timeout_ms,
            resource_policy=resource_policy,
            decline_cookies=decline_cookies,
            decline_cookies_timeout_ms=decline_cookies_timeout_ms,
            solve_cloudflare=solve_cloudflare,
            solve_datadome=solve_datadome,
            solve_perimeterx=solve_perimeterx,
            solve_recaptcha=solve_recaptcha,
            profile_mode=profile_mode,
            reduce_fingerprint_surface=reduce_fingerprint_surface,
            mask_headless_user_agent=mask_headless_user_agent,
            virtual_display=virtual_display,
            gpu=gpu,
            fake_media_devices=fake_media_devices,
            launch_args=launch_args or [],
            no_sandbox=no_sandbox,
            webrtc_ip_handling_policy=webrtc_ip_handling_policy,
        )
    )


async def _fetch(
    *,
    url: str,
    profile: str,
    channel: str | None,
    user_data_dir: Path | None,
    screenshot: Path | None,
    output: Path | None,
    output_format: str,
    max_chars: int,
    offset: int,
    stdout: bool,
    text_only: bool,
    links: bool,
    max_links: int,
    quiet: bool,
    wait_until: str,
    ready_selector: str | None,
    timeout_ms: float,
    resource_policy: ResourcePolicy,
    decline_cookies: bool,
    decline_cookies_timeout_ms: float,
    solve_cloudflare: bool,
    solve_datadome: bool,
    solve_perimeterx: bool,
    solve_recaptcha: bool,
    profile_mode: str,
    reduce_fingerprint_surface: bool,
    mask_headless_user_agent: bool,
    virtual_display: bool,
    gpu: GpuBackend,
    fake_media_devices: bool,
    launch_args: list[str],
    no_sandbox: bool,
    webrtc_ip_handling_policy: str | None,
) -> None:
    parsed_output_format = parse_output_format(output_format)
    selected_profile = get_profile(profile)
    config = _session_config(
        channel=channel,
        no_sandbox=no_sandbox,
        user_data_dir=user_data_dir,
        timeout_ms=timeout_ms,
        resource_policy=resource_policy,
        decline_cookies=decline_cookies,
        decline_cookies_timeout_ms=decline_cookies_timeout_ms,
        solve_cloudflare=solve_cloudflare,
        solve_datadome=solve_datadome,
        solve_perimeterx=solve_perimeterx,
        solve_recaptcha=solve_recaptcha,
        profile_mode=profile_mode,
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        virtual_display=virtual_display,
        gpu=gpu,
        fake_media_devices=fake_media_devices,
        launch_args=launch_args,
        webrtc_ip_handling_policy=webrtc_ip_handling_policy,
    )

    try:
        parsed_wait_until = parse_cli_value(wait_until, parse_wait_until)
        result = await _with_channel_fallback(
            lambda resolved: _run_fetch(
                resolved,
                url=url,
                profile=selected_profile,
                wait_until=parsed_wait_until,
                ready_selector=ready_selector,
                screenshot=screenshot or False,
                timeout_ms=timeout_ms,
                text_only=text_only,
                include_links=links,
                max_links=max_links,
            ),
            config,
            parsed_output_format,
        )
    except (typer.Exit, typer.Abort, typer.BadParameter):
        raise
    except Exception as exc:
        # A timeout, a refused host or an unwritable path leaves through the
        # same envelope as a launch failure, so `--format json` never has to
        # answer with Rich markup on stderr.
        fail(exc, parsed_output_format)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.text, encoding="utf-8")

    if parsed_output_format == "json":
        print_json(shape_fetch_result(result, max_chars, offset))
        return

    if stdout:
        typer.echo(result.text, nl=False)
        return

    if quiet:
        return

    console.print(f"[bold]Status:[/bold] {result.status}")
    console.print(f"[bold]Final URL:[/bold] {result.final_url}")
    console.print(f"[bold]Title:[/bold] {escape(result.title)}")
    if result.cookie_notice_declined:
        console.print(f"[bold]Cookie notice:[/bold] declined ({result.cookie_notice_declined})")
    if links:
        console.print(f"[bold]Links:[/bold] {len(result.links)} of {result.links_total}")
    if result.screenshot_path:
        console.print(f"[bold]Screenshot:[/bold] {result.screenshot_path}", soft_wrap=True)
    if output:
        label = "Text" if text_only else "HTML"
        console.print(f"[bold]{label}:[/bold] {output}", soft_wrap=True)


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="Words to search for.")],
    engine: Annotated[
        SearchEngine,
        typer.Option("--engine", "-e", help="Search engine: bing (default) or ddg (DuckDuckGo)."),
    ] = SearchEngine.BING,
    max_results: Annotated[
        int,
        typer.Option("--max-results", "-n", min=0, help="Maximum hits to return."),
    ] = 10,
    profile: ProfileOption = "desktop-chrome",
    channel: ChannelOption = "chrome",
    user_data_dir: UserDataDirOption = None,
    output_format: OutputFormatOption = "human",
    timeout_ms: Annotated[
        float,
        typer.Option("--timeout-ms", min=1, help="Navigation timeout."),
    ] = 30_000,
    resource_policy: Annotated[
        ResourcePolicy,
        typer.Option("--resource-policy", help="Resource routing preset."),
    ] = ResourcePolicy.ALL,
    decline_cookies: Annotated[
        bool,
        typer.Option(
            "--decline-cookies/--no-decline-cookies",
            help="Click the reject button of a cookie consent notice on the results page.",
        ),
    ] = True,
    decline_cookies_timeout_ms: DeclineCookiesTimeoutMsOption = 2_000,
    profile_mode: ProfileModeOption = "native",
    reduce_fingerprint_surface: ReduceFingerprintSurfaceOption = False,
    mask_headless_user_agent: MaskHeadlessUserAgentOption = False,
    virtual_display: VirtualDisplayOption = False,
    gpu: GpuOption = GpuBackend.AUTO,
    fake_media_devices: FakeMediaDevicesOption = False,
    launch_args: LaunchArgsOption = None,
    no_sandbox: NoSandboxOption = False,
    webrtc_ip_handling_policy: WebrtcIpHandlingPolicyOption = None,
) -> None:
    """Search the web through the stealth browser and list the organic hits.

    Loads Bing's results page (or DuckDuckGo's HTML page with --engine ddg) and
    prints each hit's title, URL and snippet with the engine's click-tracking
    unwrapped. Google is not offered. A `blocked` failure means the engine
    served a bot challenge: switch engine or exit IP rather than retrying.
    """
    asyncio.run(
        _search(
            query=query,
            engine=engine,
            max_results=max_results,
            profile=profile,
            channel=channel,
            user_data_dir=user_data_dir,
            output_format=output_format,
            timeout_ms=timeout_ms,
            resource_policy=resource_policy,
            decline_cookies=decline_cookies,
            decline_cookies_timeout_ms=decline_cookies_timeout_ms,
            profile_mode=profile_mode,
            reduce_fingerprint_surface=reduce_fingerprint_surface,
            mask_headless_user_agent=mask_headless_user_agent,
            virtual_display=virtual_display,
            gpu=gpu,
            fake_media_devices=fake_media_devices,
            launch_args=launch_args or [],
            no_sandbox=no_sandbox,
            webrtc_ip_handling_policy=webrtc_ip_handling_policy,
        )
    )


async def _search(
    *,
    query: str,
    engine: SearchEngine,
    max_results: int,
    profile: str,
    channel: str | None,
    user_data_dir: Path | None,
    output_format: str,
    timeout_ms: float,
    resource_policy: ResourcePolicy,
    decline_cookies: bool,
    decline_cookies_timeout_ms: float,
    profile_mode: str,
    reduce_fingerprint_surface: bool,
    mask_headless_user_agent: bool,
    virtual_display: bool,
    gpu: GpuBackend,
    fake_media_devices: bool,
    launch_args: list[str],
    no_sandbox: bool,
    webrtc_ip_handling_policy: str | None,
) -> None:
    parsed_output_format = parse_output_format(output_format)
    config = _session_config(
        channel=channel,
        no_sandbox=no_sandbox,
        user_data_dir=user_data_dir,
        timeout_ms=timeout_ms,
        resource_policy=resource_policy,
        decline_cookies=decline_cookies,
        decline_cookies_timeout_ms=decline_cookies_timeout_ms,
        profile_mode=profile_mode,
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        virtual_display=virtual_display,
        gpu=gpu,
        fake_media_devices=fake_media_devices,
        launch_args=launch_args,
        webrtc_ip_handling_policy=webrtc_ip_handling_policy,
    )

    try:
        result = await _with_channel_fallback(
            lambda resolved: _run_search(
                resolved,
                query=query,
                engine=engine,
                max_results=max_results,
                profile=get_profile(profile),
                timeout_ms=timeout_ms,
            ),
            config,
            parsed_output_format,
        )
    except (typer.Exit, typer.Abort, typer.BadParameter):
        raise
    except Exception as exc:
        fail(exc, parsed_output_format)

    if parsed_output_format == "json":
        print_json(shape_search_result(result))
        return

    console.print(f"[bold]Engine:[/bold] {result.engine.value}")
    console.print(f"[bold]Status:[/bold] {result.status}")
    console.print(f"[bold]Hits:[/bold] {len(result.hits)} of {result.hits_total}")
    if result.cookie_notice_declined:
        console.print(f"[bold]Cookie notice:[/bold] declined ({result.cookie_notice_declined})")
    # Titles and snippets are page-controlled text; escaped so a "[/bold]" in
    # a result cannot break the render or inject a terminal hyperlink.
    for index, hit in enumerate(result.hits, start=1):
        console.print(f"\n[bold]{index}. {escape(hit.title)}[/bold]")
        console.print(f"   {escape(hit.url)}")
        if hit.snippet:
            console.print(f"   {escape(hit.snippet)}")


def _session_config(
    *,
    channel: str | None,
    no_sandbox: bool,
    user_data_dir: Path | None,
    timeout_ms: float,
    resource_policy: ResourcePolicy,
    decline_cookies: bool,
    decline_cookies_timeout_ms: float,
    profile_mode: str,
    reduce_fingerprint_surface: bool,
    mask_headless_user_agent: bool,
    virtual_display: bool,
    gpu: GpuBackend,
    fake_media_devices: bool,
    launch_args: list[str],
    webrtc_ip_handling_policy: str | None,
    solve_cloudflare: bool = False,
    solve_datadome: bool = False,
    solve_perimeterx: bool = False,
    solve_recaptcha: bool = False,
) -> SessionConfig:
    """Build the headless session config that ``fetch`` and ``search`` share."""
    return SessionConfig(
        headless=True,
        channel=channel,
        chromium_sandbox=sandbox_enabled(False if no_sandbox else None),
        user_data_dir=user_data_dir,
        navigation_timeout_ms=timeout_ms,
        resource_policy=resource_policy,
        decline_cookies=decline_cookies,
        decline_cookies_timeout_ms=decline_cookies_timeout_ms,
        solve_cloudflare=solve_cloudflare,
        solve_datadome=solve_datadome,
        solve_perimeterx=solve_perimeterx,
        solve_recaptcha=solve_recaptcha,
        profile_mode=parse_cli_value(profile_mode, parse_profile_mode),
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        virtual_display=virtual_display,
        gpu=gpu,
        fake_media_devices=fake_media_devices,
        launch_args=launch_args,
        webrtc_ip_handling_policy=parse_cli_value(
            webrtc_ip_handling_policy, parse_webrtc_ip_handling_policy
        ),
    )


def _fail_launch(exc: Exception, output_format: OutputFormat) -> NoReturn:
    """Report an unlaunchable browser the way `doctor` does, not as a traceback."""
    if is_sandbox_failure(exc):
        # The first line only says the browser closed; the cause is in the
        # browser log below it.
        message = "Browser did not launch: Chromium's OS sandbox could not start"
        fail(WebSkrapError(message, code=ErrorCode.SANDBOX), output_format)
    message = f"Browser did not launch: {first_line(exc)}"
    fail(WebSkrapError(message, code=ErrorCode.BROWSER_LAUNCH), output_format)


async def _run_fetch(config: SessionConfig, **kwargs: Any) -> FetchResult:
    async with WebSkrapClient() as client:
        return await client.fetch(config=config, **kwargs)


async def _run_search(config: SessionConfig, **kwargs: Any) -> SearchResult:
    async with WebSkrapClient() as client:
        return await client.search(config=config, **kwargs)


async def _with_channel_fallback(
    run: Callable[[SessionConfig], Awaitable[T]],
    config: SessionConfig,
    output_format: OutputFormat,
) -> T:
    """Run with :func:`with_channel_fallback`, reporting launch failures cleanly."""

    def announce(failed: str | None, retry: str | None) -> None:
        stderr_console.print(
            f"[yellow]channel '{failed}' did not launch; retrying with {retry}[/yellow]"
        )

    try:
        return await with_channel_fallback(run, config, on_retry=announce)
    except Exception as exc:
        if is_launch_failure(exc) or is_sandbox_failure(exc):
            _fail_launch(exc, output_format)
        raise


def _print_doctor_result(result: dict[str, Any]) -> None:
    message = str(result["message"])
    if result["ok"]:
        console.print(f"[green]{message}[/green]")
    else:
        console.print("[yellow]Chromium is unavailable.[/yellow]")
        console.print(message)
        if hint := result.get("hint"):
            console.print(str(hint))
    _print_doctor_details(result)


def _print_doctor_details(result: dict[str, Any]) -> None:
    """Print the surrounding facts, skipping any a caller stubbed out."""
    if identity := result.get("browser_identity"):
        console.print(f"[bold]Browser:[/bold] {identity}")
    if architecture := result.get("cpu_architecture"):
        console.print(f"[bold]CPU architecture:[/bold] {architecture}")
    if (fonts := result.get("font_count")) is not None:
        console.print(f"[bold]Font families:[/bold] {fonts}")
    if versions := result.get("versions"):
        installed = ", ".join(f"{name} {value or 'missing'}" for name, value in versions.items())
        console.print(f"[bold]Versions:[/bold] {installed}")
    if executable := result.get("executable_path"):
        console.print(f"[bold]Chromium:[/bold] {executable}", soft_wrap=True)
    if paths := result.get("paths"):
        for label, path in paths.items():
            console.print(f"[bold]{label}:[/bold] {path}", soft_wrap=True)
    if environment := result.get("environment"):
        overrides = {name: value for name, value in environment.items() if value is not None}
        console.print(f"[bold]Environment:[/bold] {overrides or 'defaults'}")
    if (sessions := result.get("sessions")) is not None:
        running = sum(1 for entry in sessions if entry["running"])
        console.print(f"[bold]Sessions:[/bold] {len(sessions)} ({running} running)")
    if timezone := result.get("host_timezone"):
        console.print(f"[bold]Host timezone:[/bold] {timezone}")
    if (mesa := result.get("mesa_gpu_available")) is not None:
        console.print(
            f"[bold]Mesa GPU (gpu=mesa):[/bold] {'available' if mesa else 'not installed'}"
        )
    for warning in result.get("warnings") or []:
        console.print(f"[yellow]warning:[/yellow] {warning}")
