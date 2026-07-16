"""GCS operations for call recordings: sha256 verification, retention deletion,
and V4 signed playback URLs. google-cloud-storage is a sync SDK — every call is
wrapped in asyncio.to_thread (mirrors vera_core/audit/gcs_anchor.py). The signed
URL is minted via IAM signBlob under Workload Identity (no exported key files;
devops-todo grants roles/iam.serviceAccountTokenCreator to the control-plane SA).

Object paths carry only tenant/call UUIDs — never PHI (bright line: no PHI in a
URL or path). The audio bytes themselves are PHI: this module never logs content,
only ids/sizes/hashes.
"""

import asyncio
import hashlib
from datetime import timedelta
from typing import Any, Protocol

_GCS_SCHEME = "gs://"
_CHUNK = 1 << 20  # 1 MiB read chunks for hashing


class SigningUnavailable(Exception):
    """Signed-URL minting failed at the GCS/auth seam (user ADC instead of a
    service account, missing signBlob grant, auth outage). Raised so routers can
    map it to a clean envelope instead of catching raw google.auth errors —
    same boundary discipline as the LiveKit gateway's domain errors."""


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Split "gs://bucket/path/to/object" into (bucket, object_path)."""
    if not uri.startswith(_GCS_SCHEME):
        raise ValueError(f"not a gs:// uri: {uri!r}")
    bucket, _, object_path = uri.removeprefix(_GCS_SCHEME).partition("/")
    if not bucket or not object_path:
        raise ValueError(f"malformed gs:// uri: {uri!r}")
    return bucket, object_path


class RecordingStorage(Protocol):
    async def sha256_and_size(self, bucket: str, object_path: str) -> tuple[str, int] | None: ...
    async def delete(self, bucket: str, object_path: str) -> None: ...
    async def exists(self, bucket: str, object_path: str) -> bool: ...
    async def signed_url(self, bucket: str, object_path: str, *, ttl_seconds: int) -> str: ...


class GCSRecordingStorage:
    def _blob_sync(self, bucket: str, object_path: str) -> Any:
        from google.cloud import storage  # type: ignore[attr-defined]  # lazy prod-only dep

        return storage.Client().bucket(bucket).blob(object_path)

    async def sha256_and_size(self, bucket: str, object_path: str) -> tuple[str, int] | None:
        return await asyncio.to_thread(self._sha256_sync, bucket, object_path)

    def _sha256_sync(self, bucket: str, object_path: str) -> tuple[str, int] | None:
        from google.api_core.exceptions import NotFound

        digest = hashlib.sha256()
        size = 0
        # Stream straight into the hash; a NotFound on open means the object is not
        # visible yet (caller retries next tick) — one GCS round-trip, no exists() HEAD.
        try:
            with self._blob_sync(bucket, object_path).open("rb") as fh:
                for chunk in iter(lambda: fh.read(_CHUNK), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except NotFound:
            return None
        return digest.hexdigest(), size

    async def delete(self, bucket: str, object_path: str) -> None:
        await asyncio.to_thread(self._delete_sync, bucket, object_path)

    def _delete_sync(self, bucket: str, object_path: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self._blob_sync(bucket, object_path).delete()
        except NotFound:
            return  # already gone — sweep retries / replica races are no-ops

    async def exists(self, bucket: str, object_path: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, bucket, object_path)

    def _exists_sync(self, bucket: str, object_path: str) -> bool:
        return bool(self._blob_sync(bucket, object_path).exists())

    async def signed_url(self, bucket: str, object_path: str, *, ttl_seconds: int) -> str:
        try:
            return await asyncio.to_thread(self._signed_url_sync, bucket, object_path, ttl_seconds)
        except Exception as exc:
            # Broad ON PURPOSE — this is the adapter boundary: user ADC raises
            # AttributeError (no service_account_email), auth raises its own
            # hierarchy. Type name only (a raw repr could embed request detail).
            raise SigningUnavailable(type(exc).__name__) from exc

    def _signed_url_sync(self, bucket: str, object_path: str, ttl_seconds: int) -> str:
        import google.auth
        from google.auth.transport import requests as ga_requests
        from google.cloud import storage  # type: ignore[attr-defined]

        # V4 signing without a key file: the ambient SA (Workload Identity) signs
        # via IAM signBlob — requires roles/iam.serviceAccountTokenCreator on itself.
        credentials, _project = google.auth.default()
        credentials.refresh(ga_requests.Request())  # type: ignore[no-untyped-call]
        blob = storage.Client(credentials=credentials).bucket(bucket).blob(object_path)
        return str(
            blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=ttl_seconds),
                service_account_email=credentials.service_account_email,  # type: ignore[attr-defined]
                access_token=credentials.token,
            )
        )


class InMemoryRecordingStorage:
    """Test/dev double: bytes in a dict, deterministic 'signed' URLs."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def sha256_and_size(self, bucket: str, object_path: str) -> tuple[str, int] | None:
        body = self.objects.get((bucket, object_path))
        if body is None:
            return None
        return hashlib.sha256(body).hexdigest(), len(body)

    async def delete(self, bucket: str, object_path: str) -> None:
        self.objects.pop((bucket, object_path), None)

    async def exists(self, bucket: str, object_path: str) -> bool:
        return (bucket, object_path) in self.objects

    async def signed_url(self, bucket: str, object_path: str, *, ttl_seconds: int) -> str:
        return f"https://storage.local/{bucket}/{object_path}?ttl={ttl_seconds}"
