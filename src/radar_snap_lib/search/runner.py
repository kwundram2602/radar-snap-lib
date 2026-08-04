"""Execute ASF archive searches and downloads.

The counterpart to ``snap_ops.runner``: a config goes in, it is validated
offline first, and only then does anything touch the network.  Searching needs
no credentials; downloading does, and only ``download`` builds a session.
"""

from __future__ import annotations

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

__all__ = ["download", "earthdata_session", "search"]

ConfigSource = str | Path | DictConfig | dict[str, Any]


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


def search(config: ConfigSource, *, write_output: bool = True) -> Any:
    """Validate a search config, then run it against the ASF archive.

    Args:
        config: Path to a YAML config, a mapping, or a ``DictConfig``.
        write_output: Write the result table to the config's ``output`` path.

    Returns:
        The ``ASFSearchResults`` for the query.

    Raises:
        SearchConfigError: If the config does not describe a valid search.
    """
    loaded = SearchConfig.load(config)
    results = asf.search(**loaded.search_options())

    if write_output and loaded.output is not None:
        loaded.write_results(results, loaded.output)
    return results


def download(config: ConfigSource) -> list[Path]:
    """Search, then download every hit into the config's ``dest`` directory.

    Args:
        config: Path to a YAML config, a mapping, or a ``DictConfig``.

    Returns:
        The paths of the downloaded files.

    Raises:
        SearchConfigError: If the config is invalid or sets no ``dest``.
        RuntimeError: If no Earthdata credentials are configured.
    """
    loaded = SearchConfig.load(config)
    dest = loaded.dest
    if dest is None:
        raise SearchConfigError(
            ["'dest' is required to download; set it to a target directory"],
            loaded.source,
        )

    results = search(config)
    dest.mkdir(parents=True, exist_ok=True)
    results.download(str(dest), session=earthdata_session(), processes=loaded.processes)

    return [dest / _file_name(product) for product in results]


def _file_name(product: Any) -> str:
    """The archive file name of a hit, whether it is an ASFProduct or a mapping."""
    properties = getattr(product, "properties", product)
    return str(properties["fileName"])
