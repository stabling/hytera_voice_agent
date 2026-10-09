# =============================================================================
# 【维护注释】文件：voice_agent/storage/object_store.py
# 所属层次：持久化抽象层
# 主要职责：定义存储接口、SQLite/PostgreSQL 切换、归档、对象存储、保留期和读路径灰度。
# 项目内直接依赖：无显式项目内依赖或仅运行时动态导入
# 主要公开符号：ObjectRef, sha256_file, FilesystemObjectStore, S3ObjectStore
# 兼容性：优先通过新增可选接口、适配器或配置开关扩展；旧公开函数/类和已有调用链应继续可用。
# 修改原则：业务校验留在服务端；模型只做语义选择/填参；跨层通信使用明确契约，避免隐式全局状态。
# 排障顺序：先看输入契约与日志，再看本层返回值，最后沿项目内直接依赖追踪到下游；不要跨层临时绕过校验。
# =============================================================================
"""Content-addressed object storage with integrity checks and resumable S3 uploads."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ObjectRef:
    key: str
    sha256: str
    size: int
    encryption: str


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class FilesystemObjectStore:
    """Test/edge object store; production should use S3-compatible storage."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / str(key)).resolve(strict=False)
        if path != self.root and self.root not in path.parents:
            raise ValueError("invalid object key")
        return path

    def put_bytes(self, data: bytes, *, prefix: str = "objects") -> ObjectRef:
        digest = hashlib.sha256(data).hexdigest()
        key = f"{prefix.strip('/')}/{digest[:2]}/{digest}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return ObjectRef(key=key, sha256=digest, size=len(data), encryption="filesystem")

    def put_file(
        self,
        source: str | Path,
        *,
        prefix: str = "objects",
        checkpoint_path: str | Path | None = None,
    ) -> ObjectRef:
        del checkpoint_path
        source_path = Path(source)
        digest, size = sha256_file(source_path)
        key = f"{prefix.strip('/')}/{digest[:2]}/{digest}"
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing_hash, existing_size = sha256_file(destination)
            if existing_hash != digest or existing_size != size:
                raise IOError("existing object does not match content hash")
            return ObjectRef(key=key, sha256=digest, size=size, encryption="filesystem")
        tmp = destination.with_suffix(".part")
        with source_path.open("rb") as src, tmp.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, destination)
        return ObjectRef(key=key, sha256=digest, size=size, encryption="filesystem")

    def get_bytes(self, ref: ObjectRef) -> bytes:
        data = self._path(ref.key).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != ref.sha256:
            raise IOError("object sha256 mismatch")
        return data

    def delete(self, ref: ObjectRef) -> bool:
        path = self._path(ref.key)
        if not path.exists():
            return False
        path.unlink()
        return True


