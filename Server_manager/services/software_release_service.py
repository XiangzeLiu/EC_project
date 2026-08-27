"""Software release storage and path-safety helpers for the SM software center."""

from __future__ import annotations

import hashlib
import logging
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


class SoftwareArtifactConflict(SoftwareReleaseError):
    """The selected release already has the requested artifact type."""

    def __init__(self, release: dict, artifact: dict):
        super().__init__("相同版本和软件包类型已存在")
        self.release = release
        self.artifact = artifact


log = logging.getLogger("server_manager")
_ARTIFACT_EXTENSIONS = {
    "installer": frozenset({".exe", ".msi"}),
    "archive": frozenset({".zip"}),
}


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


def _validate_artifact_file(artifact_type: str, file_name: str) -> None:
    allowed = _ARTIFACT_EXTENSIONS.get(artifact_type)
    if not allowed:
        raise SoftwareReleaseError("软件包类型不正确")
    if Path(file_name).suffix.lower() not in allowed:
        expected = "、".join(sorted(allowed))
        label = "安装器" if artifact_type == "installer" else "压缩包"
        raise SoftwareReleaseError(f"{label}仅支持 {expected} 文件")


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


def _remove_empty_parents(path: Path) -> None:
    root = _storage_root()
    current = path.resolve()
    while current != root and _inside_storage_root(current):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _delete_storage_keys(storage_keys: list[str]) -> None:
    root = _storage_root()
    for storage_key in dict.fromkeys(str(item or "") for item in storage_keys):
        if not storage_key:
            continue
        path = (root / storage_key).resolve()
        if not _inside_storage_root(path):
            log.warning("ignored software cleanup path outside storage root: %s", storage_key)
            continue
        try:
            path.unlink(missing_ok=True)
            _remove_empty_parents(path.parent)
        except OSError as exc:
            log.warning("software artifact cleanup failed: %s: %s", storage_key, exc)


async def save_uploaded_file(
    upload,
    *,
    product_type: str,
    version: str,
    artifact_type: str,
    platform: str,
    created_by: str,
    replace: bool = False,
    expected_artifact_id: str = "",
) -> dict:
    product_type = str(product_type or "").strip().lower()
    if product_type not in {"client", "ts"}:
        raise SoftwareReleaseError("软件类型不正确")
    version = _safe_component(version, "版本号")
    platform = _safe_component(platform or "windows-x64", "平台")
    artifact_type = _safe_component(artifact_type or "installer", "软件包类型", 32)
    file_name = _safe_file_name(getattr(upload, "filename", ""))
    _validate_artifact_file(artifact_type, file_name)
    if not created_by:
        raise SoftwareReleaseError("缺少上传人信息")

    existing = database.find_software_release(
        product_type,
        version,
        platform,
        include_deleted=True,
    )
    current_artifact = None
    if existing and existing.get("status") != "deleted":
        current_artifact = next(
            (
                item
                for item in existing.get("artifacts") or []
                if item.get("artifact_type") == artifact_type
            ),
            None,
        )
    if current_artifact and (
        not replace or str(expected_artifact_id or "") != str(current_artifact.get("artifact_id") or "")
    ):
        raise SoftwareArtifactConflict(existing, current_artifact)

    root = _storage_root()
    temp_dir = root / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{secrets.token_urlsafe(16)}.part"
    final_path: Path | None = None
    final_dir: Path | None = None
    committed = False
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

        release_id = str(existing.get("release_id") if existing else _new_id("rel"))
        artifact_id = _new_id("art")
        final_dir = root / product_type / release_id / artifact_type / artifact_id
        final_dir.mkdir(parents=True, exist_ok=True)
        final_path = final_dir / file_name
        if not _inside_storage_root(final_path):
            raise SoftwareReleaseError("软件存储路径不合法")
        if final_path.exists():
            raise SoftwareReleaseError("软件存储文件已存在")
        temp_path.replace(final_path)
        storage_key = "/".join((product_type, release_id, artifact_type, artifact_id, file_name))
        now = datetime.now(timezone.utc).isoformat()
        result = database.save_software_artifact_record(
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
            replace=replace,
            expected_artifact_id=str(expected_artifact_id or ""),
        )
        if not result.get("ok"):
            if result.get("code") == "conflict":
                conflict_release = result.get("release") or {}
                conflict_artifact = next(
                    (
                        item
                        for item in conflict_release.get("artifacts") or []
                        if item.get("artifact_type") == artifact_type
                    ),
                    {},
                )
                raise SoftwareArtifactConflict(conflict_release, conflict_artifact)
            raise SoftwareReleaseError("软件版本记录保存失败")
        committed = True
        _delete_storage_keys(result.get("obsolete_storage_keys") or [])
        return result
    except Exception:
        temp_path.unlink(missing_ok=True)
        if final_path is not None and not committed:
            final_path.unlink(missing_ok=True)
        if final_dir is not None and not committed:
            _remove_empty_parents(final_dir)
        raise


def resolve_artifact(release_id: str, artifact_id: str = "", include_deleted: bool = False) -> tuple[dict, dict, Path]:
    release = database.get_software_release(release_id, include_deleted=include_deleted)
    if not release:
        raise FileNotFoundError("软件版本不存在")
    artifacts = release.get("artifacts") or []
    if artifact_id:
        artifact = next((item for item in artifacts if item.get("artifact_id") == artifact_id), None)
    else:
        artifact = artifacts[0] if artifacts else None
    if not artifact:
        raise FileNotFoundError("软件文件不存在")
    root = _storage_root()
    path = (root / str(artifact.get("storage_key") or "")).resolve()
    if not _inside_storage_root(path) or not path.is_file():
        raise FileNotFoundError("软件文件不存在")
    return release, artifact, path


def delete_release_files(release: dict) -> None:
    history = database.list_software_artifact_history(str(release.get("release_id") or ""))
    storage_keys = [str(item.get("storage_key") or "") for item in release.get("artifacts") or []]
    storage_keys.extend(str(item.get("storage_key") or "") for item in history)
    _delete_storage_keys(storage_keys)


def delete_artifact_files(result: dict) -> None:
    artifact = result.get("artifact") or {}
    storage_keys = [str(artifact.get("storage_key") or "")]
    storage_keys.extend(str(item or "") for item in result.get("history_storage_keys") or [])
    _delete_storage_keys(storage_keys)
