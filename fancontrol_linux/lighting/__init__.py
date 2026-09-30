"""Lighting: plugin host, catalog and effect engine.

Plugins are small Python programs that talk to LED hardware. The daemon starts each plugin in its own process
without root privileges and talks to it over a line-based JSON protocol (see docs/plugin-guide.md).
"""

PLUGIN_API = 1
