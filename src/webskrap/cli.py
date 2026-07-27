from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from webskrap.client import WebSkrapClient
from webskrap.models import (
    FetchResult,
    ResourcePolicy,
    SessionConfig,
    WaitUntil,
    WebRtcIPHandlingPolicy,
    shape_fetch_result,
)
from webskrap.parsing import (
    parse_wait_until,
    parse_webrtc_ip_handling_policy,
)
from webskrap.profiles import get_profile, list_profiles

app = typer.Typer(help="WebSkrap browser scraping toolkit.")
console = Console()
OutputFormat = Literal["human", "json"]
INSTALL_COMMANDS = (
    (sys.executable, "-m", "playwright", "install", "chromium"),
    (sys.executable, "-m", "patchright", "install", "chromium"),
)
UPDATE_CHECK_URL = "https://pypi.org/pypi/webskrap/json"
UPDATE_CHECK_INTERVAL = 86_400  # once per day
UPDATE_CHECK_CACHE = Path.home() / ".webskrap" / "update-check.json"
# ponytail: ~/.webskrap not XDG/APPDATA-aware; swap to platformdirs if that matters


def _is_newer(latest: str, current: str) -> bool:
    # ponytail: naive X.Y.Z compare; swap to packaging.version if pre-release tags ever ship
    try:
        return tuple(map(int, latest.split("."))) > tuple(map(int, current.split(".")))
    except ValueError:
        return False


def _check_for_update() -> None:
    """Best-effort 'update available' notice. Never raises, never touches stdout."""
    try:
        if (
            os.environ.get("WEBSKRAP_NO_UPDATE_CHECK")
            or os.environ.get("CI")
            or not sys.stderr.isatty()
        ):
            return

        current = metadata.version("webskrap")
        latest: str | None = None

        try:
            cached = json.loads(UPDATE_CHECK_CACHE.read_text())
            if time.time() - cached["checked_at"] < UPDATE_CHECK_INTERVAL:
                latest = cached["latest"]
        except Exception:
            latest = None

        if latest is None:
            fetched: str | None = None
            try:
                with urllib.request.urlopen(UPDATE_CHECK_URL, timeout=2) as response:
                    fetched = json.load(response)["info"]["version"]
            except Exception:
                fetched = None
            # Stamp the attempt either way so a PyPI outage can't cause hammering.
            latest = fetched or current
            try:
                UPDATE_CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
                UPDATE_CHECK_CACHE.write_text(
                    json.dumps({"checked_at": time.time(), "latest": latest})
                )
            except Exception:
                pass

        if _is_newer(latest, current):
            Console(stderr=True, highlight=False).print(
                f"[yellow]webskrap {latest} available[/] (you have {current}) — "
                "upgrade: [bold]pip install -U webskrap[/]"
            )
    except Exception:
        return


@app.callback()
def _main() -> None:
    _check_for_update()


@app.command("install")
def install_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    output_format = _parse_output_format(format)
    results = [_run_install_command(command) for command in INSTALL_COMMANDS]
    ok = all(result["ok"] for result in results)
    payload = {"ok": ok, "steps": results}
    if output_format == "json":
        _print_json(payload)
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
    output_format = _parse_output_format(format)
    profiles = list_profiles()
    if output_format == "json":
        _print_json({"profiles": [profile.model_dump(mode="json") for profile in profiles]})
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


@app.command("doctor")
def doctor_command(
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: human or json."),
    ] = "human",
) -> None:
    output_format = _parse_output_format(format)
    result = asyncio.run(_doctor())
    if output_format == "json":
        _print_json(result)
    else:
        _print_doctor_result(result)
    if not result["ok"]:
        raise typer.Exit(code=1)


async def _doctor() -> dict[str, object]:
    try:
        from patchright.async_api import async_playwright
    except Exception as exc:
        return {
            "ok": False,
            "message": f"Patchright import failed: {exc}",
            "hint": "Run: webskrap install",
        }

    # The chrome channel is unavailable on some platforms (Linux ARM64), where
    # bundled chromium still works. Report the best channel that launches
    # instead of failing the whole check.
    failure: Exception | None = None
    for channel in ("chrome", None):
        try:
            manager = async_playwright()
            playwright = await manager.start()
            browser = await playwright.chromium.launch(channel=channel, headless=True)
            await browser.close()
            await playwright.stop()
        except Exception as exc:  # noqa: BLE001 - try the next channel
            failure = exc
            continue
        label = channel or "chromium"
        return {
            "ok": True,
            "message": f"Patchright headless {label} is ready.",
            "channel": label,
        }

    return {
        "ok": False,
        "message": f"Patchright headless Chrome did not launch: {failure}",
        "hint": "Run: webskrap install",
    }


