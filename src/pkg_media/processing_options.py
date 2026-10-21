from __future__ import annotations

from typing import Any, Dict

import httpx

from pkg_media._http import raise_for_status


class ProcessingOptionsAPI:
    """Metadata for building upload settings: kinds, visibilities, MIME types
    per kind, and each kind's processing pipeline with its step options.

    Returned as raw dicts so the client does not break when the server adds
    fields.
    """

    BASE = "/v1/manage/processing-options"

    def __init__(self, http: httpx.Client):
        self._http = http

    def get_schema(self) -> Dict[str, Any]:
        """Metadata schema, with the server's camelCase keys: ``kinds``,
        ``visibilities``, ``mimeTypesByKind``, ``pipelinesByKind`` (each with
        ``pipeline``, ``processors`` and ``configurableSteps``),
        ``autoStartSteps``, ``gatedSteps``, ``transcriptionAvailable`` and
        ``uploadDefaults``.
        """
        return self._get(f"{self.BASE}/schema")

    def get_pipelines(self) -> Dict[str, Any]:
        """Processing steps and their options per AssetKind."""
        return self._get(f"{self.BASE}/pipelines")

    def _get(self, path: str) -> Dict[str, Any]:
        resp = self._http.get(path)
        raise_for_status(resp)
        return resp.json()
