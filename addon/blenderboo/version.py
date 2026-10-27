"""Single source for the add-on's declared semantic version."""

ADDON_VERSION = (0, 10, 0)


def version_string() -> str:
    return ".".join(str(part) for part in ADDON_VERSION)
