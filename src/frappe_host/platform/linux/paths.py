"""System locations that frappe-host reads or (with consent) changes.

FRAPPE_HOST_SYSROOT prefixes every path. It exists so the test-suite can
exercise production/publish workflows against a throwaway directory tree;
it is not a supported user setting.
"""

from __future__ import annotations

import os
from pathlib import Path


def sysroot() -> Path:
    return Path(os.environ.get("FRAPPE_HOST_SYSROOT", "/"))


def _p(rel: str) -> Path:
    return sysroot() / rel


def nginx_conf_d() -> Path:
    return _p("etc/nginx/conf.d")


def supervisor_conf_d() -> Path:
    return _p("etc/supervisor/conf.d")


def hosts_file() -> Path:
    return _p("etc/hosts")


def letsencrypt_live() -> Path:
    return _p("etc/letsencrypt/live")


def ufw_conf() -> Path:
    return _p("etc/ufw/ufw.conf")
