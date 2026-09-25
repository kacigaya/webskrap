"""Typer entry point for the ``webskrap`` command.

Subcommands cover installing browsers, listing profiles, checking the
install, fetching pages, and searching; persistent-session commands live in
:mod:`webskrap.browser_cli` under ``webskrap browser``. This layer parses
arguments, formats output, and turns failures into one-line errors with a
non-zero exit code.
"""

from __future__ import annotations

import asyncio
import subprocess  # nosec B404  # noqa: S404 - fixed argv for browser installs, no shell
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, NoReturn, Protocol, TypedDict, TypeVar

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from webskrap.browser_cli import browser_app
from webskrap.browser_session import sandbox_enabled
from webskrap.cli_output import (
    OutputFormat,
    fail,
    parse_output_format,
    print_json,
    stderr_console,
)
from webskrap.client import WebSkrapClient
from webskrap.diagnostics import diagnose
from webskrap.errors import ErrorCode, WebSkrapError, first_line, is_sandbox_failure
from webskrap.models import (
    FetchResult,
    GpuBackend,
    ResourcePolicy,
    SearchEngine,
    SearchResult,
    SessionConfig,
    WaitUntil,
    WebRtcIPHandlingPolicy,
    shape_fetch_result,
    shape_search_result,
)
from webskrap.parsing import (
    parse_wait_until,
    parse_webrtc_ip_handling_policy,
)
from webskrap.profiles import get_profile, list_profiles

app = typer.Typer(help="WebSkrap browser scraping toolkit.")
app.add_typer(browser_app, name="browser")
console = Console()

T = TypeVar("T")


class InstallResult(TypedDict):
    """Outcome of one browser-install step."""

    ok: bool
    command: list[str]
    message: str


INSTALL_COMMANDS = (
    (sys.executable, "-m", "playwright", "install", "chromium"),
    (sys.executable, "-m", "patchright", "install", "chromium"),
)


@app.command("install")
def install_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    """Download the Chromium builds Playwright and Patchright need."""
    output_format = parse_output_format(format)
    results = [_run_install_command(command) for command in INSTALL_COMMANDS]
    ok = all(result["ok"] for result in results)
    payload = {"ok": ok, "steps": results}
    if output_format == "json":
        print_json(payload)
    else:
        _print_install_result(results)
    if not ok:
        raise typer.Exit(code=1)


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


