from __future__ import annotations

from hashlib import sha256
from importlib import import_module, util
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[3]
MIN_PART_SIZE = 5 * 1024 * 1024
MINIO_RELEASE = "RELEASE.2025-10-15T17-29-55Z"
MINIO_COMMIT = "9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a"


def _storage_module():
    spec = util.find_spec("app.services.audio_storage")
    assert spec is not None, "private audio storage service is not implemented"
    return import_module("app.services.audio_storage")


class FakeResponse(BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.release_calls = 0

    def release_conn(self) -> None:
        self.release_calls += 1


class FakeMinioClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.remove_calls: list[tuple[str, str]] = []
        self.copy_calls: list[tuple[str, str, Any]] = []
        self.range_calls: list[tuple[str, str, int, int]] = []
        self.existing_buckets = {"audio-quarantine"}
        self.created_buckets: list[str] = []
        self.private_policy_calls: list[str] = []
        self.responses: list[FakeResponse] = []

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: Any,
        length: int,
        *,
        content_type: str,
        part_size: int,
        metadata: dict[str, str] | None = None,
    ) -> SimpleNamespace:
        body = bytearray()
        while len(body) < length:
            chunk = data.read(min(3, length - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        self.objects[(bucket_name, object_name)] = bytes(body)
        self.put_calls.append(
            {
                "bucket": bucket_name,
                "key": object_name,
                "length": length,
                "content_type": content_type,
                "part_size": part_size,
                "metadata": metadata or {},
            }
        )
        return SimpleNamespace(etag="upload-etag", version_id=None)

    def stat_object(self, bucket_name: str, object_name: str) -> SimpleNamespace:
        value = self.objects[(bucket_name, object_name)]
        return SimpleNamespace(
            size=len(value),
            etag="stat-etag",
            content_type="audio/mpeg",
            last_modified=None,
            metadata={},
            version_id=None,
        )

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        offset: int,
        length: int,
    ) -> FakeResponse:
        self.range_calls.append((bucket_name, object_name, offset, length))
        value = self.objects[(bucket_name, object_name)][offset : offset + length]
        response = FakeResponse(value)
        self.responses.append(response)
        return response

    def copy_object(
        self,
        bucket_name: str,
        object_name: str,
        source: Any,
    ) -> SimpleNamespace:
        self.copy_calls.append((bucket_name, object_name, source))
        value = self.objects[(source.bucket_name, source.object_name)]
        self.objects[(bucket_name, object_name)] = value
        return SimpleNamespace(etag="copy-etag", version_id=None)

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        self.remove_calls.append((bucket_name, object_name))
        self.objects.pop((bucket_name, object_name), None)

    def bucket_exists(self, bucket_name: str) -> bool:
        return bucket_name in self.existing_buckets

    def make_bucket(self, bucket_name: str) -> None:
        self.existing_buckets.add(bucket_name)
        self.created_buckets.append(bucket_name)

    def delete_bucket_policy(self, bucket_name: str) -> None:
        self.private_policy_calls.append(bucket_name)


def _storage(client: FakeMinioClient, *, max_bytes: int = 1024):
    module = _storage_module()
    return module.MinioAudioStorage(
        client,
        max_object_bytes=max_bytes,
        part_size_bytes=MIN_PART_SIZE,
    )


def test_put_stream_uses_opaque_key_multipart_and_sha256() -> None:
    module = _storage_module()
    client = FakeMinioClient()
    payload = b"SYCO23-private-audio"
    digest = sha256(payload).hexdigest()

    stored = _storage(client).put_stream(
        module.AUDIO_QUARANTINE_BUCKET,
        BytesIO(payload),
        length=len(payload),
        content_type="audio/mpeg",
        expected_sha256=digest,
    )

    assert stored.bucket == module.AUDIO_QUARANTINE_BUCKET
    assert stored.key.startswith("objects/")
    assert ".." not in stored.key
    assert stored.sha256 == digest
    assert stored.size == len(payload)
    assert client.put_calls == [
        {
            "bucket": module.AUDIO_QUARANTINE_BUCKET,
            "key": stored.key,
            "length": len(payload),
            "content_type": "audio/mpeg",
            "part_size": MIN_PART_SIZE,
            "metadata": {"sha256": digest},
        }
    ]


def test_put_stream_rejects_bounds_and_removes_checksum_mismatch() -> None:
    module = _storage_module()
    client = FakeMinioClient()
    storage = _storage(client, max_bytes=8)

    with pytest.raises(module.AudioStorageBoundsError):
        storage.put_stream(
            module.AUDIO_QUARANTINE_BUCKET,
            BytesIO(b"too-large"),
            length=9,
            content_type="audio/mpeg",
        )
    assert client.put_calls == []

    with pytest.raises(module.AudioChecksumMismatch):
        storage.put_stream(
            module.AUDIO_QUARANTINE_BUCKET,
            BytesIO(b"12345678"),
            length=8,
            content_type="audio/mpeg",
            expected_sha256="0" * 64,
        )
    assert len(client.remove_calls) == 1
    assert client.remove_calls[0][0] == module.AUDIO_QUARANTINE_BUCKET
    assert client.objects == {}


def test_range_reader_closes_connection_and_validates_object_keys() -> None:
    module = _storage_module()
    client = FakeMinioClient()
    storage = _storage(client)
    key = storage.new_object_key()
    client.objects[(module.AUDIO_ORIGINALS_BUCKET, key)] = b"0123456789"

    with storage.open_range(
        module.AUDIO_ORIGINALS_BUCKET,
        key,
        start=2,
        length=4,
    ) as response:
        assert response.read() == b"2345"

    assert client.range_calls == [(module.AUDIO_ORIGINALS_BUCKET, key, 2, 4)]
    assert client.responses[0].closed is True
    assert client.responses[0].release_calls == 1

    for invalid in ("../secret", "/absolute", "objects/not-opaque", "objects/aa/../../x"):
        with pytest.raises(module.InvalidAudioObjectKey):
            storage.stat(module.AUDIO_ORIGINALS_BUCKET, invalid)


def test_copy_promotes_to_a_new_opaque_key_and_delete_is_bounded() -> None:
    module = _storage_module()
    client = FakeMinioClient()
    storage = _storage(client)
    source_key = storage.new_object_key()
    client.objects[(module.AUDIO_QUARANTINE_BUCKET, source_key)] = b"audio"

    promoted = storage.copy(
        module.AUDIO_QUARANTINE_BUCKET,
        source_key,
        module.AUDIO_ORIGINALS_BUCKET,
    )

    assert promoted.key != source_key
    assert promoted.key.startswith("objects/")
    assert client.copy_calls[0][2].bucket_name == module.AUDIO_QUARANTINE_BUCKET
    assert client.copy_calls[0][2].object_name == source_key

    storage.delete(module.AUDIO_ORIGINALS_BUCKET, promoted.key)
    assert client.remove_calls[-1] == (module.AUDIO_ORIGINALS_BUCKET, promoted.key)


def test_bucket_initialization_creates_all_private_audio_buckets() -> None:
    module = _storage_module()
    client = FakeMinioClient()
    storage = _storage(client)

    storage.ensure_buckets()

    assert set(client.created_buckets) == {
        module.AUDIO_ORIGINALS_BUCKET,
        module.AUDIO_DERIVATIVES_BUCKET,
    }
    assert set(client.private_policy_calls) == set(module.AUDIO_BUCKETS)


def test_storage_enabled_requires_server_side_credentials_in_production() -> None:
    with pytest.raises(ValueError, match="MinIO"):
        Settings(
            environment="production",
            repository_mode="postgres",
            auth_mode="supabase",
            supabase_url="https://project.supabase.co",
            supabase_anon_key="anon",
            audio_storage_enabled=True,
            minio_access_key="",
            minio_secret_key="",
        )


def test_compose_keeps_minio_internal_and_initializes_fixed_buckets() -> None:
    for filename in ("docker-compose.yml", "docker-compose.production.yml"):
        compose = yaml.safe_load((ROOT / filename).read_text())
        services = compose["services"]
        minio = services["minio"]
        initializer = services["audio-storage-init"]

        assert minio.get("ports", []) == []
        assert minio["expose"] == ["9000"]
        assert minio["environment"]["MINIO_BROWSER"] == "off"
        assert any("minio_data:/data" == item for item in minio["volumes"])
        command = initializer["command"]
        command_text = " ".join(command) if isinstance(command, list) else command
        assert "app.cli.init_audio_storage" in command_text
        assert initializer["depends_on"]["minio"]["condition"] == "service_healthy"

    caddy = (ROOT / "docker" / "Caddyfile").read_text()
    assert "minio" not in caddy.lower()


def test_minio_server_is_built_from_pinned_source_release() -> None:
    dockerfile_path = ROOT / "docker" / "minio.Dockerfile"
    assert dockerfile_path.exists()
    dockerfile = dockerfile_path.read_text()

    assert f"ARG MINIO_VERSION={MINIO_RELEASE}" in dockerfile
    assert f"ARG MINIO_COMMIT={MINIO_COMMIT}" in dockerfile
    assert 'git fetch --depth 1 origin "${MINIO_VERSION}"' in dockerfile
    assert 'test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"' in dockerfile
    assert "go run buildscripts/gen-ldflags.go" in dockerfile
    assert 'go build -tags kqueue -trimpath --ldflags "${LDFLAGS}"' in dockerfile
    assert "go install github.com/minio/minio" not in dockerfile

    for filename in ("docker-compose.yml", "docker-compose.production.yml"):
        compose_path = ROOT / filename
        compose_text = compose_path.read_text()
        compose = yaml.safe_load(compose_text)
        minio = compose["services"]["minio"]

        assert "quay.io/minio/minio" not in compose_text
        assert minio["build"]["dockerfile"] == "docker/minio.Dockerfile"
        assert minio["build"]["args"]["MINIO_VERSION"] == MINIO_RELEASE
        healthcheck = " ".join(minio["healthcheck"]["test"])
        assert "/minio/health/live" in healthcheck


def test_operations_document_records_private_topology_and_single_copy_risk() -> None:
    document = ROOT / "docs" / "minio-operations.md"
    assert document.exists()
    text = document.read_text()

    for expected in (
        "audio-quarantine",
        "audio-originals",
        "audio-derivatives",
        "not exposed",
        "second audio copy",
        "restore",
        "checksum",
    ):
        assert expected in text
