from webdav3.client import Client
from typing import List, Dict
import os
import io


def get_client(url: str, username: str, password: str) -> Client:
    options = {
        "webdav_hostname": url,
        "webdav_login": username,
        "webdav_password": password,
        "disable_check": True,
    }
    return Client(options)


def list_files(url: str, username: str, password: str, remote_path: str = "/") -> List[Dict]:
    client = get_client(url, username, password)
    items = client.list(remote_path, get_info=True)
    result = []
    for item in items:
        if item.get("path") == remote_path:
            continue
        name = item.get("name") or os.path.basename(item.get("path", "").rstrip("/"))
        is_dir = item.get("isdir", False)
        result.append({
            "name": name,
            "path": item.get("path", ""),
            "is_dir": is_dir,
            "size": item.get("size", 0),
        })
    result.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return result


def download_file(url: str, username: str, password: str, remote_path: str, local_path: str):
    client = get_client(url, username, password)
    client.download_sync(remote_path=remote_path, local_path=local_path)


def download_bytes(url: str, username: str, password: str, remote_path: str) -> bytes:
    """Download a remote file into memory and return its bytes."""
    client = get_client(url, username, password)
    buf = io.BytesIO()
    client.download_from(buf, remote_path)
    return buf.getvalue()


def check_exists(url: str, username: str, password: str, remote_path: str) -> bool:
    """Return True if the remote path exists."""
    client = get_client(url, username, password)
    return client.check(remote_path)


def upload_bytes(url: str, username: str, password: str, remote_path: str, data: bytes):
    """Upload bytes to a remote path."""
    client = get_client(url, username, password)
    buf = io.BytesIO(data)
    client.upload_to(buf, remote_path)
