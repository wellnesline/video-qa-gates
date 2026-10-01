"""synthetic stub: minimal channel resolver (the real module is not included).

Provides the helpers the QA scripts import: current_channel / videos_root /
channel_config / add_channel_arg. Layout: <root>/channels/<channel>/{channel.yaml,videos/}.
"""
import os
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = os.environ.get("DEFAULT_CHANNEL", "default")


def add_channel_arg(ap):
    ap.add_argument("--channel", default=None, help="channel name (channels/<name>/)")


def current_channel(arg=None) -> str:
    return arg or os.environ.get("CHANNEL") or DEFAULT


def channel_dir(channel=None) -> Path:
    return ROOT / "channels" / current_channel(channel)


def videos_root(channel=None) -> Path:
    return channel_dir(channel) / "videos"


def channel_config(channel=None) -> dict:
    p = channel_dir(channel) / "channel.yaml"
    if not p.exists() or yaml is None:
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}