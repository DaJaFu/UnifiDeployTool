"""
config_io.py - Round-trip YAML load/save for the editable config files.

Uses ruamel.yaml in round-trip mode so editing a few values from the GUI config
editor preserves the rest of the file: comments, key order, and quoting. The
deployment code (main.py) keeps reading these files with PyYAML; only the editor
needs round-trip fidelity.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_DIR / "config"
DEVICE_PROFILES_PATH = CONFIG_DIR / "device_profiles.yaml"
VLAN_CONFIG_PATH = CONFIG_DIR / "vlan_config.yaml"


def _yaml() -> YAML:
    y = YAML()                 # round-trip mode by default
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load(path: Path):
    """Load a YAML file in round-trip mode (returns a CommentedMap)."""
    with open(path) as f:
        return _yaml().load(f)


def save(path: Path, data) -> None:
    """Write a round-trip document back, preserving comments and structure."""
    with open(path, "w") as f:
        _yaml().dump(data, f)


def load_device_profiles():
    return load(DEVICE_PROFILES_PATH)


def save_device_profiles(data) -> None:
    save(DEVICE_PROFILES_PATH, data)
