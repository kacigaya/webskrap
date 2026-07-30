---
title: Browser Profiles for Playwright Scraping
description: Configure coherent desktop and mobile browser profiles for Python web scraping with realistic locale, viewport, and headers.
---

# Profiles

Profiles describe browser-visible settings such as viewport, locale, timezone, language headers, and device characteristics.

Patchright sessions use the browser's native context by default. Set
`patchright_context_profile=True` when you intentionally want WebSkrap to apply
the selected profile's locale, timezone, viewport, media, and user agent.

## Built-in profiles

```python
from webskrap import get_profile, list_profiles

for profile in list_profiles():
    print(profile.name)

profile = get_profile("desktop-chrome")
```

Built-in profile names:

- `desktop-chrome`
- `desktop-edge`
- `mobile-chrome`

`get_profile()` returns a copy, so you can adjust it without mutating the
bundled profile:

```python
profile = get_profile("desktop-chrome")
profile.locale = "fr-FR"
profile.navigator_languages = ["fr-FR", "fr", "en-US", "en"]
```

## Custom profile

```python
from webskrap import BrowserProfile, Viewport

profile = BrowserProfile(
    name="fr-desktop",
    viewport=Viewport(width=1440, height=900),
    screen=Viewport(width=1440, height=900),
    locale="fr-FR",
    timezone_id="Europe/Paris",
    navigator_languages=["fr-FR", "fr", "en-US", "en"],
)
```

Pass it to a session:

```python
session = await client.session("fr", profile=profile)
```

Or use it for a one-shot fetch:

```python
result = await client.fetch("https://example.com", profile=profile)
```

## Language headers

`BrowserProfile` keeps `locale`, `navigator_languages`, and the generated
`Accept-Language` header coherent. The first language gets full weight and later
languages get descending quality weights:

```python
profile = BrowserProfile(
    name="fr",
    locale="fr-FR",
    navigator_languages=["fr-FR", "fr", "en-US", "en"],
)
print(profile.accept_language())
# fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7
```

Extra headers are merged after WebSkrap's defaults:

```python
profile = BrowserProfile(
    name="api-docs",
    extra_http_headers={"DNT": "1"},
)
```

## Mobile profile

Use the built-in mobile profile when viewport, touch, and mobile user-agent
settings should move together:

```python
result = await client.fetch("https://example.com", profile="mobile-chrome")
```

For custom mobile profiles, keep `viewport`, `screen`, `device_scale_factor`,
`is_mobile`, `has_touch`, and `user_agent` coherent.
