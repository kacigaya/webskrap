---
title: Resource Routing for Faster Web Scraping
description: Use WebSkrap resource policies to block heavy assets, trackers, images, and fonts for faster browser scraping.
---

# Resource policy

Resource policies control which request types are allowed.

```python
from webskrap import ResourcePolicy, SessionConfig

config = SessionConfig(resource_policy=ResourcePolicy.LITE)
```

## Policies

| Policy | Behavior |
| --- | --- |
| `ResourcePolicy.ALL` | Allows all resources. |
| `ResourcePolicy.LITE` | Blocks images, fonts, and media. |
| `ResourcePolicy.DOCUMENTS` | Blocks images, fonts, media, and stylesheets. |

Use `ALL` for maximum page fidelity. Use `LITE` when you want a faster page load while keeping stylesheets and scripts.

## Examples

Use `LITE` for most extraction jobs that still need styled or script-rendered
HTML:

```python
from webskrap import ResourcePolicy, SessionConfig, WebSkrapClient

config = SessionConfig(resource_policy=ResourcePolicy.LITE)

async with WebSkrapClient() as client:
    result = await client.fetch("https://example.com", config=config)
```

Use `DOCUMENTS` when you only need document-oriented HTML and can tolerate
unstyled pages:

```python
config = SessionConfig(resource_policy=ResourcePolicy.DOCUMENTS)
```

Resource policies block resource types through the CDP `Fetch` domain on every
page of the session. Scripts and XHR/fetch requests are not blocked by these presets.

## CLI

```bash
webskrap fetch https://example.com --resource-policy lite
webskrap fetch https://example.com --resource-policy documents --output page.html
```

If a page renders incorrectly, switch back to `all` first. Many pages require
images, fonts, media, or stylesheets for layout-dependent interactions even when
the final extraction target is text.
