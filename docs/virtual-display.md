# Native virtual displays

`virtual_display=True` runs headed Chromium on a real display. Linux uses Xvfb.
macOS uses CoreGraphics `CGVirtualDisplay` through Python's `ctypes` bindings.
Windows uses preconfigured monitors from the open-source
[Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver).
No backend substitutes headless mode or `--screen-info` for a display.
No extra Python package is required.

```python
from webskrap import SessionConfig, WebSkrapClient
from webskrap.models import Viewport

config = SessionConfig(
    virtual_display=True,
    headless_screen=Viewport(width=1280, height=720),
)

async with WebSkrapClient(default_config=config) as client:
    session = await client.session("example", config=config)
    page = await session.new_page()
    await page.goto("https://example.com")
```

`headless_screen` keeps its existing name for compatibility. With a virtual
display, it selects the native display resolution. The browser CLI and MCP keep
their existing `virtual_display` option. Relative window positions are placed
within the selected display. A missing backend produces a `browser_launch`
error. It does not silently switch browser modes.

## Linux

1. Install Xvfb with your system package manager.
2. Install `mesa-vulkan-drivers` if you need the Mesa software renderer.
3. Use `virtual_display=True`.

Each session gets its own X server, authentication cookie and temporary
directory. The Linux Mesa GPU selection from PR #90 stays in effect. On a host
without hardware 3D, `gpu="auto"` can select Mesa lavapipe for headed Chromium.
WebGPU availability still depends on Chromium and the renderer. A real display
does not ensure a WebGPU adapter.

## macOS

