"""plexlists: build curated TV show playlists in Plex, matched by episode title."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("plexlists")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"
