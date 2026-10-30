# DataDome website navigation diagnosis

Checked on 2026-10-29 against a DataDome-protected website.

## Root cause

The request reaches the website through the existing Gluetun VPN. DataDome then
chooses whether to show a device check, a CAPTCHA, or a hard block.
The headless browser received a hard-block document. That document has no
CAPTCHA widget. A headed browser on the same VPN exit received a working
manual slider. This is an external verification requirement.

WebSkrap also had three integration defects:

1. Detection recognized `ct.captcha-delivery.com/c.js`, but missed the
   `i.js` device-check script before its iframe appeared.
2. It treated the device check as a manual challenge and did not wait for
   its own script to reload the page.
3. It read the parent document's iframe `src`. A cross-origin iframe can
   navigate itself while that attribute stays unchanged. A hard block then
   received the same hint as a solvable CAPTCHA.

The optional headless user-agent probe also started a separate browser
without the session's proxy or network flags.

## Evidence

- Gluetun reported a healthy tunnel and a French exit. A browser request to
  the IP check service returned the same exit as a request from the container.
- The proxy required authentication. Browser credentials stayed in memory.
- The container resolved the website and the CAPTCHA script, frame, and asset hosts.
  Its BusyBox `nslookup` printed a parse error for one CNAME response. The
  proxy still resolved and loaded that host successfully.
- The website returned HTTP 403 with `x-datadome: protected` and `x-dd-b: 3`.
- The device-check script and iframe returned HTTP 200. In the headed run,
  CAPTCHA JavaScript, CSS, fonts, and logo also returned HTTP 200.
- Browser logs showed GPU and canvas warnings, but no failed CAPTCHA resource
  request in the captured headed flow.
- The headless provider frame contained
  `data-dd-response-page="hard-block"`. Its parent had no visible body text.
- The headed provider frame showed the manual verification slider.
- Cookies included `datadome`, `__cf_bm`, and `__cflb`. Cookie values and
  challenge URLs are excluded from this report.

## Changes

Detection now reads the active provider frame through the existing CDP frame
code. It uses provider origin and markup, not page titles or translated text.
Device checks use the existing bounded interstitial wait. Frame inspection
uses the remaining navigation budget. Manual challenges stay open in persistent
sessions. Hard blocks receive the block hint.

The user-agent probe now keeps the session's launch flags, including its
proxy and WebRTC policy. No CAPTCHA solver, token injection, provider script
patch, new dependency, or site-specific branch was added.

## Manual completion

1. Start a headed persistent browser on a display you can access.
2. Configure the VPN proxy before navigation. Keep all page resources enabled.
3. Complete the CAPTCHA yourself. Use its retry control if it has expired.
4. Continue with the same browser, profile, and proxy. Do not copy challenge
   tokens into another browser.

A temporary headed browser session and loopback VNC listener on port 15900 were used
to prepare manual completion. Both were closed when manual completion was
declined. Xvfb alone does not give remote access.

## Verification limits

The final Linux ARM64 run passed 858 tests. It excluded 30 opt-in live tests
for unrelated public sites. Coverage was 88.85%, above the 85% project gate.
The focused suite passed 123 tests before the combined run.

Ruff lint and format checks, Pyright for Linux, Windows, and macOS, Bandit,
package builds, and Twine metadata checks passed. OSV found no vulnerabilities
in the 51 public packages from the lockfile. Pip-audit also found none.
Windows and macOS runtime tests require those operating systems and were not
run on this host.

Local regression fixtures cover device-check completion, a transition to a
manual challenge, a transition to a hard block, cross-origin frame navigation,
proxy authentication, image loading, and continuation with the same cookies.
The fixture's manual button is a local test control. It does not contact or
complete real CAPTCHA verification.

Live tests reached the rendered CAPTCHA. Navigation beyond verification
cannot be confirmed until a person completes it. The provider can still
refuse the session after manual verification. No forms were submitted.

## VPN exit retest

Eight distinct French NordVPN exits were tested, including the initial
baseline. Each headed run returned HTTP 403 and a DataDome manual challenge.
Fresh browser profiles were used for the replacement exits. No CAPTCHA was
completed or bypassed. This sample did not identify a CAPTCHA-free exit.
It does not establish that IP reputation is the only input to the decision.

A control run placed the browser directly inside Gluetun's network namespace.
It used the same VPN exit without the HTTP proxy and still received a challenge.
The proxy is therefore not the sole cause.

Rotation also exposed an operational defect: Gluetun's NordVPN server catalog
was dated March 2024. Some listed endpoints timed out or rejected authentication.
The catalog was refreshed through Gluetun's existing updater. The original
catalog was backed up, and the VPN was restored to a healthy state.

The final verified exit was `187.13.198.190` in Marseille. It still required
verification. The refreshed catalog is in the container's writable layer;
container recreation can replace it because there is no catalog volume mount.

