import json
from pathlib import Path

CONFIG_PATH = Path("camera_config.json")

DEFAULT_CONFIG = {
    "active_camera": None
}

def load_camera_config():
    if not CONFIG_PATH.exists():
        save_camera_config(DEFAULT_CONFIG)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def save_camera_config(cfg: dict):
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
