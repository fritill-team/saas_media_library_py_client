# SaaS Media Library - Python Client

Python client for the SaaS Media Library API. Designed for two primary use cases:

1. **Init containers** — Sync resource policies at app startup
2. **Runtime** — Access assets and manage visibility from application code

## Installation

```bash
pip install git+ssh://git@github.com/fritill-team/saas_media_library_py_client.git
```

Or add to `requirements.txt`:

```
saas-media-library @ git+ssh://git@github.com/fritill-team/saas_media_library_py_client.git
```

## Quick Start

```python
from saas_media_library import MediaLibraryClient

client = MediaLibraryClient(
    base_url="https://api.media-library.fritill.ae",
    tenant_id="your-tenant-id",
    token="service-account-jwt",
)
```

## Use Case 1: Resource Policies (Init Container)

Declare all resource policies at startup so the media library knows what each
app accepts (file types, sizes, processing pipelines).

```python
from saas_media_library import MediaLibraryClient, AssetKind, Visibility

client = MediaLibraryClient(
    base_url="https://api.media-library.fritill.ae",
    tenant_id="my-tenant",
    token="service-account-jwt",
)

# Sync all policies in one call — creates or updates each one
client.policies.sync_policies([
    {
        "resource_type": "courses",
        "collection_name": "cover",
        "kind": AssetKind.IMAGE,
        "visibility": Visibility.PUBLIC,
        "allowed_mime_types": {"image/jpeg", "image/png", "image/webp"},
        "max_size_bytes": 5_000_000,
        # Pre-configure step_options so the pipeline runs fully automatically.
        # Without options, configurable steps pause for user input.
        "step_options": {
            "image_derive": {"formats": ["webp"], "sizes": [256, 512, 1024]}
        },
    },
    {
        "resource_type": "courses",
        "collection_name": "video",
        "kind": AssetKind.VIDEO,
        "visibility": Visibility.RESTRICTED,
        "allowed_mime_types": {"video/mp4", "video/quicktime"},
        "max_size_bytes": 2_000_000_000,
        "step_options": {
            "video_transcode": {"crf": 23, "preset": "fast"},
            "video_adaptive_stream": {"formats": ["hls"], "resolutions": [360, 720, 1080]},
            "video_thumbnail": {"timestamp": 1.0},
        },
    },
    {
        "resource_type": "courses",
        "collection_name": "attachments",
        "kind": AssetKind.DOCUMENT,
        "visibility": Visibility.PRIVATE,
        "allowed_mime_types": {"application/pdf"},
        "max_size_bytes": 50_000_000,
    },
])

client.close()
```

### Individual operations

```python
# Upsert a single policy
policy = client.policies.upsert(
    resource_type="products",
    kind=AssetKind.IMAGE,
    visibility=Visibility.PUBLIC,
    allowed_mime_types={"image/jpeg", "image/png"},
    max_size_bytes=10_000_000,
    step_options={"image_derive": {"formats": ["webp"], "sizes": [256, 512]}},
)

# Get / list
policy = client.policies.get(policy.id)
all_policies = client.policies.list()
image_policies = client.policies.list(resource_type="products")

# Activate / deactivate
client.policies.deactivate(policy.id)
client.policies.activate(policy.id)

# Hard delete
client.policies.delete(policy.id, hard=True)
```

## Use Case 2: Asset Access (Runtime)

Resolve download URLs for private/restricted assets to serve them to users.

```python
from saas_media_library import MediaLibraryClient

client = MediaLibraryClient(
    base_url="https://api.media-library.fritill.ae",
    tenant_id="my-tenant",
    token="user-jwt-token",
)

# Get a single asset with its download URL
asset = client.assets.get("asset-uuid")
print(asset.download_url)  # presigned S3 URL or volume token URL

# Batch resolve (up to 100 at once)
results = client.assets.resolve(["uuid-1", "uuid-2", "uuid-3"])
for r in results:
    if r.status == "ok":
        print(f"{r.asset.id} -> {r.asset.download_url}")

# List assets
page = client.assets.list(kind="video", status="ready", page=1, page_size=20)
for asset in page.items:
    print(f"{asset.original_filename}: {asset.status}")

# Delivery endpoints (slim response for client-facing apps)
delivery_asset = client.assets.delivery_get("asset-uuid")
delivery_page = client.assets.delivery_list(kind="image")

# Delete an asset
client.assets.delete("asset-uuid")
```

## Context Manager

```python
with MediaLibraryClient(
    base_url="https://api.media-library.fritill.ae",
    tenant_id="my-tenant",
    token="jwt",
) as client:
    client.policies.sync_policies([...])
```

## Error Handling

```python
from saas_media_library import MediaLibraryClient, NotFoundError, AuthenticationError

client = MediaLibraryClient(...)

try:
    asset = client.assets.get("nonexistent-id")
except NotFoundError:
    print("Asset not found")
except AuthenticationError:
    print("Invalid or expired token")
```

All exceptions inherit from `MediaLibraryError` and include:
- `status_code` — HTTP status code
- `body` — Raw response body

## Init Container Example (Kubernetes)

```yaml
initContainers:
  - name: sync-media-policies
    image: python:3.12-slim
    command: ["python", "/scripts/sync_policies.py"]
    env:
      - name: MEDIA_LIBRARY_URL
        value: "http://media-library-api:8000"
      - name: TENANT_ID
        valueFrom:
          configMapKeyRef:
            name: app-config
            key: TENANT_ID
      - name: SERVICE_TOKEN
        valueFrom:
          secretKeyRef:
            name: app-secrets
            key: MEDIA_LIBRARY_TOKEN
```

```python
# sync_policies.py
import os
from saas_media_library import MediaLibraryClient, AssetKind, Visibility

client = MediaLibraryClient(
    base_url=os.environ["MEDIA_LIBRARY_URL"],
    tenant_id=os.environ["TENANT_ID"],
    token=os.environ["SERVICE_TOKEN"],
)

client.policies.sync_policies([
    {
        "resource_type": "courses",
        "collection_name": "cover",
        "kind": AssetKind.IMAGE,
        "visibility": Visibility.PUBLIC,
        "allowed_mime_types": {"image/jpeg", "image/png", "image/webp"},
        "max_size_bytes": 5_000_000,
        "step_options": {"image_derive": {"formats": ["webp"], "sizes": [256, 512]}},
    },
    # ... more policies
])

print("Media library policies synced.")
client.close()
```