class S3ObjectStore:
    """S3-compatible content-addressed store with crash-resumable multipart upload.

    Multipart state is persisted next to the local spool file.  A retry first asks
    the object store which parts were already committed and uploads only missing
    parts.  The final object is accepted only after metadata hash/size verification.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "audio",
        endpoint_url: str | None = None,
        kms_key_id: str | None = None,
        multipart_part_size: int = 8 * 1024 * 1024,
    ) -> None:
        if not str(bucket).strip():
            raise ValueError("OBJECT_STORE_BUCKET is required")
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("S3 object storage requires boto3") from exc
        self.bucket = str(bucket)
        self.prefix = str(prefix).strip("/")
        self.kms_key_id = str(kms_key_id or "").strip() or None
        self.part_size = max(5 * 1024 * 1024, int(multipart_part_size))
        self.client = boto3.client("s3", endpoint_url=endpoint_url or None)

    def _encryption_args(self) -> tuple[dict[str, str], str]:
        if self.kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self.kms_key_id,
            }, "aws:kms"
        return {"ServerSideEncryption": "AES256"}, "AES256"

    def _key(self, digest: str, prefix: str | None) -> str:
        base = (prefix or self.prefix).strip("/")
        return f"{base}/{digest[:2]}/{digest}"

    def _head_if_matching(self, *, key: str, digest: str, size: int) -> bool:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # boto3 exception hierarchy is optional at import time
            response = getattr(exc, "response", {}) or {}
            status = ((response.get("ResponseMetadata") or {}).get("HTTPStatusCode"))
            code = str((response.get("Error") or {}).get("Code") or "")
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        metadata_hash = str((head.get("Metadata") or {}).get("sha256") or "")
        remote_size = int(head.get("ContentLength") or -1)
        if metadata_hash != digest or remote_size != size:
            raise IOError("existing object metadata/hash/size mismatch")
        return True

    def put_bytes(self, data: bytes, *, prefix: str | None = None) -> ObjectRef:
        digest = hashlib.sha256(data).hexdigest()
        key = self._key(digest, prefix)
        extra, encryption = self._encryption_args()
        if not self._head_if_matching(key=key, digest=digest, size=len(data)):
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                Metadata={"sha256": digest},
                **extra,
            )
        if not self._head_if_matching(key=key, digest=digest, size=len(data)):
            raise IOError("object verification failed")
        return ObjectRef(key=key, sha256=digest, size=len(data), encryption=encryption)

    def _listed_parts(self, *, key: str, upload_id: str) -> dict[int, str]:
        parts: dict[int, str] = {}
        marker = 0
        while True:
            response = self.client.list_parts(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                PartNumberMarker=marker,
            )
            for item in response.get("Parts") or []:
                parts[int(item["PartNumber"])] = str(item["ETag"])
            if not response.get("IsTruncated"):
                break
            marker = int(response.get("NextPartNumberMarker") or 0)
        return parts

    def put_file(
        self,
        source: str | Path,
        *,
        prefix: str | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> ObjectRef:
        path = Path(source)
        digest, size = sha256_file(path)
        key = self._key(digest, prefix)
        extra, encryption = self._encryption_args()
        if self._head_if_matching(key=key, digest=digest, size=size):
            if checkpoint_path:
                Path(checkpoint_path).unlink(missing_ok=True)
            return ObjectRef(key=key, sha256=digest, size=size, encryption=encryption)
        if size < self.part_size:
            return self.put_bytes(path.read_bytes(), prefix=prefix)

        checkpoint = Path(checkpoint_path or (str(path) + ".multipart.json"))
        state: dict[str, Any] = {}
        if checkpoint.exists():
            try:
                state = json.loads(checkpoint.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        expected = {
            "bucket": self.bucket,
            "key": key,
            "sha256": digest,
            "size": size,
            "part_size": self.part_size,
        }
        if any(state.get(name) != value for name, value in expected.items()):
            state = {}

        upload_id = str(state.get("upload_id") or "")
        if not upload_id:
            response = self.client.create_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                Metadata={"sha256": digest},
                **extra,
            )
            upload_id = str(response["UploadId"])
            state = {**expected, "upload_id": upload_id}
            checkpoint.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

        try:
            existing = self._listed_parts(key=key, upload_id=upload_id)
        except Exception:
            # The server may have expired/aborted the multipart upload.  Restart
            # safely because the final object key is content-addressed.
            checkpoint.unlink(missing_ok=True)
            response = self.client.create_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                Metadata={"sha256": digest},
                **extra,
            )
            upload_id = str(response["UploadId"])
            state = {**expected, "upload_id": upload_id}
            checkpoint.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            existing = {}

        completed: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            part_no = 1
            while True:
                chunk = handle.read(self.part_size)
                if not chunk:
                    break
                etag = existing.get(part_no)
                if etag is None:
                    response = self.client.upload_part(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_no,
                        Body=chunk,
                    )
                    etag = str(response["ETag"])
                completed.append({"ETag": etag, "PartNumber": part_no})
                part_no += 1

        self.client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": completed},
        )
        if not self._head_if_matching(key=key, digest=digest, size=size):
            raise IOError("object verification failed after multipart completion")
        checkpoint.unlink(missing_ok=True)
        return ObjectRef(key=key, sha256=digest, size=size, encryption=encryption)