@app.command("doctor")
def doctor_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    """Check that Patchright and Chromium are installed and can launch."""
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
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            "-p",
            help="Bundled profile metadata (requires --patchright-context-profile).",
        ),
    ] = "desktop-chrome",
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="Browser channel for headless Patchright stealth."),
    ] = "chrome",
    user_data_dir: Annotated[
        Path | None,
        typer.Option("--user-data-dir", help="Persistent browser profile directory."),
    ] = None,
    screenshot: Annotated[
        Path | None,
        typer.Option("--screenshot", help="Write a full-page screenshot to this path."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write fetched content to this file."),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
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
            help="Click the reject button of a cookie consent notice after load.",
        ),
    ] = True,
    decline_cookies_timeout_ms: Annotated[
        float,
        typer.Option(
            "--decline-cookies-timeout-ms",
            min=0,
            help="How long to wait for a cookie consent notice to appear.",
        ),
    ] = 2_000,
    patchright_context_profile: Annotated[
        bool,
        typer.Option(
            "--patchright-context-profile",
            help="Apply locale/timezone/media profile metadata in Patchright contexts.",
        ),
    ] = False,
    reduce_fingerprint_surface: Annotated[
        bool,
        typer.Option(
            "--reduce-fingerprint-surface",
            help="Disable Chromium WebGL and canvas readback with native browser flags.",
        ),
    ] = False,
    mask_headless_user_agent: Annotated[
        bool,
        typer.Option(
            "--mask-headless-user-agent",
            help=(
                "Rewrite HeadlessChrome to Chrome via Chromium's user-agent flag. "
                "Empties high-entropy client hints; prefer --virtual-display."
            ),
        ),
    ] = False,
    virtual_display: Annotated[
        bool,
        typer.Option(
            "--virtual-display",
            help="Run headed Chromium on a private Xvfb display instead of headless (Linux).",
        ),
    ] = False,
    gpu: Annotated[
        GpuBackend,
        typer.Option(
            "--gpu",
            help=(
                "WebGL renderer: auto (Chromium's choice, SwiftShader without a GPU) or "
                "mesa (Mesa lavapipe; Linux, mesa-vulkan-drivers)."
            ),
        ),
    ] = GpuBackend.AUTO,
    fake_media_devices: Annotated[
        bool,
        typer.Option("--fake-media-devices", help="Use synthetic Chromium camera and microphone."),
    ] = False,
    launch_args: Annotated[
        list[str] | None,
        typer.Option(
            "--launch-arg",
            help="Additional browser launch argument. Repeat for multiple args.",
        ),
    ] = None,
    no_sandbox: Annotated[
        bool,
        typer.Option(
            "--no-sandbox",
            help="Disable Chromium's OS sandbox (weakens isolation; only where it cannot start).",
        ),
    ] = False,
    webrtc_ip_handling_policy: Annotated[
        str | None,
        typer.Option(
            "--webrtc-ip-handling-policy",
            help=(
                "Chromium WebRTC IP policy: default, default_public_and_private_interfaces, "
                "default_public_interface_only, or disable_non_proxied_udp."
            ),
        ),
    ] = None,
) -> None:
    """Fetch a URL with the Patchright stealth driver and report the result."""
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
            timeout_ms=timeout_ms,
            resource_policy=resource_policy,
            decline_cookies=decline_cookies,
            decline_cookies_timeout_ms=decline_cookies_timeout_ms,
            patchright_context_profile=patchright_context_profile,
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
    timeout_ms: float,
    resource_policy: ResourcePolicy,
    decline_cookies: bool,
    decline_cookies_timeout_ms: float,
    patchright_context_profile: bool,
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
    config = SessionConfig(
        driver="patchright",
        headless=True,
        channel=channel,
        chromium_sandbox=sandbox_enabled(False if no_sandbox else None),
        user_data_dir=user_data_dir,
        navigation_timeout_ms=timeout_ms,
        resource_policy=resource_policy,
        decline_cookies=decline_cookies,
        decline_cookies_timeout_ms=decline_cookies_timeout_ms,
        patchright_context_profile=patchright_context_profile,
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        virtual_display=virtual_display,
        gpu=gpu,
        fake_media_devices=fake_media_devices,
        launch_args=launch_args,
        webrtc_ip_handling_policy=_parse_webrtc_ip_handling_policy(webrtc_ip_handling_policy),
    )

    try:
        result = await _fetch_with_channel_fallback(
            config,
            parsed_output_format,
            url=url,
            profile=selected_profile,
            wait_until=_parse_wait_until(wait_until),
            screenshot=screenshot or False,
            timeout_ms=timeout_ms,
            text_only=text_only,
            include_links=links,
            max_links=max_links,
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
        console.print(f"[bold]Screenshot:[/bold] {result.screenshot_path}")
    if output:
        label = "Text" if text_only else "HTML"
        console.print(f"[bold]{label}:[/bold] {output}")


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
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            "-p",
            help="Bundled profile metadata (requires --patchright-context-profile).",
        ),
    ] = "desktop-chrome",
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="Browser channel for headless Patchright stealth."),
    ] = "chrome",
    user_data_dir: Annotated[
        Path | None,
        typer.Option("--user-data-dir", help="Persistent browser profile directory."),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
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
    decline_cookies_timeout_ms: Annotated[
        float,
        typer.Option(
            "--decline-cookies-timeout-ms",
            min=0,
            help="How long to wait for a cookie consent notice to appear.",
        ),
    ] = 2_000,
    patchright_context_profile: Annotated[
        bool,
        typer.Option(
            "--patchright-context-profile",
            help="Apply locale/timezone/media profile metadata in Patchright contexts.",
        ),
    ] = False,
    reduce_fingerprint_surface: Annotated[
        bool,
        typer.Option(
            "--reduce-fingerprint-surface",
            help="Disable Chromium WebGL and canvas readback with native browser flags.",
        ),
    ] = False,
    mask_headless_user_agent: Annotated[
        bool,
        typer.Option(
            "--mask-headless-user-agent",
            help=(
                "Rewrite HeadlessChrome to Chrome via Chromium's user-agent flag. "
                "Empties high-entropy client hints; prefer --virtual-display."
            ),
        ),
    ] = False,
    virtual_display: Annotated[
        bool,
        typer.Option(
            "--virtual-display",
            help="Run headed Chromium on a private Xvfb display instead of headless (Linux).",
        ),
    ] = False,
    gpu: Annotated[
        GpuBackend,
        typer.Option(
            "--gpu",
            help=(
                "WebGL renderer: auto (Chromium's choice, SwiftShader without a GPU) or "
                "mesa (Mesa lavapipe; Linux, mesa-vulkan-drivers)."
            ),
        ),
    ] = GpuBackend.AUTO,
    fake_media_devices: Annotated[
        bool,
        typer.Option("--fake-media-devices", help="Use synthetic Chromium camera and microphone."),
    ] = False,
    launch_args: Annotated[
        list[str] | None,
        typer.Option(
            "--launch-arg",
            help="Additional browser launch argument. Repeat for multiple args.",
        ),
    ] = None,
    no_sandbox: Annotated[
        bool,
        typer.Option(
            "--no-sandbox",
            help="Disable Chromium's OS sandbox (weakens isolation; only where it cannot start).",
        ),
    ] = False,
    webrtc_ip_handling_policy: Annotated[
        str | None,
        typer.Option(
            "--webrtc-ip-handling-policy",
            help=(
                "Chromium WebRTC IP policy: default, default_public_and_private_interfaces, "
                "default_public_interface_only, or disable_non_proxied_udp."
            ),
        ),
    ] = None,
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
            patchright_context_profile=patchright_context_profile,
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
    patchright_context_profile: bool,
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
    config = SessionConfig(
        driver="patchright",
        headless=True,
        channel=channel,
        chromium_sandbox=sandbox_enabled(False if no_sandbox else None),
        user_data_dir=user_data_dir,
        navigation_timeout_ms=timeout_ms,
        resource_policy=resource_policy,
        decline_cookies=decline_cookies,
        decline_cookies_timeout_ms=decline_cookies_timeout_ms,
        patchright_context_profile=patchright_context_profile,
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        virtual_display=virtual_display,
        gpu=gpu,
        fake_media_devices=fake_media_devices,
        launch_args=launch_args,
        webrtc_ip_handling_policy=_parse_webrtc_ip_handling_policy(webrtc_ip_handling_policy),
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


LAUNCH_FAILURE_MARKERS = (
    "executable doesn't exist",
    "is not found at",
    "playwright install",
    "failed to launch",
    "browsertype.launch",
)


def _is_launch_failure(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in LAUNCH_FAILURE_MARKERS)


def _fail_launch(exc: Exception, output_format: OutputFormat) -> NoReturn:
    """Report an unlaunchable browser the way `doctor` does, not as a traceback."""
    if is_sandbox_failure(exc):
        # Playwright's first line only says the browser closed; the cause is
        # in the browser log below it.
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
    """Run, trying Edge and bundled Chromium when Chrome cannot launch.

    The default channel is `chrome`, which does not exist on every platform
    (Linux ARM64 has no Chrome build). An installed Edge is tried before the
    bundled Chromium. Sandbox failures and non-launch errors are not retried.
    """
    fallbacks = ("msedge", "chromium") if config.channel == "chrome" else ("chromium",)
    channels = (config.channel, *(() if config.channel in (None, "chromium") else fallbacks))
    last_failure: Exception | None = None
    for index, channel in enumerate(channels):
        try:
            selected = config if index == 0 else config.model_copy(update={"channel": channel})
            return await run(selected)
        except Exception as exc:
            if not _is_launch_failure(exc):
                raise
            if is_sandbox_failure(exc):
                _fail_launch(exc, output_format)
            last_failure = exc
            if index + 1 < len(channels):
                stderr_console.print(
                    f"[yellow]channel '{channel}' did not launch; "
                    f"retrying with {channels[index + 1]}[/yellow]"
                )
    if last_failure is None:
        raise RuntimeError("no browser channel to launch")
    _fail_launch(last_failure, output_format)


async def _fetch_with_channel_fallback(
    config: SessionConfig, output_format: OutputFormat, **kwargs: Any
) -> FetchResult:
    return await _with_channel_fallback(
        lambda resolved: _run_fetch(resolved, **kwargs), config, output_format
    )


def _parse_wait_until(value: str) -> WaitUntil:
    try:
        return parse_wait_until(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc).partition(" must ")[2]) from exc


def _parse_webrtc_ip_handling_policy(
    value: str | None,
) -> WebRtcIPHandlingPolicy | None:
    try:
        return parse_webrtc_ip_handling_policy(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc).partition(" must ")[2]) from exc