@app.command("fetch")
def fetch_command(
    url: Annotated[str, typer.Argument(help="URL to fetch.")],
    profile: Annotated[
        str,
        typer.Option("--profile", "-p", help="Bundled profile name."),
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
    stdout: Annotated[
        bool,
        typer.Option("--stdout", help="Write fetched content to stdout."),
    ] = False,
    text_only: Annotated[
        bool,
        typer.Option("--text-only", help="Return readable body text instead of HTML."),
    ] = False,
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
            help="Rewrite HeadlessChrome to Chrome via Chromium's user-agent flag.",
        ),
    ] = False,
    launch_args: Annotated[
        list[str] | None,
        typer.Option(
            "--launch-arg",
            help="Additional browser launch argument. Repeat for multiple args.",
        ),
    ] = None,
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
            stdout=stdout,
            text_only=text_only,
            quiet=quiet,
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            resource_policy=resource_policy,
            decline_cookies=decline_cookies,
            decline_cookies_timeout_ms=decline_cookies_timeout_ms,
            patchright_context_profile=patchright_context_profile,
            reduce_fingerprint_surface=reduce_fingerprint_surface,
            mask_headless_user_agent=mask_headless_user_agent,
            launch_args=launch_args or [],
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
    stdout: bool,
    text_only: bool,
    quiet: bool,
    wait_until: str,
    timeout_ms: float,
    resource_policy: ResourcePolicy,
    decline_cookies: bool,
    decline_cookies_timeout_ms: float,
    patchright_context_profile: bool,
    reduce_fingerprint_surface: bool,
    mask_headless_user_agent: bool,
    launch_args: list[str],
    webrtc_ip_handling_policy: str | None,
) -> None:
    parsed_output_format = _parse_output_format(output_format)
    selected_profile = get_profile(profile)
    config = SessionConfig(
        driver="patchright",
        headless=True,
        channel=channel,
        user_data_dir=user_data_dir,
        navigation_timeout_ms=timeout_ms,
        resource_policy=resource_policy,
        decline_cookies=decline_cookies,
        decline_cookies_timeout_ms=decline_cookies_timeout_ms,
        patchright_context_profile=patchright_context_profile,
        reduce_fingerprint_surface=reduce_fingerprint_surface,
        mask_headless_user_agent=mask_headless_user_agent,
        launch_args=launch_args,
        webrtc_ip_handling_policy=_parse_webrtc_ip_handling_policy(webrtc_ip_handling_policy),
    )

    result = await _fetch_with_channel_fallback(
        config,
        url=url,
        profile=selected_profile,
        wait_until=_parse_wait_until(wait_until),
        screenshot=screenshot or False,
        timeout_ms=timeout_ms,
        text_only=text_only,
    )

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.text, encoding="utf-8")

    if parsed_output_format == "json":
        _print_json(shape_fetch_result(result, max_chars))
        return

    if stdout:
        typer.echo(result.text, nl=False)
        return

    if quiet:
        return

    console.print(f"[bold]Status:[/bold] {result.status}")
    console.print(f"[bold]Final URL:[/bold] {result.final_url}")
    console.print(f"[bold]Title:[/bold] {result.title}")
    if result.cookie_notice_declined:
        console.print(f"[bold]Cookie notice:[/bold] declined ({result.cookie_notice_declined})")
    if result.screenshot_path:
        console.print(f"[bold]Screenshot:[/bold] {result.screenshot_path}")
    if output:
        console.print(f"[bold]HTML:[/bold] {output}")


LAUNCH_FAILURE_MARKERS = (
    "executable doesn't exist",
    "is not found at",
    "playwright install",
    "failed to launch",
    "browsertype.launch",
)


def _is_launch_failure(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in LAUNCH_FAILURE_MARKERS)


def _fail_launch(exc: Exception) -> NoReturn:
    """Report an unlaunchable browser the way `doctor` does, not as a traceback."""
    detail = str(exc).strip().splitlines()
    stderr = Console(stderr=True, highlight=False)
    stderr.print(f"[red]Browser did not launch:[/red] {detail[0] if detail else exc}")
    stderr.print("Run: [bold]webskrap install[/bold]")
    raise typer.Exit(code=1)


async def _run_fetch(config: SessionConfig, **kwargs: Any) -> FetchResult:
    async with WebSkrapClient() as client:
        return await client.fetch(config=config, **kwargs)


async def _fetch_with_channel_fallback(config: SessionConfig, **kwargs: Any) -> FetchResult:
    """Fetch, retrying on bundled chromium when the chosen channel cannot launch.

    The default channel is `chrome`, which does not exist on every platform
    (Linux ARM64 has no Chrome build). Falling back keeps `webskrap fetch`
    working there instead of dumping a Playwright traceback.
    """
    try:
        return await _run_fetch(config, **kwargs)
    except Exception as exc:
        if not _is_launch_failure(exc):
            raise
        if config.channel is None:
            _fail_launch(exc)
        Console(stderr=True, highlight=False).print(
            f"[yellow]channel '{config.channel}' did not launch; retrying with chromium[/yellow]"
        )
        try:
            return await _run_fetch(config.model_copy(update={"channel": None}), **kwargs)
        except Exception as retry_exc:
            if not _is_launch_failure(retry_exc):
                raise
            _fail_launch(retry_exc)


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


def _parse_output_format(value: str) -> OutputFormat:
    if value not in ("human", "json"):
        raise typer.BadParameter("must be one of: human, json")
    return value


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False))


def _run_install_command(command: tuple[str, ...]) -> dict[str, object]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
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


def _print_install_result(results: list[dict[str, object]]) -> None:
    for result in results:
        command = " ".join(str(part) for part in result["command"])
        if result["ok"]:
            console.print(f"[green]OK:[/green] {command}")
        else:
            console.print(f"[red]FAILED:[/red] {command}")
        if result["message"]:
            console.print(str(result["message"]))


def _print_doctor_result(result: dict[str, object]) -> None:
    message = str(result["message"])
    if result["ok"]:
        console.print(f"[green]{message}[/green]")
        return
    if message.startswith("Patchright import failed: "):
        detail = message.removeprefix("Patchright import failed: ")
        console.print(f"[red]Patchright import failed:[/red] {detail}")
    else:
        console.print(
            "[yellow]Patchright is installed, but headless Chrome did not launch.[/yellow]"
        )
        console.print(message.removeprefix("Patchright headless Chrome did not launch: "))
    if hint := result.get("hint"):
        console.print(str(hint))
