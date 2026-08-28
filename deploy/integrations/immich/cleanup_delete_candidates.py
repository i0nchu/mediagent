#!/usr/bin/env python3
"""Remove Immich delete-candidate assets through Mediagent's managed lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def api_request(base_url: str, api_key: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api{path}",
        data=data,
        headers={
            "x-api-key": api_key,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Immich {request.method} {path} failed with HTTP {error.code}.") from error


def album_assets(base_url: str, api_key: str, album_id: str):
    page: int | str = 1
    while True:
        result = api_request(
            base_url,
            api_key,
            "/search/metadata",
            {"albumIds": [album_id], "page": page, "size": 250},
        )
        assets = result.get("assets", {})
        yield from assets.get("items", [])
        page = assets.get("nextPage")
        if not page:
            return


def asset_original_path(base_url: str, api_key: str, asset: dict[str, Any]) -> str | None:
    if asset.get("originalPath"):
        return str(asset["originalPath"])
    asset_id = asset.get("id")
    if not asset_id:
        return None
    return api_request(base_url, api_key, f"/assets/{asset_id}").get("originalPath")


def find_album(base_url: str, api_key: str, album_name: str) -> dict[str, Any]:
    albums = api_request(base_url, api_key, "/albums")
    matches = [album for album in albums if album.get("albumName") == album_name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(f"Immich album not found: {album_name}")
    raise RuntimeError(f"Multiple Immich albums have the same name: {album_name}")


def source_path_from_immich(
    original_path: str,
    *,
    container_root: str,
    library_root: Path,
) -> Path | None:
    prefix = container_root.rstrip("/") + "/"
    if not original_path.startswith(prefix):
        return None
    source = (library_root / original_path.removeprefix(prefix)).resolve()
    try:
        source.relative_to(library_root.resolve())
    except ValueError:
        return None
    return source


def mediagent_remove_command(
    *,
    project: Path,
    db_path: Path,
    library_root: Path,
    source: Path,
    asset_id: str,
) -> list[str]:
    return [
        "/usr/bin/env",
        "uv",
        "run",
        "--locked",
        "--project",
        str(project),
        "mediagent",
        "library",
        "remove",
        "--path",
        str(source),
        "--db-path",
        str(db_path),
        "--library-root",
        str(library_root),
        "--reason",
        "Immich delete-candidate album",
        "--external-ref",
        f"immich:{asset_id}",
        "--json",
    ]


def remove_with_mediagent(
    *,
    project: Path,
    db_path: Path,
    library_root: Path,
    source: Path,
    asset_id: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    completed = runner(
        mediagent_remove_command(
            project=project,
            db_path=db_path,
            library_root=library_root,
            source=source,
            asset_id=asset_id,
        ),
        cwd=project,
        env={
            **os.environ,
            "MEDIAGENT_DB_PATH": str(db_path),
            "MEDIAGENT_LIBRARY_DIR": str(library_root),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"Mediagent remove returned invalid JSON (exit {completed.returncode}).") from exc
    if completed.returncode != 0 or payload.get("status") != "success":
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or "unknown_error")
        raise RuntimeError(f"Mediagent remove failed for Immich asset {asset_id}: {code}")
    return payload.get("data") if isinstance(payload.get("data"), dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Apply removals through Mediagent.")
    args = parser.parse_args()

    base_url = os.environ.get("IMMICH_URL", "http://127.0.0.1:2284").rstrip("/")
    api_key = os.environ["IMMICH_API_KEY"]
    album_name = os.environ.get("IMMICH_DELETE_ALBUM", "_delete_candidates")
    container_root = os.environ.get("IMMICH_CONTAINER_LIBRARY_ROOT", "/mnt/mediagent")
    library_root = Path(os.environ.get("MEDIAGENT_LIBRARY_ROOT", "/data/nas/mediagent")).resolve()
    project = Path(os.environ.get("MEDIAGENT_PROJECT", "/data/services/mediagent")).resolve()
    db_path = Path(
        os.environ.get("MEDIAGENT_DB_PATH", str(project / "data/mediagent.sqlite3"))
    ).resolve()
    dry_run = env_bool("DRY_RUN", True) and not args.execute

    album = find_album(base_url, api_key, album_name)
    assets = list(album_assets(base_url, api_key, str(album["id"])))
    removed = 0
    skipped = 0
    failed = 0
    print(f"Album: {album_name} ({album['id']})")
    print(f"Assets: {len(assets)}")
    print(f"Mode: {'DRY-RUN' if dry_run else 'EXECUTE'}")

    for asset in assets:
        asset_id = str(asset.get("id") or "<unknown>")
        original_path = asset_original_path(base_url, api_key, asset)
        source = (
            source_path_from_immich(
                original_path,
                container_root=container_root,
                library_root=library_root,
            )
            if original_path
            else None
        )
        if source is None:
            print(f"SKIP unmanaged path: {asset_id}")
            skipped += 1
            continue
        if dry_run:
            print(f"DRY-RUN mediagent remove: {source} (immich:{asset_id})")
            removed += 1
            continue
        try:
            result = remove_with_mediagent(
                project=project,
                db_path=db_path,
                library_root=library_root,
                source=source,
                asset_id=asset_id,
            )
        except RuntimeError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(f"REMOVED: {source} -> {result.get('trash_path')} ({result.get('removal_id')})")
        removed += 1

    print(f"Done. Candidates: {removed}, skipped: {skipped}, failed: {failed}")
    if dry_run:
        print("No files were moved. Run with --execute or set DRY_RUN=false to apply removals.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
