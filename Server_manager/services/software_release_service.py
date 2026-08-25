"""Software release storage and path-safety helpers for the SM software center."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import database
from config import (
    SM_SOFTWARE_ALLOWED_EXTENSIONS,
    SM_SOFTWARE_MAX_UPLOAD_BYTES,
    SM_SOFTWARE_STORAGE_DIR,
)


class SoftwareReleaseError(ValueError):
    """A user-facing software release validation error."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def _safe_component(value: str, field_name: str, max_length: int = 80) -> str:
    value = str(value or "").strip()
    if not value or len(value) > max_length or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise SoftwareReleaseError(f"{field_name} 格式不正确")
    return value


def _safe_file_name(file_name: str) -> str:
    raw = Path(str(file_name or "")).name
    if not raw or raw in {".", ".."}:
        raise SoftwareReleaseError("软件文件名不能为空")
    suffix = Path(raw).suffix.lower()
    if suffix not in SM_SOFTWARE_ALLOWED_EXTENSIONS:
        raise SoftwareReleaseError("不支持的软件下载文件类型")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw).stem).strip("._") or "package"
    return f"{stem[:100]}{suffix}"


def _storage_root() -> Path:
    root = Path(SM_SOFTWARE_STORAGE_DIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _inside_storage_root(path: Path) -> bool:
    root = _storage_root()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


async def save_uploaded_file(
    upload,
    *,
    product_type: str,
    version: str,
    artifact_type: str,
    platform: str,
    created_by: str,
) -> dict:
    product_type = str(product_type or "").strip().lower()
    if product_type not in {"client", "ts"}:
        raise SoftwareReleaseError("软件类型不正确")
    version = _safe_component(version, "版本号")
    platform = _safe_component(platform or "windows-x64", "平台")
    artifact_type = _safe_component(artifact_type or "installer", "软件包类型", 32)
    file_name = _safe_file_name(getattr(upload, "filename", ""))
    if not created_by:
        raise SoftwareReleaseError("缺少上传人信息")

    root = _storage_root()
    temp_dir = root / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{secrets.token_urlsafe(16)}.part"
    final_path: Path | None = None
    final_dir: Path | None = None
    size = 0
    digest = hashlib.sha256()
    try:
        with temp_path.open("wb") as target:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > SM_SOFTWARE_MAX_UPLOAD_BYTES:
                    raise SoftwareReleaseError("软件包超过上传大小限制")
                digest.update(chunk)
                target.write(chunk)

        existing = database.find_software_release(product_type, version, platform)
        release_id = str(existing.get("release_id") if existing else _new_id("rel"))
        if existing and any(item.get("artifact_type") == artifact_type for item in existing.get("artifacts") or []):
            raise SoftwareReleaseError("相同软件版本和软件包类型已存在")
        artifact_id = _new_id("art")
        final_dir = root / product_type / release_id
        final_dir.mkdir(parents=True, exist_ok=True)
        final_path = final_dir / file_name
        if not _inside_storage_root(final_path):
            raise SoftwareReleaseError("软件存储路径不合法")
        if final_path.exists():
            raise SoftwareReleaseError("软件存储文件已存在")
        temp_path.replace(final_path)
        storage_key = "/".join((product_type, release_id, file_name))
        now = datetime.now(timezone.utc).isoformat()
        record = database.create_software_release_record(
            {
                "release_id": release_id,
                "product_type": product_type,
                "version": version,
                "platform": platform,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            },
            {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "file_name": file_name,
                "storage_key": storage_key,
                "file_size": size,
                "sha256": digest.hexdigest(),
                "created_at": now,
            },
        )
        if not record:
            final_path.unlink(missing_ok=True)
            final_dir.rmdir()
            raise SoftwareReleaseError("相同产品、版本和平台的软件已存在")
        return record
    except Exception:
        temp_path.unlink(missing_ok=True)
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        if final_dir is not None and final_dir.exists():
            try:
                final_dir.rmdir()
            except OSError:
                pass
        raise


def resolve_artifact(release_id: str, artifact_id: str = "", include_deleted: bool = False) -> tuple[dict, dict, Path]:
    release = database.get_software_release(release_id, include_deleted=include_deleted)
    if not release:
        raise FileNotFoundError("软件版本不存在")
    artifacts = release.get("artifacts") or []
    artifact = next((item for item in artifacts if item.get("artifact_id") == artifact_id), None) if artifact_id else None
    if artifact is None and artifacts:
        artifact = artifacts[0]
    if not artifact:
        raise FileNotFoundError("软件文件不存在")
    root = _storage_root()
    path = (root / str(artifact.get("storage_key") or "")).resolve()
    if not _inside_storage_root(path) or not path.is_file():
        raise FileNotFoundError("软件文件不存在")
    return release, artifact, path


def delete_release_files(release: dict) -> None:
    root = _storage_root()
    for artifact in release.get("artifacts") or []:
        path = (root / str(artifact.get("storage_key") or "")).resolve()
        if not _inside_storage_root(path):
            continue
        path.unlink(missing_ok=True)
        parent = path.parent
        if parent != root and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                pass
