"""Google Drive integration: list, download, archive CSVs and manage cache files."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

logger = logging.getLogger(__name__)

_CACHE_YFINANCE = "yfinance_cache.json"
_CACHE_ACCOUNT_STATE = "account_state.json"


def build_drive_service(credentials):
    """Build Google Drive API v3 service from OAuth credentials."""
    return build("drive", "v3", credentials=credentials)


def list_pending_csvs(service, upload_folder_id: str) -> list[dict]:
    """List all CSV files in the upload folder.

    Returns list of {id, name} dicts, ordered by name.
    """
    results = (
        service.files()
        .list(
            q=(
                f"'{upload_folder_id}' in parents"
                " and fileExtension='csv'"
                " and trashed=false"
            ),
            fields="files(id, name)",
            orderBy="name",
        )
        .execute()
    )
    return results.get("files", [])


def download_csv(service, file_id: str, dest_path: Path) -> None:
    """Download a Drive file to a local path."""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=file_id)
    with dest_path.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    logger.debug("Downloaded %s → %s", file_id, dest_path)


def move_to_processed(
    service,
    file_id: str,
    filename: str,
    upload_folder_id: str,
    processed_folder_id: str,
) -> None:
    """Move a successfully processed CSV from upload/ to processed/ with a UTC timestamp prefix."""
    _move_file(service, file_id, filename, upload_folder_id, processed_folder_id)


def move_to_failed(
    service,
    file_id: str,
    filename: str,
    upload_folder_id: str,
    failed_folder_id: str,
) -> None:
    """Move a failed CSV from upload/ to failed/ with a UTC timestamp prefix."""
    _move_file(service, file_id, filename, upload_folder_id, failed_folder_id)


def load_yfinance_cache(service, cache_folder_id: str) -> dict:
    """Download yfinance_cache.json from the Drive cache folder. Return {} if not found."""
    return _load_json(service, cache_folder_id, _CACHE_YFINANCE)


def save_yfinance_cache(service, cache_folder_id: str, cache: dict) -> None:
    """Upload/overwrite yfinance_cache.json in the Drive cache folder."""
    _save_json(service, cache_folder_id, _CACHE_YFINANCE, cache)


def load_account_state(service, cache_folder_id: str) -> dict:
    """Download account_state.json from the Drive cache folder. Return {} if not found."""
    return _load_json(service, cache_folder_id, _CACHE_ACCOUNT_STATE)


def save_account_state(service, cache_folder_id: str, account_state: dict) -> None:
    """Upload/overwrite account_state.json in the Drive cache folder."""
    _save_json(service, cache_folder_id, _CACHE_ACCOUNT_STATE, account_state)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _move_file(
    service,
    file_id: str,
    filename: str,
    remove_folder_id: str,
    add_folder_id: str,
) -> None:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    new_name = f"{timestamp}_{filename}"
    service.files().update(
        fileId=file_id,
        addParents=add_folder_id,
        removeParents=remove_folder_id,
        body={"name": new_name},
        fields="id, parents",
    ).execute()
    logger.info("Archived %s → %s (folder %s)", filename, new_name, add_folder_id)


def _find_file_id(service, folder_id: str, filename: str) -> str | None:
    """Return the Drive file ID for ``filename`` in ``folder_id``, or None."""
    files = (
        service.files()
        .list(
            q=(
                f"'{folder_id}' in parents"
                f" and name='{filename}'"
                " and trashed=false"
            ),
            fields="files(id)",
        )
        .execute()
        .get("files", [])
    )
    return files[0]["id"] if files else None


def _load_json(service, folder_id: str, filename: str) -> dict:
    file_id = _find_file_id(service, folder_id, filename)
    if file_id is None:
        logger.debug("%s not found in Drive folder %s", filename, folder_id)
        return {}

    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    data = json.loads(buf.read().decode("utf-8"))
    logger.debug("Loaded %s from Drive", filename)
    return data


def _save_json(service, folder_id: str, filename: str, data: dict) -> None:
    payload = json.dumps(data, indent=2, default=str).encode("utf-8")
    media = MediaInMemoryUpload(payload, mimetype="application/json")
    existing_id = _find_file_id(service, folder_id, filename)
    if existing_id:
        service.files().update(
            fileId=existing_id,
            media_body=media,
        ).execute()
        logger.debug("Updated %s in Drive", filename)
    else:
        service.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
        ).execute()
        logger.debug("Created %s in Drive", filename)
