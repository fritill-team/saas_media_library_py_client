from __future__ import annotations

from typing import Any, Dict


class ProcessingOptionsAPI:
    """Metadata for building upload settings: kinds, visibilities, MIME types
    per kind, and each kind's processing pipeline with its step options
    (HTTP ``/manage/processing-options``, gRPC ``fritill.media.v1.Processing``).

    Returned as raw dicts so the client does not break when the server adds
    fields.
    """

    BASE = "/v1/manage/processing-options"

    def __init__(self, transport: Any):
        self._transport = transport

    async def get_schema(self) -> Dict[str, Any]:
        """Metadata schema, with the server's camelCase keys: ``kinds``,
        ``visibilities``, ``mimeTypesByKind``, ``pipelinesByKind`` (each with
        ``pipeline``, ``processors`` and ``configurableSteps``),
        ``autoStartSteps``, ``gatedSteps``, ``transcriptionAvailable`` and
        ``uploadDefaults``.
        """
        return await self._transport.get_processing_schema()

    async def get_pipelines(self) -> Dict[str, Any]:
        """Processing steps and their options per AssetKind."""
        return await self._transport.get_processing_pipelines()
