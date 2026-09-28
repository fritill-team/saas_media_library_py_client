from __future__ import annotations

from typing import Any, Dict

import httpx

from saas_media_library._http import raise_for_status


class ProcessingOptionsAPI:
    """Metadata for building upload settings: kinds, visibilities, MIME types
    per kind, and each kind's processing pipeline with its step options.

    Returned as raw dicts so the client does not break when the server adds
    fields. Older servers only expose this under the resource-policy routes;
    on a 404 from the new route the legacy one is used.
    """

    BASE = "/v1/manage/processing-options"
    LEGACY_BASE = "/v1/manage/resource-policies"

    def __init__(self, http: httpx.Client):
        self._http = http

    def get_schema(self) -> Dict[str, Any]:
        """Full metadata schema (kinds, visibilities, MIME types, pipelines, defaults)."""
        return self._get_with_fallback(f"{self.BASE}/schema", f"{self.LEGACY_BASE}/schema")

    def get_pipelines(self) -> Dict[str, Any]:
        """Processing steps and their options per AssetKind."""
        return self._get_with_fallback(
            f"{self.BASE}/pipelines",
            f"{self.LEGACY_BASE}/pipelines/available-steps",
        )

    def _get_with_fallback(self, path: str, legacy_path: str) -> Dict[str, Any]:
        resp = self._http.get(path)
        if resp.status_code == 404:
            resp = self._http.get(legacy_path)
        raise_for_status(resp)
        return resp.json()
