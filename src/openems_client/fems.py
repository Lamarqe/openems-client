"""Knowledge about FEMS, the OpenEMS distribution of FENECON."""

from dataclasses import dataclass
import html
import re
from typing import Any

from yarl import URL

from .const import CONN_TYPE_LOCAL_FEMS, CONN_TYPE_WEB_FENECON

FENECON = "FENECON"
FEMS_CHANGELOG_URL = "https://fenecon.de/fems-changelog"
FEMS_PORTAL_CHANGELOG_PAGE = "https://portal.fenecon.de/_/changelog"
OPENEMS_RELEASES_URL = "https://github.com/OpenEMS/openems/releases"

_FEMS_CONNECTION_TYPES = (CONN_TYPE_LOCAL_FEMS, CONN_TYPE_WEB_FENECON)
_FEMS_HOSTNAME = re.compile(r"fems\d+")
_BACKEND_FACTORY = "Controller.Api.Backend"
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_LINK = re.compile(
    r"<a\s[^>]*?href\s*=\s*([\"'])(?P<url>.*?)\1[^>]*>(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")
# languages of the changelog, all releases are available in German
_FALLBACK_LANGUAGES = ("en", "de")


def is_fems(
    connection_type: str, hostname: str, component_config: dict[str, dict[str, Any]]
) -> bool:
    """Return True if the edge is a FEMS.

    Besides the FEMS connection types, a FEMS reports to the FENECON backend and is
    named fems<number> by FENECON.
    """
    if connection_type in _FEMS_CONNECTION_TYPES:
        return True
    if _FEMS_HOSTNAME.fullmatch(hostname):
        return True
    for component in component_config.values():
        if component.get("factoryId") != _BACKEND_FACTORY:
            continue
        host = URL(str(component.get("properties", {}).get("uri", ""))).host or ""
        if host == "fenecon.de" or host.endswith(".fenecon.de"):
            return True
    return False


def release_url(connection_type: str, host: str | None, fems: bool) -> str:
    """Return the page with the changes of the software releases."""
    if connection_type == CONN_TYPE_WEB_FENECON:
        return FEMS_PORTAL_CHANGELOG_PAGE
    if fems and host:
        return f"http://{host}/_/changelog"
    return OPENEMS_RELEASES_URL


def parse_version(version: str) -> tuple[int, int, int] | None:
    """Return the numeric parts of an OpenEMS version, eg 2026.11.1."""
    if match := _VERSION.search(version):
        return int(match[1]), int(match[2]), int(match[3])
    return None


@dataclass(frozen=True, kw_only=True)
class ChangelogEntry:
    """Change of a release."""

    # may contain links: <a href="...">...</a>
    text: str
    # None: shown to everyone, eg "INSTALLER" or "ADMIN" otherwise
    min_role: str | None = None


@dataclass(frozen=True, kw_only=True)
class ChangelogRelease:
    """Changes of a release in all available languages."""

    version: str
    entries: dict[str, list[ChangelogEntry]]


def parse_changelog(data: Any) -> list[ChangelogRelease]:
    """Parse the FEMS changelog, skipping entries which are no system releases.

    Eg the changes of the FENECON App are listed as "FENECON App (16.11.2026)".
    """
    releases: list[ChangelogRelease] = []
    for release in data if isinstance(data, list) else []:
        version = str(release.get("version", ""))
        if not _VERSION.fullmatch(version):
            continue
        entries: dict[str, list[ChangelogEntry]] = {}
        for language, changes in (release.get("entries") or {}).items():
            entries[language] = [
                ChangelogEntry(text=change)
                if isinstance(change, str)
                else ChangelogEntry(
                    text=str(change.get("text", "")),
                    min_role=change.get("roleIsAtLeast"),
                )
                for change in changes
                if isinstance(change, str | dict)
            ]
        releases.append(ChangelogRelease(version=version, entries=entries))
    return releases


def releases_between(
    releases: list[ChangelogRelease], installed: str, latest: str
) -> list[ChangelogRelease]:
    """Return the releases after the installed version up to the latest version."""
    installed_version = parse_version(installed)
    latest_version = parse_version(latest)
    if installed_version is None or latest_version is None:
        return []
    return [
        release
        for release in releases
        if (version := parse_version(release.version)) is not None
        and installed_version < version <= latest_version
    ]


def _to_markdown(text: str) -> str:
    """Convert the links of a changelog entry to Markdown, drop other markup."""
    text = _LINK.sub(lambda m: f"[{m['title'].strip()}]({m['url']})", text)
    return html.unescape(_TAG.sub("", text)).strip()


def release_notes(releases: list[ChangelogRelease], language: str) -> str | None:
    """Return the changes visible to owners as Markdown, newest release first.

    Each release is given in the requested language, otherwise English or German.
    """
    primary = language.split("-", maxsplit=1)[0].lower()
    languages = list(dict.fromkeys((primary, *_FALLBACK_LANGUAGES)))
    sections: list[str] = []
    for release in releases:
        entries = next(
            (release.entries[lang] for lang in languages if release.entries.get(lang)),
            [],
        )
        lines = [
            f"- {_to_markdown(entry.text)}"
            for entry in entries
            if entry.min_role is None and entry.text
        ]
        if lines:
            sections.append(f"## {release.version}\n\n" + "\n".join(lines))
    return "\n\n".join(sections) or None