def _run_install_command(command: tuple[str, ...]) -> InstallResult:
    try:
        # argv is one of the INSTALL_COMMANDS constants, built from
        # sys.executable. No shell, and no caller-supplied argument.
        completed = subprocess.run(  # nosec B603  # noqa: S603
            command, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        return {
            "ok": False,
            "command": list(command),
            "message": str(exc),
        }
    output = (completed.stdout or completed.stderr).strip()
    return {
        "ok": completed.returncode == 0,
        "command": list(command),
        "message": output,
    }


def _print_install_result(results: list[InstallResult]) -> None:
    for result in results:
        command = " ".join(str(part) for part in result["command"])
        if result["ok"]:
            console.print(f"[green]OK:[/green] {command}")
        else:
            console.print(f"[red]FAILED:[/red] {command}")
        if result["message"]:
            console.print(str(result["message"]))


def _print_doctor_result(result: dict[str, Any]) -> None:
    message = str(result["message"])
    if result["ok"]:
        console.print(f"[green]{message}[/green]")
    else:
        console.print("[yellow]Patchright is unavailable.[/yellow]")
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
        console.print(f"[bold]Chromium:[/bold] {executable}")
    if paths := result.get("paths"):
        for label, path in paths.items():
            console.print(f"[bold]{label}:[/bold] {path}")
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


def _json_safe(value: Any) -> Any:
    """Reduce a click default to something json.dumps accepts.

    Defaults reach here as enum members, Paths and tuples as often as as plain
    scalars, and a schema that cannot be serialized is no schema at all.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return str(value)


class _ParameterType(Protocol):
    """The part of a Click parameter type :func:`describe_command` reads."""

    @property
    def name(self) -> str: ...


class _Parameter(Protocol):
    """The part of a Click parameter :func:`describe_command` reads."""

    @property
    def name(self) -> str | None: ...

    @property
    def opts(self) -> Sequence[str]: ...

    @property
    def param_type_name(self) -> str: ...

    @property
    def required(self) -> bool: ...

    @property
    def multiple(self) -> bool: ...

    @property
    def default(self) -> object: ...

    @property
    def type(self) -> _ParameterType: ...


class _Command(Protocol):
    """The part of a Click command :func:`describe_command` reads.

    A structural type, not ``click.Command``: Typer builds its commands from
    its own Click-compatible classes, which are private and do not subclass
    Click's. Naming what is actually read keeps the check honest without
    reaching into ``typer._click``. Every member is read-only, which is both
    true of this use and what keeps the sequences covariant.
    """

    @property
    def help(self) -> str | None: ...

    @property
    def short_help(self) -> str | None: ...

    @property
    def params(self) -> Sequence[_Parameter]: ...


def _describe_parameter(parameter: _Parameter) -> dict[str, Any]:
    """Describe one option or positional argument.

    ``param_type_name`` is read rather than the parameter's class, because
    Typer's parameters do not subclass ``click.Argument`` or ``click.Option``
    and an isinstance check would silently call every parameter an option.
    """
    choices = getattr(parameter.type, "choices", None)
    return {
        "name": parameter.name,
        "kind": parameter.param_type_name,
        "flags": list(parameter.opts),
        "type": parameter.type.name,
        "choices": [str(choice) for choice in choices] if choices else None,
        "required": bool(parameter.required),
        "multiple": bool(parameter.multiple),
        "default": _json_safe(parameter.default),
        "help": getattr(parameter, "help", None),
    }


def describe_command(command: _Command, name: str) -> dict[str, Any]:
    """Describe ``command`` and, for a group, everything under it.

    The auto-generated ``--help`` flag is left out: it is on every command and
    tells a caller nothing about that one.
    """
    described: dict[str, Any] = {
        "name": name,
        "help": (command.help or command.short_help or "").strip().split("\n\n")[0].strip(),
        "parameters": [
            _describe_parameter(parameter)
            for parameter in command.params
            if parameter.name != "help"
        ],
    }
    if children := getattr(command, "commands", None):
        described["commands"] = [
            describe_command(child, child_name) for child_name, child in sorted(children.items())
        ]
    return described
