"""MRA (Machine Readable Appendix) fetcher from GitHub Pages."""

import json
import logging
import shutil
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

_LOGGER = logging.getLogger(__name__)

MRA_BASE_URL = "https://sayurin.github.io/pyhems/mra"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "pyhems" / "mra"


class MRAFetchError(Exception):
    """Error fetching MRA data."""


class MRAFetcher:
    """Fetcher for MRA data from GitHub Pages with local caching."""

    def __init__(
        self,
        base_url: str = MRA_BASE_URL,
        cache_dir: Path | None = None,
    ) -> None:
        """Initialize the MRA fetcher.

        Args:
            base_url: Base URL for MRA data.
            cache_dir: Local cache directory. Defaults to ~/.cache/pyhems/mra

        """
        self._base_url = base_url.rstrip("/")
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._metadata: dict[str, Any] | None = None

    @property
    def cache_dir(self) -> Path:
        """Return the cache directory path."""
        return self._cache_dir

    @property
    def is_cached(self) -> bool:
        """Check if MRA data is cached locally."""
        return (self._cache_dir / "metaData.json").exists()

    def _fetch_url(self, path: str) -> bytes:
        """Fetch data from URL.

        Args:
            path: Path relative to base URL.

        Returns:
            Response content as bytes.

        Raises:
            MRAFetchError: If fetch fails.

        """
        url = f"{self._base_url}/{path}"
        try:
            with urlopen(url, timeout=30) as response:
                result: bytes = response.read()
                return result
        except URLError as ex:
            raise MRAFetchError(f"Failed to fetch {url}: {ex}") from ex

    def _fetch_json(self, path: str) -> dict[str, Any]:
        """Fetch and parse JSON from URL.

        Args:
            path: Path relative to base URL.

        Returns:
            Parsed JSON data.

        Raises:
            MRAFetchError: If fetch or parse fails.

        """
        try:
            data = self._fetch_url(path)
            result: dict[str, Any] = json.loads(data)
        except json.JSONDecodeError as ex:
            raise MRAFetchError(f"Failed to parse JSON from {path}: {ex}") from ex
        return result

    def get_remote_version(self) -> str:
        """Get the MRA version from remote.

        Returns:
            Version string (dataVersion from metaData.json).

        Raises:
            MRAFetchError: If fetch fails.

        """
        metadata = self._fetch_json("metaData.json")
        version: str = metadata.get("metaData", {}).get("dataVersion", "unknown")
        return version

    def get_local_version(self) -> str | None:
        """Get the cached MRA version.

        Returns:
            Version string or None if not cached.

        """
        metadata_path = self._cache_dir / "metaData.json"
        if not metadata_path.exists():
            return None

        try:
            with metadata_path.open(encoding="utf-8") as f:
                metadata = json.load(f)
            version: str | None = metadata.get("metaData", {}).get("dataVersion")
        except (json.JSONDecodeError, OSError):
            return None
        return version

    def needs_update(self) -> bool:
        """Check if local cache needs to be updated.

        Returns:
            True if cache is missing or outdated.

        """
        local_version = self.get_local_version()
        if local_version is None:
            return True

        try:
            remote_version = self.get_remote_version()
        except MRAFetchError:
            # If we can't check remote, use cached version
            return False
        else:
            return local_version != remote_version

    def ensure_mra(self, force: bool = False) -> Path:
        """Ensure MRA data is available locally.

        Downloads MRA data if not cached or if force=True.

        Args:
            force: Force re-download even if cached.

        Returns:
            Path to local MRA cache directory.

        Raises:
            MRAFetchError: If download fails.

        """
        if not force and self.is_cached and not self.needs_update():
            return self._cache_dir

        return self.download()

    def download(self) -> Path:
        """Download all MRA data to cache directory.

        Returns:
            Path to cache directory.

        Raises:
            MRAFetchError: If download fails.

        """
        _LOGGER.info("Downloading MRA data to %s", self._cache_dir)

        # Create cache directory
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # Download metadata first
        metadata = self._fetch_json("metaData.json")
        metadata_path = self._cache_dir / "metaData.json"
        with metadata_path.open("w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        # Download definitions
        self._download_directory("definitions")

        # Download devices
        self._download_directory("devices")

        # Download nodeProfile
        self._download_directory("nodeProfile")

        # Download superClass
        self._download_directory("superClass")

        # Download MCRules
        self._download_directory("MCRules")

        _LOGGER.info("MRA download complete")
        return self._cache_dir

    def _download_directory(self, dirname: str) -> None:
        """Download all files in a directory.

        This uses a known list of files since we can't list directories on GitHub Pages.
        """
        target_dir = self._cache_dir / dirname
        target_dir.mkdir(parents=True, exist_ok=True)

        # Try to download known files based on directory
        if dirname == "definitions":
            self._download_file(f"{dirname}/definitions.json")
        elif dirname == "nodeProfile":
            self._download_file(f"{dirname}/0x0EF0.json")
        elif dirname == "superClass":
            self._download_file(f"{dirname}/0x0000.json")
        elif dirname == "devices":
            # Download all known device files
            device_codes = [
                "0x0002",
                "0x0003",
                "0x0007",
                "0x0011",
                "0x0012",
                "0x0016",
                "0x001B",
                "0x001D",
                "0x0022",
                "0x0023",
                "0x00D0",
                "0x0130",
                "0x0133",
                "0x0134",
                "0x0135",
                "0x0156",
                "0x0157",
                "0x0260",
                "0x0263",
                "0x026B",
                "0x026F",
                "0x0272",
                "0x0273",
                "0x0279",
                "0x027A",
                "0x027B",
                "0x027C",
                "0x027D",
                "0x027E",
                "0x0280",
                "0x0281",
                "0x0282",
                "0x0287",
                "0x0288",
                "0x028A",
                "0x028D",
                "0x028E",
                "0x028F",
                "0x0290",
                "0x0291",
                "0x02A1",
                "0x02A3",
                "0x02A4",
                "0x02A5",
                "0x02A6",
                "0x02A7",
                "0x03B7",
                "0x03B9",
                "0x03BB",
                "0x03CE",
                "0x03D3",
                "0x03D4",
                "0x05FD",
                "0x05FF",
                "0x0602",
            ]
            for code in device_codes:
                try:
                    self._download_file(f"{dirname}/{code}.json")
                except MRAFetchError:
                    _LOGGER.debug("Device %s not found, skipping", code)
        elif dirname == "MCRules":
            # Download all known MCRules files
            mcrule_codes = [
                "0x0000",
                "0x0130",
                "0x0133",
                "0x0134",
                "0x0135",
                "0x0156",
                "0x0157",
                "0x0263",
                "0x0279",
                "0x027A",
                "0x027B",
                "0x027C",
                "0x027D",
                "0x027E",
                "0x0280",
                "0x0281",
                "0x0287",
                "0x0288",
                "0x028A",
                "0x028D",
                "0x028E",
                "0x028F",
                "0x0290",
                "0x0291",
                "0x02A1",
                "0x02A5",
                "0x02A7",
                "0x03B9",
                "0x03CE",
                "0x03D3",
                "0x03D4",
                "0x05FF",
                "0x0602",
            ]
            for code in mcrule_codes:
                try:
                    self._download_file(f"{dirname}/{code}_mcrule.json")
                except MRAFetchError:
                    _LOGGER.debug("MCRule %s not found, skipping", code)

    def _download_file(self, path: str) -> None:
        """Download a single file to cache."""
        data = self._fetch_url(path)
        target_path = self._cache_dir / path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("wb") as f:
            f.write(data)

    def load_metadata(self) -> dict[str, Any]:
        """Load metadata from cache.

        Returns:
            Metadata dictionary.

        Raises:
            MRAFetchError: If not cached.

        """
        if self._metadata is not None:
            return self._metadata

        metadata_path = self._cache_dir / "metaData.json"
        if not metadata_path.exists():
            raise MRAFetchError("MRA not cached, call ensure_mra() first")

        with metadata_path.open(encoding="utf-8") as f:
            metadata: dict[str, Any] = json.load(f)
            self._metadata = metadata
        return self._metadata

    def load_device(self, device_code: str | int) -> dict[str, Any]:
        """Load device specification from cache.

        Args:
            device_code: Device class code (e.g., "0x0130" or 0x0130)

        Returns:
            Device specification dictionary.

        Raises:
            MRAFetchError: If not found.

        """
        if isinstance(device_code, int):
            device_code = f"0x{device_code:04X}"

        device_path = self._cache_dir / "devices" / f"{device_code}.json"
        if not device_path.exists():
            raise MRAFetchError(f"Device {device_code} not found")

        with device_path.open(encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result

    def load_definitions(self) -> dict[str, Any]:
        """Load common definitions from cache.

        Returns:
            Definitions dictionary.

        Raises:
            MRAFetchError: If not found.

        """
        definitions_path = self._cache_dir / "definitions" / "definitions.json"
        if not definitions_path.exists():
            raise MRAFetchError("Definitions not found")

        with definitions_path.open(encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result

    def load_super_class(self) -> dict[str, Any]:
        """Load super class (common properties) from cache.

        Returns:
            Super class specification dictionary.

        Raises:
            MRAFetchError: If not found.

        """
        super_class_path = self._cache_dir / "superClass" / "0x0000.json"
        if not super_class_path.exists():
            raise MRAFetchError("Super class not found")

        with super_class_path.open(encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result

    def clear_cache(self) -> None:
        """Remove all cached MRA data."""
        if self._cache_dir.exists():
            shutil.rmtree(self._cache_dir)
        self._metadata = None
