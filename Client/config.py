"""
Configuration Management
本地凭证存储与配置加载
"""

import json
import os
from pathlib import Path


def _get_config_path() -> str:
    """返回配置文件路径"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tt_config.json")


def load_config() -> dict:
    """加载本地配置"""
    try:
        with open(_get_config_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data: dict) -> None:
    """保存配置到本地"""
    try:
        with open(_get_config_path(), "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def load_credentials() -> tuple[str, str]:
    """加载保存的用户名和密码"""
    cfg = load_config()
    return cfg.get("username", ""), cfg.get("password", "")


def save_credentials(username: str, password: str) -> None:
    """保存用户名和密码"""
    save_config({"username": username, "password": password})
