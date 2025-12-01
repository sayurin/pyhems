"""Tests for MRA fetcher."""

import json
from pathlib import Path

import pytest

from pyhems.mra_fetcher import MRAFetcher, MRAFetchError


class TestMRAFetcher:
    """Tests for MRAFetcher class."""

    def test_init_defaults(self) -> None:
        """Test default initialization."""
        fetcher = MRAFetcher()
        assert fetcher._base_url == "https://sayurin.github.io/pyhems/mra"
        assert fetcher._cache_dir == Path.home() / ".cache" / "pyhems" / "mra"

    def test_init_custom(self, tmp_path: Path) -> None:
        """Test custom initialization."""
        fetcher = MRAFetcher(
            base_url="https://example.com/mra",
            cache_dir=tmp_path / "mra",
        )
        assert fetcher._base_url == "https://example.com/mra"
        assert fetcher._cache_dir == tmp_path / "mra"

    def test_is_cached_false(self, tmp_path: Path) -> None:
        """Test is_cached returns False when not cached."""
        fetcher = MRAFetcher(cache_dir=tmp_path / "mra")
        assert not fetcher.is_cached

    def test_is_cached_true(self, tmp_path: Path) -> None:
        """Test is_cached returns True when cached."""
        cache_dir = tmp_path / "mra"
        cache_dir.mkdir(parents=True)
        (cache_dir / "metaData.json").write_text('{"metaData": {"dataVersion": "1.0"}}')

        fetcher = MRAFetcher(cache_dir=cache_dir)
        assert fetcher.is_cached

    def test_get_local_version(self, tmp_path: Path) -> None:
        """Test getting local version."""
        cache_dir = tmp_path / "mra"
        cache_dir.mkdir(parents=True)
        (cache_dir / "metaData.json").write_text(
            '{"metaData": {"dataVersion": "1.3.1"}}'
        )

        fetcher = MRAFetcher(cache_dir=cache_dir)
        assert fetcher.get_local_version() == "1.3.1"

    def test_get_local_version_not_cached(self, tmp_path: Path) -> None:
        """Test getting local version when not cached."""
        fetcher = MRAFetcher(cache_dir=tmp_path / "mra")
        assert fetcher.get_local_version() is None

    def test_load_device(self, tmp_path: Path) -> None:
        """Test loading device from cache."""
        cache_dir = tmp_path / "mra"
        devices_dir = cache_dir / "devices"
        devices_dir.mkdir(parents=True)

        device_data = {"className": {"en": "Test Device"}, "elProperties": []}
        (devices_dir / "0x0130.json").write_text(json.dumps(device_data))

        fetcher = MRAFetcher(cache_dir=cache_dir)
        result = fetcher.load_device("0x0130")
        assert result["className"]["en"] == "Test Device"

    def test_load_device_by_int(self, tmp_path: Path) -> None:
        """Test loading device by integer code."""
        cache_dir = tmp_path / "mra"
        devices_dir = cache_dir / "devices"
        devices_dir.mkdir(parents=True)

        device_data = {"className": {"en": "Test Device"}}
        (devices_dir / "0x0130.json").write_text(json.dumps(device_data))

        fetcher = MRAFetcher(cache_dir=cache_dir)
        result = fetcher.load_device(0x0130)
        assert result["className"]["en"] == "Test Device"

    def test_load_device_not_found(self, tmp_path: Path) -> None:
        """Test loading non-existent device raises error."""
        cache_dir = tmp_path / "mra"
        cache_dir.mkdir(parents=True)

        fetcher = MRAFetcher(cache_dir=cache_dir)
        with pytest.raises(MRAFetchError, match="Device 0x9999 not found"):
            fetcher.load_device("0x9999")

    def test_clear_cache(self, tmp_path: Path) -> None:
        """Test clearing cache."""
        cache_dir = tmp_path / "mra"
        cache_dir.mkdir(parents=True)
        (cache_dir / "metaData.json").write_text("{}")

        fetcher = MRAFetcher(cache_dir=cache_dir)
        assert fetcher.is_cached

        fetcher.clear_cache()
        assert not fetcher.is_cached
        assert not cache_dir.exists()