## CDP identity investigation

An ordinary headed Chromium process was launched on the same VPN exit with
no debugging port, no debugging pipe, and no WebSkrap page setup. Its native
network log recorded an HTTP 403, a device check, and then a CAPTCHA.
This establishes that CDP is not required to trigger the current challenge.
It does not reveal DataDome's private scoring rule.

A separate public fingerprint probe reported `isBot: false` for default
headed WebSkrap. It reported no CDP, webdriver, worker-value, or client-hint
inconsistency. The headed Mesa/locale configuration also passed that probe.
Both configurations still received a CAPTCHA. The headless run was
flagged for its bot user agent and weak signals, with no CDP leak reported.
These are observations from one public probe, not universal detection claims.

Local page-owned probes compared request headers and JavaScript values in
documents, cross-origin iframes, dedicated workers, and shared workers.
Two full-profile setup defects were reproduced and fixed:

1. A Windows or macOS user agent changed client hints but left the document's
   `navigator.platform` at its native Linux value. The CDP override now sets
   an unambiguous legacy platform as well. Unknown CPU values are not guessed.
2. A desktop viewport could be wider than its real browser window. The new
   regression reproduced `innerWidth=1365` with `outerWidth=780`. Setup now
   resizes the real window before applying device metrics. A missing command
   on an older browser keeps the previous behavior; other errors propagate.

The changes use Chromium's own
[user-agent override](https://chromedevtools.github.io/devtools-protocol/tot/Emulation/#method-setUserAgentOverride)
and [content-size command](https://chromedevtools.github.io/devtools-protocol/tot/Browser/#method-setContentsSize).
No JavaScript getter replacements were added.

The probe also confirmed existing limits. UA masking removes high-entropy
client hints. Cross-process frames keep their real screen and pixel ratio.
Workers keep their native legacy platform, and shared workers keep their
native user agent. Full cross-platform emulation therefore cannot promise a
coherent identity across all contexts. Native mode leaves those values intact.
Neither the fixes nor changing the GPU and locale removed the website's verification
requirement in the measured runs.

The final follow-up suite ran inside Gluetun's network namespace. It passed
872 tests with 88.89% coverage and excluded 30 opt-in live tests. The focused
suite passed 125 tests. Lint, format, Linux/Windows/macOS type checks, Bandit,
OSV, pip-audit, package builds, and Twine checks passed. Windows and macOS
runtime checks were not available on this Linux host.

## Protected form retest

A later retest compared ordinary pages with the website's quote form path.
Each run used a fresh browser profile and a VPN exit that the run had not
used before.

The browser passed DataDome on ordinary pages. On some exits the first page
returned HTTP 403 with a device check (`x-dd-b: 3`). The device check then
reloaded into the requested page with HTTP 200. On other exits the first page
returned HTTP 200 at once. DataDome's page tag ran on those pages.

The quote form path returned HTTP 403 with a CAPTCHA on every exit. This
occurred with a direct load and with a click-through from the landing page.
These changes did not remove the CAPTCHA:

1. Google Chrome stable for Linux ARM64 instead of Chrome for Testing. This
   adds the `Google Chrome` brand.
2. A locale and a timezone that match the exit country.
3. Mesa WebGL.
4. `x86` client hints instead of the host's `arm` value.
5. A 25-second pause with mouse movement and wheel scrolling on each page.
6. A four-page session with about 75 seconds of activity before the form.
7. Exits in eleven countries on eight hosting ASNs.

A desktop-like host environment did not change the result. It added Noto
and Ubuntu fonts and PulseAudio audio devices. On a fresh exit, a direct load
of the form received a device check (`x-dd-b: 3`). The check reloaded into a
CAPTCHA. The same check reloaded into HTTP 200 on ordinary pages.

SwiftShader WebGL failed the device check and received a hard block.
The VPN provider's dedicated-IP servers refused this setup's credentials,
so a dedicated exit was not tested.

A Windows user agent made the result worse: the first page returned a hard
block. Repeated fresh profiles on one exit also made the result worse. A later
visit received the CAPTCHA, or a hard block, without a device check.

The VPN provider's catalog contains only hosting ASNs. The measured limit is
therefore the exit IP class for this form path, not a browser defect that
WebSkrap can fix. A residential or mobile IP was not tested.

The retest found two WebSkrap defects:

1. Cookie decline did not search Commanders Act TrustCommander notices.
2. Full profiles put q-values in `navigator.languages` and doubled them in
   `Accept-Language`.

A later local fingerprint check found two more:

1. With a full profile, `navigator.serviceWorker.register()` never
   finished. The page and the browser both held the worker paused.
2. Mouse presses reported `PointerEvent.pressure` 0. A real mouse reports
   0.5 while a button is down.

All four are fixed and have regression tests. No CAPTCHA was solved.
