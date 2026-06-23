"""Production AnchorSink: writes immutable anchor objects to an object-locked
GCS bucket. google-cloud-storage is a sync SDK, so every call is wrapped in
asyncio.to_thread (the stack is asyncio-locked; no anyio). Uploads are
create-only (if_generation_match=0); the bucket's locked retention policy is the
real WORM guarantee (provisioned per adr/devops-todo.md)."""

import asyncio


class GCSAnchorSink:
    def __init__(self, bucket: str, prefix: str) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    async def write_anchor(self, key: str, body: bytes) -> None:
        await asyncio.to_thread(self._write_sync, key, body)

    def _write_sync(self, key: str, body: bytes) -> None:
        from google.cloud import storage  # type: ignore[import-untyped]

        blob = storage.Client().bucket(self._bucket).blob(self._full_key(key))
        blob.upload_from_string(body, content_type="application/json", if_generation_match=0)

    async def read_latest(self) -> bytes | None:
        return await asyncio.to_thread(self._read_latest_sync)

    def _read_latest_sync(self) -> bytes | None:
        from google.cloud import storage

        client = storage.Client()
        prefix = self._full_key("anchors/")
        blobs = list(client.list_blobs(self._bucket, prefix=prefix))
        if not blobs:
            return None
        latest: bytes = max(blobs, key=lambda b: b.name).download_as_bytes()
        return latest
