from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import httpx

from saas_media_library._http import raise_for_status


class ProcessingOptionsAPI:
    """Metadata for building upload settings: kinds, visibilities, MIME types
    per kind, and each kind's processing pipeline with its step options.

    Returned as raw dicts so the client does not break when the server adds
    fields. Older servers only expose this under the resource-policy routes;
    on a 404 from the new route the legacy one is used (see ``get_schema``
    for how its payload is converted).
    """

    BASE = "/v1/manage/processing-options"
    LEGACY_BASE = "/v1/manage/resource-policies"

    def __init__(self, http: httpx.Client):
        self._http = http

    def get_schema(self) -> Dict[str, Any]:
        """Metadata schema, with the server's camelCase keys: ``kinds``,
        ``visibilities``, ``mimeTypesByKind``, ``pipelinesByKind`` (each with
        ``pipeline``, ``processors`` and ``configurableSteps``),
        ``autoStartSteps``, ``gatedSteps``, ``transcriptionAvailable`` and
        ``uploadDefaults``.

        On an older server the legacy policy schema is converted to this
        shape. It only yields ``kinds``, ``visibilities``, ``mimeTypesByKind``
        and ``pipelinesByKind``; ``autoStartSteps``, ``gatedSteps``,
        ``transcriptionAvailable`` and ``uploadDefaults`` cannot be derived
        from it and are left out, as are the legacy ``defaults`` (policy
        defaults) and ``permission_schema``.
        """
        return self._get_with_fallback(
            f"{self.BASE}/schema",
            f"{self.LEGACY_BASE}/schema",
            convert_legacy=_schema_from_legacy,
        )

    def get_pipelines(self) -> Dict[str, Any]:
        """Processing steps and their options per AssetKind (same payload on
        old and new servers)."""
        return self._get_with_fallback(
            f"{self.BASE}/pipelines",
            f"{self.LEGACY_BASE}/pipelines/available-steps",
        )

    def _get_with_fallback(
        self,
        path: str,
        legacy_path: str,
        *,
        convert_legacy: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        resp = self._http.get(path)
        if resp.status_code != 404:
            raise_for_status(resp)
            return resp.json()
        resp = self._http.get(legacy_path)
        raise_for_status(resp)
        data = resp.json()
        return convert_legacy(data) if convert_legacy is not None else data


# Legacy (snake_case) key -> key of GET /manage/processing-options/schema.
_LEGACY_SCHEMA_KEYS = {
    "kinds": "kinds",
    "visibilities": "visibilities",
    "mime_types_by_kind": "mimeTypesByKind",
}


def _schema_from_legacy(legacy: Dict[str, Any]) -> Dict[str, Any]:
    """The keys of ``GET /manage/resource-policies/schema`` that exist in the
    new schema, renamed to its camelCase. Everything else is dropped."""
    schema = {new: legacy[old] for old, new in _LEGACY_SCHEMA_KEYS.items() if old in legacy}
    pipelines = legacy.get("pipelines_by_kind")
    if isinstance(pipelines, dict):
        schema["pipelinesByKind"] = {
            kind: {
                ("configurableSteps" if key == "configurable_steps" else key): value
                for key, value in pipeline.items()
            }
            for kind, pipeline in pipelines.items()
        }
    return schema