Run Python from a logged-in GUI session. A service without access to WindowServer
cannot create a usable display. Turn off **Displays have separate Spaces** in
System Settings > Desktop & Dock > Mission Control, then log out and back in.
With separate Spaces, macOS moves each new window to the active display, so
Chrome cannot stay on the virtual display. The backend refuses to start while
the setting is on. The backend creates one display per session at
the requested size, without HiDPI scaling. Its native Objective-C calls follow
[Chromium's virtual display test implementation](https://github.com/chromium/chromium/blob/main/ui/display/mac/test/virtual_display_util_mac.mm).
Each descriptor has a nonzero vendor ID and a separate random serial number.
macOS 14 requires those values for concurrent virtual displays.

`CGVirtualDisplay` is a private API. Apple can change it between macOS releases.
Creation is checked at runtime. No PyObjC package or compiled helper is required.
A Python helper keeps the native object alive for persistent browser sessions.
The OS owns display removal when that helper exits.

Adding, moving or removing a display makes macOS redraw every display, and
the screen goes black for a moment. The backend keeps these changes few. It
keeps the place macOS gives a new display, usually left of the desktop. Only a
mirrored or overlapping display moves right of the desktop, in logical
CoreGraphics points, for the login session. That move waits until WindowServer
reports it, because a browser launched earlier keeps the old layout. A per-user
lock serializes these changes. A stopped temporary session's display then
waits 15 seconds for the next session of the same size before macOS removes
it, so back-to-back sessions share one display. `webskrap.display.release_idle()`
removes waiting displays at once. Persistent sessions always get their own.
Each display's random serial leaves a stale entry in `com.apple.spaces`.

Display creation alone does not assign Chrome to it.
After launch, both temporary and persistent sessions move Chrome's native window
through CDP and read its real screen dimensions and window position. A session
fails and cleans up if Chrome remains on the physical screen. No screen values
are emulated or rewritten. Chrome may briefly show its startup window before
placement completes; this is not an isolated or hidden macOS desktop.

The virtual display extends the user's desktop. Other desktop apps can observe
it, and it appears in Displays settings. This does not provide Xvfb's separate
X server. Test creation, screen assignment, concurrent display arrangement,
GPU rendering and removal on each supported macOS release before deployment.

## Windows

1. Install the signed [Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver/wiki) as an administrator.
2. Configure `C:\VirtualDisplayDriver\vdd_settings.xml` with enough monitors in
   `<monitors><count>` for your concurrent sessions.
3. Add each needed size under `<resolutions>` and restart the driver as its
   documentation requires.
4. Set the displays to 100% scaling for the initial validation.
5. Run Python inside the logged-in desktop session.

WebSkrap leases one monitor from that pool. Python calls
[`EnumDisplayDevicesW`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumdisplaydevicesw),
`EnumDisplaySettingsW` and
[`ChangeDisplaySettingsExW`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw)
to find the driver, set the requested mode and place Chromium on the monitor.
A byte-range lock prevents two WebSkrap sessions of the same Windows user from
leasing the same monitor. A separate lock serializes desktop layout changes.
An unsupported size or an exhausted pool produces an explicit error.

The runtime does not install drivers, create driver devices, alter certificates,
or hotplug a monitor per session. Cleanup restores each monitor's previous mode
and attachment state. The configured driver and its monitor pool remain
installed. A journal saved before a mode change permits restoration on the next
lease if the helper is terminated before cleanup. Recovery refuses a journal
whose display identity has changed. Keep driver configuration and desktop layout
stable while sessions run. Locks do not coordinate different Windows users.

Windows displays share the desktop. This does not provide an isolated desktop
or an invisible window. Mixed DPI layouts, RDP reconnects, monitor renumbering,
Session 0 services and GPU driver changes require separate physical tests.
Mixed DPI placement is not verified. WebSkrap does not change DPI settings or
rewrite browser fingerprint values to compensate.

## Lifecycle and crash recovery

Normal session close stops Chromium and releases its display. Persistent browser
sessions keep the helper until browser close or recovery of a dead browser.
Temporary macOS and Windows helpers also watch their owner's input pipe and
release the display when that owner dies. Force-killing a Linux owner can leave
its Xvfb alive until session cleanup. Force-killing a Windows helper can leave
the changed layout until the next lease recovers the journal. macOS removes its
process-owned virtual display after helper death.

Do not change a leased monitor manually. Restoring the recorded mode can undo
such changes. None of the backends grants a security boundary against other
processes of the same OS user.

## Integration tests and CI

The `Virtual displays` workflow installs Chromium and runs real headed-browser
tests on Linux, macOS and Windows hosted runners. Linux installs Xvfb and Mesa.
Windows runs `scripts/ci/install-vdd.ps1` on a disposable runner. The setup script
pins VDD 25.7.23 and nefcon 1.21.1 archives with SHA-256 checks, verifies the catalog signature, imports only its
signer certificate into TrustedPublisher, configures two monitors and waits for native
enumeration. It refuses to overwrite existing driver settings. Do not run this
CI installer on a personal or shared machine.

The tests require the backend, so driver setup or display failures fail the job.
They check CDP connection, requested screen size, canvas and WebGL pixel readback,
headed browser identity, client hints, pointer media queries, concurrent sessions,
persistent browser crash recovery, helper exit, native display removal or
restored Windows modes, and temporary directory cleanup. JSON probe
results are uploaded as workflow artifacts. WebGPU is probed separately because
a VM or software GPU can expose the API without providing an adapter.

[GitHub documents](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
that hosted Windows runners have administrator rights. It does not guarantee
IddCx driver support, a usable interactive desktop or GPU acceleration. Hosted
macOS likewise does not guarantee this private CoreGraphics API or GPU features.
A failed hosted job must be investigated from its logs and probe artifacts.
It must not be reclassified as a passing mock test.

The workflow has an optional `physical` input for manually dispatched runs.
It targets self-hosted runners labelled `virtual-display` plus `macOS` or
`Windows`. Provision these machines with a logged-in desktop. Install and
configure the Windows driver in advance. Run their Actions runner interactively,
rather than as a Session 0 Windows service. Dispatch only trusted repository
code to these machines. This path does not install or replace drivers.

## Verified hosted run, 2026-11-17

[Run 44088360042](https://github.com/kacigaya/webskrap/actions/runs/44088360042)
passed on all three hosted operating systems at code commit `ad22f0a`:

- Linux: 55 passed, 2 skipped.
- macOS 26.7.2 ARM64: 53 passed, 4 skipped.
- Windows Server 2025: 53 passed, 4 skipped.

All three real-browser integration tests passed on every OS. Other skips are
platform-specific tests. The driver installed successfully on the disposable
Windows runner and exposed two monitors. The tests verified requested screen
sizes, separate concurrent sessions, persistent browser recovery, rendering,
native display cleanup and Windows mode restoration.

macOS rendered WebGL through Apple's paravirtual Metal device and passed WebGPU
blue-pixel readback. Linux rendered WebGL through Mesa lavapipe. Windows rendered
through Microsoft's Basic Render Driver. Linux and Windows exposed WebGPU but
returned no adapter, so WebGPU rendering was not tested on those runners.
The persistent Windows Chrome browser lost its WebGL context after the WebGPU
adapter probe; its WebGL readback and renderer were captured before that probe.
The reports record this context loss. No browser flags or fingerprints were
changed to hide it.

Physical GPU behavior, mixed DPI, RDP reconnects and coverage of older OS releases
remain unverified. The optional physical workflow job was not run. Hosted virtual
GPU results do not establish those physical-machine behaviors.

The final local non-live suite passed 1,125 tests with 87.83% coverage. Two
Windows-only tests were skipped locally; the hosted Windows job ran them. Ruff,
format checks, Pyright for all three platforms, Bandit, pip-audit, OSV checks
for 51 locked public packages, build and Twine checks passed.

## Physical Mac placement regression

A physical Intel Mac reported successful display creation but Chrome 156 stayed
on its 1792x1120 physical screen. The earlier hosted result did not cover that
Retina desktop arrangement. The placement follow-up adds explicit native display
arrangement, CDP movement and screen-assignment checks. Its physical Mac retest
is still required; no linked Mac was available during the local implementation.
The earlier hosted results above apply to the recorded commit, not this follow-up.

To retest with installed Google Chrome, without downloading Chrome for Testing:

```bash
WEBSKRAP_REQUIRE_VIRTUAL_DISPLAY=1 \
WEBSKRAP_VIRTUAL_DISPLAY_CHANNEL=chrome \
WEBSKRAP_VIRTUAL_DISPLAY_REPORT=/tmp/webskrap-mac-display-report \
uv run --extra dev pytest -q tests/test_virtual_display_integration.py
```

The channel override selects the browser for temporary integration sessions.
Persistent sessions keep their normal installed-Chrome-first selection. Reports
include native screen sizes and window positions. The tests also check that the
window center lies within its assigned display, even when two displays have the
same resolution. No new Actions run was started for this follow-up.
