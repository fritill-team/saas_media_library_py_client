"""Transports behind ``AsyncMediaClient``: HTTP (``http_transport.HttpTransport``)
and gRPC (``grpc_transport.GrpcTransport``).

Both expose the same async operations and return the same pydantic models;
the API classes (``client.assets``…) keep the client-side logic (validation,
batching, caching) and call one of them. Request bodies are passed as the HTTP
API's JSON payloads (``create_session``, ``issue_grant``); the gRPC transport
maps them to the ``fritill.media.v1`` messages.
"""
