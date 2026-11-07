"""The display theme (« Affichage »): Automatique, Clair or Sombre, per device.

The single source of the theme vocabulary. The settings route validates
against it, ``routes/_helpers.theme_context`` renders it into
``<html data-theme>`` and the color-scheme meta, and
``tests/test_theme_palette.py`` derives from it the theme keys that
``static/src/app.input.css`` writes into its dark selectors.

THE KEYS ARE STORED VALUES — in every device's cookie and in those CSS
selectors. Change a label, never a key: a renamed key silently resets every
device to the default and turns dark mode off for that choice.

The preference lives in its own cookie, set only by the CSRF-protected POST
on the « Affichage » tab:

* per device, not in Firestore: one lawyer, a phone and a desktop that may
  want different themes, and no settings read on every page render;
* not in the Flask session: logout and the session expiry clear ``session``
  (and only it — keep it that way), and the theme must reach the login page;
* unsigned: the value is not secret and is checked on every read, so a
  forged cookie can only select another valid theme, or the default;
* always ``Secure`` (production is HTTPS end to end; browsers accept a
  Secure cookie on localhost in development), and host-only by setting no
  ``Domain``, which also keeps it off the portal's host, whose pages stay
  light. No ``__Host-`` prefix: it would only stop a sibling subdomain from
  overwriting the cookie, whose worst outcome is another valid theme.

Pure: no Flask import (the request-bound reader is
``routes/_helpers.theme_context``).
"""

from typing import Optional

VALID_THEMES: tuple[str, ...] = ("auto", "clair", "sombre")

LABELS: dict[str, str] = {
    "auto": "Automatique",
    "clair": "Clair",
    "sombre": "Sombre",
}

DESCRIPTIONS: dict[str, str] = {
    "auto": "Suit le réglage clair ou sombre de cet appareil.",
    "clair": "Toujours clair.",
    "sombre": "Toujours sombre.",
}

DEFAULT_THEME = "auto"

# The <meta name="color-scheme"> each theme renders. It sets the browser's
# canvas and native controls before the stylesheet applies. « only light »
# also opts Clair out of Chrome's forced dark (and thus the Android TWA's).
COLOR_SCHEMES: dict[str, str] = {
    "auto": "light dark",
    "clair": "only light",
    "sombre": "dark",
}

THEME_COOKIE = "athena_theme"

# 400 days: the longest lifetime browsers accept for a cookie.
THEME_COOKIE_MAX_AGE = 400 * 24 * 60 * 60


def parse_theme(raw: object) -> Optional[str]:
    """The theme key if ``raw`` is EXACTLY one, else None.

    No stripping, case folding or sanitizing first: a value that would need
    cleaning is refused, never repaired.
    """
    if isinstance(raw, str) and raw in VALID_THEMES:
        return raw
    return None


def theme_from(raw: object) -> str:
    """The theme to render for a stored value; anything unknown falls back."""
    return parse_theme(raw) or DEFAULT_THEME
