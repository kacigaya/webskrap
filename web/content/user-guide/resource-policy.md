---
title: Resource Routing for Faster Web Scraping
description: Use WebSkrap resource policies to block heavy assets, trackers, images, and fonts for faster browser scraping.
---

# Resource policy

A resource policy sets which request types load.

```python
from webskrap import ResourcePolicy, SessionConfig

config = SessionConfig(resource_policy=ResourcePolicy.LITE)
```

## Policies

| Policy | Behavior |
| --- | --- |
| `ResourcePolicy.AUTO` | Default. Uses lite for text without screenshots; all for other tasks. |
| `ResourcePolicy.ALL` | Allows all resources. |
| `ResourcePolicy.LITE` | Blocks images, fonts, and media. |
| `ResourcePolicy.DOCUMENTS` | Blocks images, fonts, media, and stylesheets. |

An explicit policy wins over `AUTO`. Use `ALL` for full page fidelity, and
`LITE` for a faster load that keeps stylesheets and scripts.

## Examples

`LITE` suits most extraction jobs that still need styled or script-rendered
HTML:

```python
from webskrap import ResourcePolicy, SessionConfig, WebSkrapClient

config = SessionConfig(resource_policy=ResourcePolicy.LITE)

async with WebSkrapClient() as client:
    result = await client.fetch("https://example.com", config=config)
```

Use `DOCUMENTS` only when an unstyled page still holds the content you need:

```python
config = SessionConfig(resource_policy=ResourcePolicy.DOCUMENTS)
```

Resource policies use the CDP `Fetch` domain. `AUTO` picks a policy for each
page and its cross-site iframes, but blocks nothing in shared or service
workers. Explicit policies also apply to shared and service workers. No preset
blocks scripts or XHR and fetch requests.

## CLI

```bash
webskrap fetch https://example.com --resource-policy lite
webskrap fetch https://example.com --resource-policy documents --output page.html
```

If a page renders wrong, switch back to `all` first. Many pages need images,
fonts, media or stylesheets for layout-dependent interactions, even when you
only extract text.
