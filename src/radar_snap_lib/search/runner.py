"""Execute ASF archive searches and downloads.

The counterpart to ``snap_ops.runner``: a config goes in, it is validated
offline first, and only then does anything touch the network.  Searching needs
no credentials; downloading does, and only ``download`` builds a session.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import asf_search as asf
from omegaconf import DictConfig

from radar_snap_lib.config import (
    EDL_TOKEN_VAR,
    EDL_USERNAME_VAR,
    earthdata_credentials,
)
from radar_snap_lib.search.SearchConfig import SearchConfig, SearchConfigError

__all__ = [
    "download",
    "earthdata_session",
    "log_duplicate_combinations",
    "product_bytes",
    "product_file_name",
    "search",
]

_LOG = logging.getLogger(__name__)

ConfigSource = str | Path | DictConfig | dict[str, Any] | SearchConfig

DUPLICATE_COMBINATION_KEYS = ("pathNumber", "flightDirection")


def earthdata_session() -> asf.ASFSession:
    """Build an authenticated ASF session from the configured credentials.

    Raises:
        RuntimeError: If no Earthdata credentials are configured.
    """
    credentials = earthdata_credentials()
    session = asf.ASFSession()

    if credentials is not None:
        if credentials.token is not None:
            return session.auth_with_token(credentials.token)
        username, password = credentials.username, credentials.password
        if username is not None and password is not None:
            return session.auth_with_creds(username, password)

    raise RuntimeError(
        f"No Earthdata credentials configured. Set {EDL_TOKEN_VAR} (or "
        f"{EDL_USERNAME_VAR} and its password) in your environment or .env "
        "file. Register at https://urs.earthdata.nasa.gov/"
    )


def search(config: ConfigSource, *, write_output: bool = True) -> asf.ASFSearchResults:
    """Validate a search config, then run it against the ASF archive.

    Args:
        config: Path to a YAML config, a mapping, a ``DictConfig``, or an
            already-loaded ``SearchConfig``.
        write_output: Write the result table to the config's ``output`` path.

    Returns:
        The ``ASFSearchResults`` for the query.

    Raises:
        SearchConfigError: If the config does not describe a valid search.
    """
    loaded = config if isinstance(config, SearchConfig) else SearchConfig.load(config)
    results = asf.search(**loaded.search_options())
    log_duplicate_combinations(results)

    if write_output and loaded.output is not None:
        loaded.write_results(results, loaded.output)
    return results


def download(config: ConfigSource) -> list[Path]:
    """Search, then download every hit into the config's ``dest`` directory.

    Args:
        config: Path to a YAML config, a mapping, a ``DictConfig``, or an
            already-loaded ``SearchConfig``.

    Returns:
        The paths of the downloaded files.

    Raises:
        SearchConfigError: If the config is invalid or sets no ``dest``. Every
            problem is reported together, not just the first one found.
        RuntimeError: If no Earthdata credentials are configured.
    """
    loaded = config if isinstance(config, SearchConfig) else SearchConfig.load(config)
    dest = loaded.dest
    errors = loaded.validate()
    if dest is None:
        errors.append("'dest' is required to download; set it to a target directory")
    if errors:
        raise SearchConfigError(errors, loaded.source)
    assert dest is not None  # guaranteed by the check above

    results = search(loaded)
    total_bytes = sum(product_bytes(product) for product in results)
    _LOG.info(
        "Downloading %d item(s), %d bytes (%.1f MB) total",
        len(results),
        total_bytes,
        total_bytes / 1_000_000,
    )
    dest.mkdir(parents=True, exist_ok=True)
    results.download(str(dest), session=earthdata_session(), processes=loaded.processes)

    return [dest / product_file_name(product) for product in results]


def log_duplicate_combinations(
    results: asf.ASFSearchResults, keys: tuple[str, ...] = DUPLICATE_COMBINATION_KEYS
) -> None:
    """Warn about hits that share the same value for every one of ``keys``.

    Repeats of e.g. ``pathNumber``/``flightDirection`` usually mean the search
    is broader than intended and could be narrowed further.
    """
    groups: dict[tuple[Any, ...], list[Any]] = {}
    for product in results:
        properties = getattr(product, "properties", product)
        combo = tuple(properties.get(key) for key in keys)
        groups.setdefault(combo, []).append(properties)

    duplicates = {combo: items for combo, items in groups.items() if len(items) > 1}
    if not duplicates:
        return

    blocks = [
        "{}\n{}".format(
            dict(zip(keys, combo, strict=True)),
            "\n".join(f" - {_product_date(properties)}" for properties in items),
        )
        for combo, items in sorted(duplicates.items(), key=lambda item: -len(item[1]))
    ]
    _LOG.warning("Search hits repeat combinations:\n%s", "\n".join(blocks))


def _product_date(properties: Any) -> str:
    start = properties.get("startTime")
    return start.split("T", 1)[0] if isinstance(start, str) else str(start)


def product_bytes(product: Any) -> int:
    """The archive file size of a hit, whether it is an ASFProduct or a mapping.

    Missing or unparsable ``bytes`` counts as 0.
    """
    properties = getattr(product, "properties", product)
    try:
        return int(properties.get("bytes") or 0)
    except (TypeError, ValueError):
        return 0


def product_file_name(product: Any, *, default: str | None = None) -> str:
    """The archive file name of a hit, whether it is an ASFProduct or a mapping.

    Prefers ``fileName``, falling back to ``sceneName``. Raises
    ``SearchConfigError`` if neither is present and no ``default`` was given.
    """
    properties = getattr(product, "properties", product)
    name = properties.get("fileName") or properties.get("sceneName")
    if name is not None:
        return str(name)
    if default is not None:
        return default
    raise SearchConfigError(
        [f"Search result has neither 'fileName' nor 'sceneName': {properties!r}"]
    )
