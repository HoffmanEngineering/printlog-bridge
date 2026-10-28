"""An older way of logging prints still set up: the OctoPrint-Webhooks plugin posting to 3D Print
Log.

Before the bridge, 3D Print Log's guide (`/docs/octoprint-webhook`) had OctoPrint users install
the Webhooks plugin and point it at `https://api.3dprintlog.com/api/Octoprint`. With the bridge
watching the same printer, both log each print. The bridge can't change OctoPrint's settings (it
only reads), so it reads them (`GET /api/settings`) on every connect, and while a hook posts to
3D Print Log it says so, in the log, `printlog-bridge status` and `doctor`.

The plugin keeps its hooks in `plugins.webhooks`: a list of `hooks` in version 3, a single `url`
before. Any URL there whose host is 3dprintlog.com or under it counts. Only that it is there is
ever shown, never the URL or the hook's settings: they hold the user's API key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

TOPIC = "webhook"
"""The `PrinterReport.warn` topic."""
HOST = "3dprintlog.com"
PLUGIN = "webhooks"
MESSAGE = (
    "OctoPrint's Webhooks plugin also logs prints to 3D Print Log, so each print can be logged "
    "twice. The bridge logs them itself: in OctoPrint, open Settings > Webhooks and turn off "
    "(or delete) the hook that posts to 3dprintlog.com, then save."
)


def posts_to_printlog(settings: Any) -> bool:
    """Whether `/api/settings`' answer has the Webhooks plugin posting to 3D Print Log."""
    if not isinstance(settings, Mapping):
        return False
    plugins = settings.get("plugins")
    plugin = plugins.get(PLUGIN) if isinstance(plugins, Mapping) else None
    if not isinstance(plugin, Mapping):
        return False
    hooks = plugin.get("hooks")
    if isinstance(hooks, list):
        return any(
            isinstance(hook, Mapping) and hook.get("enabled", True) is not False and _url(hook)
            for hook in hooks
        )
    return _url(plugin)


def _url(hook: Mapping[str, Any]) -> bool:
    url = hook.get("url")
    if not isinstance(url, str):
        return False
    try:
        host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == HOST or host.endswith("." + HOST)
