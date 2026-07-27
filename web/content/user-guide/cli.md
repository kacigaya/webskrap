---
title: WebSkrap CLI: Scrape Pages from the Terminal
description: Use the WebSkrap command-line interface to fetch pages, return JSON, save HTML, and inspect browser scraping results.
---

# CLI

WebSkrap installs the `webskrap` command.

## Install browsers

```bash
webskrap install
webskrap install --format json
```

`webskrap install` downloads the Chromium browsers used by Playwright and
Patchright. Use JSON output in scripts.

## Check environment

```bash
webskrap doctor
webskrap doctor --format json
```

The CLI `fetch` command uses headless Patchright stealth mode, so `doctor`
checks that Patchright can launch headless Chrome.

## List profiles

```bash
webskrap profiles
webskrap profiles --format json
```

## Fetch a page

```bash
webskrap fetch https://example.com
```

The default human output prints status, final URL, title, and artifact paths.

For LLMs and shell automation, use JSON:

```bash
webskrap fetch https://example.com --format json --max-chars 12000
```

JSON output includes `url`, `final_url`, `status`, `ok`, `title`, `headers`,
`text`, `text_length`, `text_truncated`, and `elapsed_ms`.

## Text and stdout

Print raw fetched content to stdout:

```bash
webskrap fetch https://example.com --stdout
```

Return readable body text instead of HTML:

```bash
webskrap fetch https://example.com --stdout --text-only
webskrap fetch https://example.com --format json --text-only
```

Suppress the human summary when writing artifacts:

```bash
webskrap fetch https://example.com --quiet --output example.html
```

## Screenshot and output

```bash
webskrap fetch https://example.com --screenshot example.png
webskrap fetch https://example.com --output example.html
webskrap fetch https://example.com -o example.html --screenshot example.png
```

Parent directories are created automatically for `--output` and `--screenshot`.

## Wait and timeout

```bash
webskrap fetch https://example.com --wait-until load --timeout-ms 60000
```

Supported wait states are `commit`, `domcontentloaded`, `load`, and
`networkidle`. Prefer `domcontentloaded` for fast HTML collection, `load` for
regular page assets, and `networkidle` only when a page actually needs a quiet
network.

## Resource policy

```bash
webskrap fetch https://example.com --resource-policy lite
webskrap fetch https://example.com --resource-policy documents -o page.html
```

`lite` blocks images, fonts, and media. `documents` also blocks stylesheets.

## Cookie banners

`webskrap fetch` clicks the reject control of a cookie consent notice before
reading the page, so banners do not bury the text and no optional cookies are
accepted. The human output prints `Cookie notice: declined (...)` when it fired,
and the JSON output carries `cookie_notice_declined`.

```bash
webskrap fetch https://example.com --no-decline-cookies
webskrap fetch https://example.com --decline-cookies-timeout-ms 4000
```

The timeout is how long to wait for a late-injected notice, and the per-fetch
cost on pages that have none. Use `0` for a single immediate check.

## Headless Patchright controls

`webskrap fetch` always runs headless Patchright. Use real Chrome and a stable
profile directory when browser continuity matters:

```bash
webskrap fetch https://example.com \
  --channel chrome \
  --user-data-dir .webskrap/headless-profile \
  --mask-headless-user-agent
```

For fingerprint-statistics or WebRTC leak-test pages, apply profile
locale/timezone/media metadata and block non-proxied WebRTC UDP candidates
without viewport, user-agent, or JavaScript patches:

```bash
webskrap fetch https://amiunique.org/fr/fingerprint \
  --channel chrome \
  --mask-headless-user-agent \
  --patchright-context-profile \
  --reduce-fingerprint-surface \
  --webrtc-ip-handling-policy disable_non_proxied_udp
```

Pass additional browser flags with repeated `--launch-arg=...` options. Use the
equals form when the browser flag itself starts with `--`.

```bash
webskrap fetch https://example.com \
  --launch-arg=--no-first-run \
  --launch-arg=--no-default-browser-check
```
