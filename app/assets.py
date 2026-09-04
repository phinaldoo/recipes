from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

_BUILD_ID = re.compile(r"^[a-f0-9]{16}$")
_SOURCE_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_FINGERPRINTED_URL = re.compile(
    r"^/static/assets/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$"
)
_ASSET_REFERENCE = re.compile(r"asset\(\s*['\"]([^'\"]+)['\"]\s*\)")
_LEGACY_STATIC_URL = re.compile(r"/static/(?:css|js|pwa)/")


@dataclass(frozen=True)
class FrontendAssets:
    build_id: str
    assets: MappingProxyType[str, str]
    precache: tuple[str, ...]
    offline_url: str
    manifest_url: str
    asset_directory: Path
    service_worker_path: Path

    @classmethod
    def load(cls, dist_directory: Path) -> FrontendAssets:
        manifest_path = dist_directory / "asset-manifest.json"
        try:
            raw = cast(Any, json.loads(manifest_path.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Frontend assets are missing. Run `npm ci && npm run build` before starting "
                "the application."
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Frontend asset manifest is not valid JSON") from exc

        if not isinstance(raw, dict) or raw.get("schema") != 1:
            raise RuntimeError("Frontend asset manifest has an unsupported schema")

        build_id = raw.get("build_id")
        source_digest = raw.get("source_digest")
        raw_assets = raw.get("assets")
        raw_precache = raw.get("precache")
        offline_url = raw.get("offline_url")
        manifest_url = raw.get("manifest_url")
        if not isinstance(build_id, str) or _BUILD_ID.fullmatch(build_id) is None:
            raise RuntimeError("Frontend asset manifest has an invalid build ID")
        if not isinstance(source_digest, str) or _SOURCE_DIGEST.fullmatch(source_digest) is None:
            raise RuntimeError("Frontend asset manifest has an invalid source digest")
        if not isinstance(raw_assets, dict) or not raw_assets:
            raise RuntimeError("Frontend asset manifest contains no assets")
        if not isinstance(raw_precache, list) or not raw_precache:
            raise RuntimeError("Frontend asset manifest contains no precache allowlist")
        if offline_url != f"/offline?v={build_id}":
            raise RuntimeError("Frontend asset manifest has an invalid offline URL")
        if manifest_url != f"/manifest.webmanifest?v={build_id}":
            raise RuntimeError("Frontend asset manifest has an invalid web-manifest URL")

        asset_directory = dist_directory / "assets"
        if not asset_directory.is_dir():
            raise RuntimeError("Frontend asset directory is missing from the build")
        built_asset_urls: set[str] = set()
        for target in asset_directory.rglob("*"):
            if not target.is_file():
                continue
            url = f"/static/{target.relative_to(dist_directory).as_posix()}"
            if _FINGERPRINTED_URL.fullmatch(url) is None:
                raise RuntimeError(f"Frontend build contains a non-fingerprinted asset: {target}")
            built_asset_urls.add(url)

        assets: dict[str, str] = {}
        for logical_name, url in raw_assets.items():
            if not isinstance(logical_name, str) or not isinstance(url, str):
                raise RuntimeError("Frontend asset manifest contains a malformed asset mapping")
            if _FINGERPRINTED_URL.fullmatch(url) is None:
                raise RuntimeError(f"Frontend asset is not fingerprinted: {url}")
            if url not in built_asset_urls:
                raise RuntimeError(f"Frontend asset is missing from the build: {url}")
            assets[logical_name] = url

        precache: list[str] = []
        for url in raw_precache:
            if not isinstance(url, str):
                raise RuntimeError("Frontend precache allowlist contains a malformed URL")
            if url != offline_url and _FINGERPRINTED_URL.fullmatch(url) is None:
                raise RuntimeError(f"Frontend precache URL is not immutable: {url}")
            if url.startswith("/static/") and url not in built_asset_urls:
                raise RuntimeError(f"Frontend precache asset is missing from the build: {url}")
            precache.append(url)
        if len(precache) != len(set(precache)):
            raise RuntimeError("Frontend precache allowlist contains duplicate URLs")

        service_worker_path = dist_directory / "service-worker.js"
        if not service_worker_path.is_file():
            raise RuntimeError("Generated service worker is missing from the frontend build")
        service_worker = service_worker_path.read_text(encoding="utf-8")
        if f'const CACHE_NAME = "rezepte-static-{build_id}";' not in service_worker:
            raise RuntimeError("Generated service worker has an invalid cache name")
        if any(json.dumps(url) not in service_worker for url in precache):
            raise RuntimeError("Generated service worker is missing a precache URL")

        static_directory = dist_directory.parent
        project_directory = static_directory.parents[1]
        templates_directory = project_directory / "app" / "templates"
        for template in templates_directory.rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            if _LEGACY_STATIC_URL.search(source):
                raise RuntimeError(f"Template contains a legacy static URL: {template}")
            for logical_name in _ASSET_REFERENCE.findall(source):
                if logical_name not in assets:
                    raise RuntimeError(
                        f"Template references an unknown frontend asset: {logical_name}"
                    )
        source_files = sorted(
            (
                *list((static_directory / "css").rglob("*")),
                *list((static_directory / "js").rglob("*")),
                *list((static_directory / "pwa").rglob("*")),
                static_directory / "service-worker.js",
                templates_directory / "offline.html",
            )
        )
        source_identity = hashlib.sha256()
        for source_file in source_files:
            if not source_file.is_file():
                continue
            source_identity.update(source_file.relative_to(project_directory).as_posix().encode())
            source_identity.update(source_file.read_bytes())
        if source_identity.hexdigest() != source_digest:
            raise RuntimeError(
                "Frontend build is stale. Run `npm run build` after changing frontend sources."
            )

        return cls(
            build_id=build_id,
            assets=MappingProxyType(assets),
            precache=tuple(precache),
            offline_url=offline_url,
            manifest_url=manifest_url,
            asset_directory=asset_directory,
            service_worker_path=service_worker_path,
        )

    def url(self, logical_name: str) -> str:
        try:
            return self.assets[logical_name]
        except KeyError as exc:
            raise RuntimeError(f"Unknown frontend asset: {logical_name}") from exc


frontend_assets = FrontendAssets.load(Path(__file__).parent / "static" / "dist")
