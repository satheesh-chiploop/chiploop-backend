import os
from pathlib import Path
from typing import Any, Dict, List, Optional


class SupabaseStorageAdapter:
    def __init__(self, storage: Any):
        self.storage = storage

    def from_(self, bucket: str) -> Any:
        return self.storage.from_(bucket)


class LocalStorageBucket:
    def __init__(self, root: Path, bucket: str):
        self.root = root / bucket
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key.strip("/")).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("Invalid storage path")
        return path

    def upload(self, key: str, content: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        path = self._path(key)
        if path.exists():
            raise FileExistsError(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_as_bytes(content))
        return {"path": key}

    def update(self, key: str, content: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_as_bytes(content))
        return {"path": key}

    def download(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        base = self._path(prefix)
        if not base.exists():
            return []
        entries = base.iterdir() if base.is_dir() else [base]
        return [{"name": entry.name, "metadata": {} if entry.is_dir() else {"size": entry.stat().st_size}} for entry in entries]


class LocalStorageAdapter:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root or os.getenv("CHIPLOOP_STORAGE_ROOT") or os.getenv("CHIPLOOP_PRIVATE_ARTIFACT_ROOT", "private_artifacts"))
        self.root.mkdir(parents=True, exist_ok=True)

    def from_(self, bucket: str) -> LocalStorageBucket:
        return LocalStorageBucket(self.root, bucket)


def _as_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    return bytes(content)


class S3StorageBucket:
    def __init__(self, client: Any, bucket: str, prefix: str = ""):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return "/".join(part for part in [self.prefix, key.strip("/")] if part)

    def upload(self, key: str, content: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        full_key = self._key(key)
        self.client.put_object(Bucket=self.bucket, Key=full_key, Body=content)
        return {"path": key}

    def update(self, key: str, content: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        return self.upload(key, content, options)

    def download(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        full_prefix = self._key(prefix)
        response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=full_prefix, Delimiter="/")
        out: List[Dict[str, Any]] = []
        for item in response.get("CommonPrefixes") or []:
            name = str(item.get("Prefix") or "").rstrip("/").split("/")[-1]
            if name:
                out.append({"name": name, "metadata": {}})
        for item in response.get("Contents") or []:
            name = str(item.get("Key") or "").split("/")[-1]
            if name:
                out.append({"name": name, "metadata": {"size": item.get("Size", 0)}})
        return out


class S3StorageAdapter:
    def __init__(self):
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for S3/MinIO storage adapter") from exc
        self.bucket = os.getenv("CHIPLOOP_STORAGE_BUCKET", "")
        if not self.bucket:
            raise RuntimeError("S3/MinIO storage adapter requires CHIPLOOP_STORAGE_BUCKET")
        self.prefix = os.getenv("CHIPLOOP_STORAGE_PREFIX", "")
        self.client = boto3.client(
            "s3",
            endpoint_url=os.getenv("CHIPLOOP_S3_ENDPOINT_URL") or None,
            region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None,
        )

    def from_(self, bucket: str) -> S3StorageBucket:
        return S3StorageBucket(self.client, self.bucket or bucket, self.prefix)


class AzureBlobStorageBucket:
    def __init__(self, container: Any, prefix: str = ""):
        self.container = container
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return "/".join(part for part in [self.prefix, key.strip("/")] if part)

    def upload(self, key: str, content: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        self.container.upload_blob(name=self._key(key), data=content, overwrite=False)
        return {"path": key}

    def update(self, key: str, content: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        self.container.upload_blob(name=self._key(key), data=content, overwrite=True)
        return {"path": key}

    def download(self, key: str) -> bytes:
        return self.container.download_blob(self._key(key)).readall()

    def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        full_prefix = self._key(prefix).rstrip("/")
        if full_prefix:
            full_prefix += "/"
        out: List[Dict[str, Any]] = []
        seen = set()
        for blob in self.container.list_blobs(name_starts_with=full_prefix):
            remainder = str(blob.name)[len(full_prefix):]
            name = remainder.split("/", 1)[0]
            if name and name not in seen:
                seen.add(name)
                out.append({"name": name, "metadata": {"size": getattr(blob, "size", 0)} if "/" not in remainder else {}})
        return out


class AzureBlobStorageAdapter:
    def __init__(self):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise RuntimeError("azure-storage-blob is required for Azure Blob storage adapter") from exc
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
        account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
        if connection_string:
            self.service = BlobServiceClient.from_connection_string(connection_string)
        elif account_url:
            self.service = BlobServiceClient(account_url=account_url)
        else:
            raise RuntimeError("Azure Blob adapter requires AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL")
        self.container_name = os.getenv("CHIPLOOP_STORAGE_BUCKET", "chiploop-artifacts")
        self.prefix = os.getenv("CHIPLOOP_STORAGE_PREFIX", "")

    def from_(self, bucket: str) -> AzureBlobStorageBucket:
        container = self.service.get_container_client(self.container_name or bucket)
        return AzureBlobStorageBucket(container, self.prefix)
