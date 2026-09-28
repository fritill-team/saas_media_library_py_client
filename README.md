# SaaS Media Library - Python Client

Python client for the SaaS Media Library API. The media library stores,
processes and serves files; your app owns the rules around them:

1. **Uploads**: upload rules (MIME types, sizes, permissions, processing) live
   in your code as `UploadProfile`s and are sent with each upload
   (`client.uploads`).
2. **Access**: your app decides who may open a `restricted` asset
   (`AccessGate`), then hands out links the media library resolved for its
   service token. Files are still served by the media library.
3. **Assets**: read, list and delete assets and change their visibility
   (`client.assets`, `client.delivery`).

Resource policies (`client.policies`) and access grants (`client.access`) are
deprecated; see [Migrating from resource policies and access
grants](#migrating-from-resource-policies-and-access-grants).

Requires Python 3.11+.

## Installation

```bash
pip install git+ssh://git@github.com/fritill-team/saas_media_library_py_client.git
# with the FastAPI routes for restricted assets:
pip install "saas-media-library[fastapi] @ git+ssh://git@github.com/fritill-team/saas_media_library_py_client.git"
```

Or add to `requirements.txt`:

```
saas-media-library[fastapi] @ git+ssh://git@github.com/fritill-team/saas_media_library_py_client.git
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

Use the tenant's **service token** on your backend. It is required to resolve
`restricted` assets and is the identity uploads are created with.

## Uploads

### Upload profiles

An `UploadProfile` describes one kind of upload in your app: what it accepts
and how the media library processes it. Define profiles once, in code, next to
the feature that uses them. They are immutable and reject unknown fields, so a
typo such as `max_size_byte=` fails at import time.

```python
from saas_media_library import AssetKind, ProcessingConfig, UploadProfile, Visibility

COURSE_COVER = UploadProfile(
    name="course-cover",
    kind=AssetKind.IMAGE,
    visibility=Visibility.PUBLIC,
    allowed_mime_types={"image/jpeg", "image/png", "image/webp"},
    max_size_bytes=5_000_000,
    required_permissions={"courses:write"},
    # Pre-configure step_options so the pipeline runs fully automatically.
    # Without options, configurable steps pause for user input.
    processing_config=ProcessingConfig(
        step_options={"image_derive": {"formats": ["webp"], "sizes": [256, 512, 1024]}},
    ),
)

COURSE_VIDEO = UploadProfile(
    name="course-video",
    kind=AssetKind.VIDEO,
    visibility=Visibility.RESTRICTED,
    allowed_mime_types={"video/mp4", "video/quicktime"},
    max_size_bytes=2_000_000_000,
    processing_config=ProcessingConfig(
        step_options={
            "video_transcode": {"crf": 23, "preset": "fast"},
            "video_adaptive_stream": {"formats": ["hls"], "resolutions": [360, 720, 1080]},
            "video_thumbnail": {"timestamp": 1.0},
        },
        enable_transcription=True,
    ),
)
```

| Field | Default | Meaning |
|-------|---------|---------|
| `kind` | required | Asset kind; selects the processing pipeline |
| `visibility` | `PRIVATE` | `PUBLIC` (CDN), `PRIVATE` (anyone who knows the tenant and the asset id, see [Visibility](#visibility)) or `RESTRICTED` (your app decides, see [Restricted assets](#restricted-assets)) |
| `allowed_mime_types` | `None` (any) | `None` or `{"*/*"}` accepts any type; an empty set accepts none. Entries must be `type/subtype`; `"*/*"` is the only wildcard |
| `min_size_bytes` / `max_size_bytes` | `None` | Inclusive bounds. When set, the file size must be known |
| `required_permissions` / `allow_anonymous` | empty / `False` | Checked by `check_permissions()` |
| `allow_multiple` | `False` | Informational, for your forms |
| `processing_config` | `None` | `None` uses the server defaults: no step options, transcription off |

`enable_transcription` defaults to **off** for uploads: a `ProcessingConfig`
that does not set it is sent with `enableTranscription: false`. Set it to
`True` explicitly where you want subtitles (the service-wide
`ENABLE_TRANSCRIPTION` flag must also be on).

The media library no longer checks MIME types, sizes or permissions for these
uploads; the profile does, in your process:

```python
from saas_media_library import UploadRejected

if not COURSE_COVER.check_permissions(user.permissions, authenticated=user.is_authenticated):
    raise PermissionError

try:
    COURSE_COVER.validate_file("image/png", size_bytes=1_234_567)
except UploadRejected as exc:
    exc.reason  # "mime_type" | "too_large" | "too_small" | "size_required" | "kind"
```

MIME types are normalized before they are checked and before they are sent:
parameters and surrounding whitespace are dropped and the type is lowercased
(`" Image/PNG; charset=binary"` is checked and uploaded as `"image/png"`), so
the media library's processors recognize the file. A value that is not
`type/subtype` is rejected with `reason="mime_type"`, whatever the profile
allows.

`UploadRejected` is a `ValidationError`, so one `except ValidationError`
handles both the profile's rules and the server's 422 responses.

### Who may upload

Uploads without a resource policy (all of `client.uploads`) are accepted
only from a **trusted caller**: the tenant's service token, or a user of a
tenant on `token_exchange` (whose JWT the media library verifies). This holds
for every call on the session: create, get, parts, finalize and abort. Other
user tokens are only decoded, not verified, so the server answers 403
(`AuthenticationError`), and anonymous callers too. The server's
`POLICYLESS_UPLOADS_ALLOW_UNVERIFIED_USERS=true` (default `false`) relaxes
this to any authenticated caller; it is an escape hatch, not the intended
setup. Create the backend client with the service token.

### Uploading from the backend

```python
result = client.uploads.upload_file("cover.png", profile=COURSE_COVER)
result.asset_id  # the new asset

# bytes or file objects work too
client.uploads.upload_file(request_file.stream, profile=COURSE_COVER,
                           filename="cover.png", collection_name="cover")
```

`upload_file` validates the file against the profile, creates the session,
uploads the parts with their checksums and finalizes with the whole-file
SHA-256, reading the file once. If anything fails it aborts the session and
re-raises. `collection_name` is only a label stored on the session.
Non-seekable streams, and paths that are not regular files (FIFOs,
`/dev/stdin`, `/proc` files), are first copied to a temporary file to learn
their size.

### Browser uploads (backend-created sessions)

Let your backend create the session, with the profile's checks and its
service token, and let the browser send the bytes:

```python
# POST /api/uploads  (your backend)
def start_upload(user, filename, mime_type, size_bytes):
    if not COURSE_VIDEO.check_permissions(user.permissions):
        raise PermissionError
    session = client.uploads.create_session(
        profile=COURSE_VIDEO,           # kind, visibility, processing_config
        mime_type=mime_type,
        expected_size_bytes=size_bytes,  # validated against the profile
        original_filename=filename,
    )
    return {"sessionId": str(session.id), "partSizeBytes": session.part_size_bytes}
```

The browser then uploads parts to `PUT /v1/manage/uploads/sessions/{id}/parts/{n}`
and calls `.../finalize` with its own tenant login. That works only when the
browser's login is trusted (see [Who may upload](#who-may-upload)): the
tenant is on `token_exchange`, or the server runs with
`POLICYLESS_UPLOADS_ALLOW_UNVERIFIED_USERS=true`. Otherwise the parts and
finalize calls get 403, and your backend has to make them with the service
token instead (e.g. relay the parts, or use `upload_file`). Once your backend
learns the asset id, check what actually arrived:

```python
COURSE_VIDEO.verify_asset(client.assets.get(asset_id))  # raises UploadRejected
```

The lower-level calls map 1:1 to the endpoints: `create_session`,
`get_session`, `upload_part`, `finalize`, `abort`.

### Processing options

Metadata for building upload settings (kinds, visibilities, known MIME types
per kind, each kind's pipeline and step options, the server's upload
defaults):

```python
schema = client.processing.get_schema()
schema["mimeTypesByKind"], schema["pipelinesByKind"], schema["uploadDefaults"]
pipelines = client.processing.get_pipelines()
```

Both fall back to the legacy resource-policy routes on older servers.
`get_pipelines()` returns the same payload either way. `get_schema()` converts
the legacy payload to the new camelCase shape, but it only has `kinds`,
`visibilities`, `mimeTypesByKind` and `pipelinesByKind` (with
`configurableSteps`): `autoStartSteps`, `gatedSteps`, `transcriptionAvailable`
and `uploadDefaults` cannot be derived from it and are absent, so read them
with `.get()` if you may talk to an older server.

## Assets

Read, list and delete assets and manage visibility (`/manage`). Download
links come from [`client.delivery`](#delivery).

```python
# Get a single asset (admin details: status, size, checksum, renditions)
asset = client.assets.get("asset-uuid")
print(asset.status, asset.size_bytes)

# Its download link
print(client.delivery.get("asset-uuid").download_url)  # presigned S3 URL or volume token URL

# Batch resolve links
for r in client.delivery.resolve(["uuid-1", "uuid-2", "uuid-3"]):
    if r.status == "ok":
        print(f"{r.asset_id} -> {r.asset.download_url}")

# List assets
page = client.assets.list(kind="video", status="ready", page=1, page_size=20)
for asset in page.items:
    print(f"{asset.original_filename}: {asset.status}")

# Delete an asset
client.assets.delete("asset-uuid")
```

### Visibility

| Visibility | Who gets a link |
|------------|-----------------|
| `public` | Anyone: CDN link, no token |
| `private` | Anyone who knows the tenant and the asset id: `/delivery` resolves it without sign-in |
| `restricted` | Only a service token; your app decides which users receive the link |

`private` is not an access check: `/delivery` requires no sign-in, so any
caller that sends the tenant (`X-Tenant`) and an asset id gets the link, and
`GET /delivery/assets` lists the tenant's assets. Use `restricted` for files
only some users may open.

```python
from saas_media_library import Visibility

client.assets.update_visibility("asset-uuid", Visibility.RESTRICTED)

result = client.assets.bulk_update_visibility(["uuid-1", "uuid-2"], Visibility.PRIVATE)
print(result.updated, result.failed)
```

Any visibility can be changed to any other, with one limit: once the server
turns on `RESTRICTED_ASSETS_SERVICE_ONLY` or
`MANAGE_RESTRICTED_PREVIEWS_PRIVILEGED_ONLY` (both off by default; see step 5
of [Access grants → `can_access`](#access-grants--can_access)), only a
privileged caller may move a `restricted` asset to another visibility: a
service token, or a user of a `token_exchange` tenant with one of the
server's `MANAGE_PRIVILEGED_ROLES`. Anyone else gets 403: `update_visibility`
raises `AuthenticationError` (status 403) and `bulk_update_visibility` counts
the asset in `failed` with the error `forbidden`. Service-token clients are
always privileged. Making an asset `restricted` is not limited.

Moving to or from `public` also moves the asset's S3-stored files between the
private and public buckets (videos, audio and web apps stay on the volume), so
making an asset `public` publishes it on the CDN. If that move fails,
`update_visibility` raises `MediaLibraryError` (status 400) and
`bulk_update_visibility` counts the asset in `failed`.

### Delivery

Slim, client-facing responses: download URL, renditions and their preview
links.

```python
asset = client.delivery.get("asset-uuid")
page = client.delivery.list(kind="image", page_size=50)

items = client.delivery.resolve(asset_ids)  # any number of ids, batched by 100
for item in items:
    item.status  # "ok" | "not_found" | "forbidden" | "error" (ResolveStatus)

# Through a cache (service-token clients only): hits are served from it,
# only misses are requested
items = service_client.delivery.resolve(asset_ids, cache=url_cache)  # still one per id, in order
```

With `cache=` (an [`AssetUrlCache`](#asseturlcache)) the results come one per
input id in order, and ids with a live entry for the client's tenant are not
requested again. Entries are keyed by tenant and asset id only, **not by who
asked**: a hit is returned without any request, so the media library never
checks the caller. Use a cache only with clients that hold the tenant's
**service token**, never share one with clients using user tokens, and
`clear()` it if you `set_token()` to a different principal. Otherwise a user
client would be served what the service token got, restricted links
included, where the media library would have answered `forbidden`.

`public` and `private` assets can keep being resolved directly by your
frontend on `/delivery` (see [Visibility](#visibility): no sign-in is needed
for either). `client.assets.delivery_get/delivery_list/delivery_resolve` still
work and call `client.delivery`.

## Restricted assets

For `restricted` assets your backend decides who may open them, from its own
data (enrollments, purchases, roles), and hands out links resolved with its
service token. Links are bound to the file, not to the user, so one cached
link per asset serves every entitled user until shortly before it expires.

```
Browser ──▶ your backend ── can_access(user, asset_ids)?  (your data)
                  └─ service token ─▶ POST /delivery/assets/resolve ─▶ links
Browser ◀── links ┘
Browser ──▶ media library delivery (CDN / nginx / S3) ─▶ file
```

### Recommended: links in your own responses

Authorize in the endpoint that serves the page, with the rules you already
have, and embed the links in its response:

1. **Store asset ids only** in your data (e.g. `Lesson.video_asset_id`), never
   links: links expire.
2. **Decide access in your endpoint**, as you do for the rest of the page.
3. **Embed links** resolved with `client.delivery.resolve(ids, cache=...)` for
   the assets the user may open. For the others, embed only
   `thumbnail_url(result)` as a locked teaser: never the result itself.

```python
from saas_media_library import AssetUrlCache, MediaLibraryClient, thumbnail_url

# The tenant's service token: only it gets links for restricted assets.
media = MediaLibraryClient(base_url=..., tenant_id=..., token=SERVICE_TOKEN)
url_cache = AssetUrlCache()  # one per process, shared by every user

def course_page(user, course):
    enrolled = is_enrolled(user, course)  # your rules
    results = media.delivery.resolve([l.video_asset_id for l in course.lessons], cache=url_cache)
    return {"lessons": [
        {
            "title": lesson.title,
            "video": (result.asset.model_dump(mode="json", by_alias=True)
                      if enrolled and result.status == "ok" else None),
            "thumbnailUrl": thumbnail_url(result),
        }
        for lesson, result in zip(course.lessons, results)
    ]}
```

`thumbnail_url(result)` is the asset's thumbnail rendition link (or the
result's `thumbnailUrl`), never a download or other preview link; it is what
`AccessGate` gives denied users.

Use `AccessGate` (below) when a frontend resolves arbitrary asset ids itself,
e.g. a player that only knows ids: it answers `/delivery`-shaped requests after
running your `can_access` over every id.

### AccessGate

```python
from saas_media_library import AccessGate, AssetUrlCache, MediaLibraryClient

# Must use the tenant's service token: only it gets links for restricted assets.
media = MediaLibraryClient(base_url=..., tenant_id=..., token=SERVICE_TOKEN)

def can_access(user, asset_ids):
    """Return the ids (of asset_ids) that user may open, in one query."""
    return db.scalars(
        select(Lesson.asset_id)
        .join(Enrollment, Enrollment.course_id == Lesson.course_id)
        .where(Enrollment.user_id == user.id, Lesson.asset_id.in_(asset_ids))
    )

gate = AccessGate(media, can_access, cache=AssetUrlCache())

results = gate.resolve(current_user, asset_ids)   # one result per id, in order
result = gate.resolve_one(current_user, asset_id)
body = gate.to_payload(results)  # exactly the JSON of POST /delivery/assets/resolve
```

- Allowed ids get the media library's result (`ok`, `not_found`, `error`).
- Denied ids come back `forbidden` with `asset: null` and only the asset's
  public `thumbnailUrl`, never a download or other preview link. An asset that
  does not exist is still `not_found`, as on the server.
- `can_access` is called once per request with every id, so it can be one
  database query. It must return an iterable of the allowed ids (not a bool),
  or a mapping `{asset_id: allowed}`, where only truthy values allow.

### AssetUrlCache

```python
cache = AssetUrlCache(
    safety_margin=30,   # seconds before the shortest link expiry to drop the entry
    default_ttl=300,    # for links without expiry (CDN)
    max_entries=10_000, # optional, least recently used evicted first
)
```

An entry lives until the shortest of its links (download URL, rendition and
variant previews) expires, minus the safety margin. Only `ok` results for
`ready` assets are cached: an asset still being processed is asked again every
time, so its renditions show up as soon as they exist. Cached results report
the time their links have left in `downloadUrlExpiresIn` /
`previewUrlExpiresIn`. Expired entries are dropped as new ones are stored, so
without `max_entries` the cache grows with its live entries only.

Entries are kept per tenant: `AccessGate` and `client.delivery.resolve(...,
cache=)` store and look up under their client's tenant, so one cache can be
shared by clients of several tenants without one tenant's links reaching
another. When calling `get`/`put` directly, pass `tenant_id=`.

Entries are not kept per principal: fill and read a cache only through
clients using a tenant's service token (as `AccessGate` requires). A client
with a user token must not use it, see [Delivery](#delivery).

The cache is thread-safe and per process; call `cache.invalidate(asset_id)`
after changing an asset (it drops the asset for every tenant; pass
`tenant_id=` to drop only one).

### FastAPI routes

```python
from fastapi import FastAPI
from saas_media_library.integrations.fastapi import create_media_router

app = FastAPI()
app.include_router(create_media_router(gate, get_current_user, prefix="/media"))
```

| Route | Same as | Responses |
|-------|---------|-----------|
| `POST /media/resolve` `{"assetIds": [...]}` (1 to 100) | `POST /delivery/assets/resolve` | 200 with `{items, total}` |
| `GET /media/{asset_id}` | `GET /delivery/assets/{id}` | 200 asset; 403 `{detail, thumbnailUrl}`; 404; 502 |

`get_current_user` is your app's FastAPI dependency; what it returns is passed
to `can_access`. Failures talking to the media library (error responses,
connection errors, timeouts, malformed responses) answer 502 with
`{"detail": "Media library request failed"}`, so an expired service token is
not mistaken for the user's own 401; the cause is logged. The gate raises
all of these as `MediaLibraryError` (transport and decoding errors with the
original as `__cause__`), and the routes catch only that. Exceptions from your
own code (`can_access`, `get_current_user`) are never turned into a 502,
whatever their type, even `httpx` or `pydantic` errors: they stay 500 (or go
to your own exception handlers). The frontend only changes its base URL for
restricted assets.

## Migrating from resource policies and access grants

Both are being retired on the server (`docs/resource-policy/retirement.md`,
`docs/asset-access/retirement.md`). During the migration window old and new
calls work side by side, and the server counts deprecated usage per tenant.

### Resource policies → upload profiles

1. **Generate profiles** from the tenant's current policies:

   ```bash
   export MEDIA_LIBRARY_TOKEN=...   # or pass --token
   python -m saas_media_library.migrate_policies \
       --base-url https://api.media-library.fritill.ae \
       --tenant my-tenant > app/media_profiles.py
   ```

   This writes a module defining `UPLOAD_PROFILES`, keyed
   `"resource_type/collection_name"` (or `"resource_type"`), with every policy
   rule carried over, including `enable_transcription` as the policy had it.
   Only active policies are converted unless you pass `--include-inactive`.
   Review it and commit it. From code:

   ```python
   from saas_media_library import profiles_from_policies, render_profiles_module

   profiles = profiles_from_policies(client.policies.list(is_active=True))
   source = render_profiles_module(profiles)
   ```

2. **Upload with profiles.** Replace uploads that sent `resource_type` with
   `client.uploads.upload_file(..., profile=UPLOAD_PROFILES["courses/cover"])`
   or `create_session(profile=...)` (see [Uploads](#uploads)). Code that has
   the old pair at hand looks the profile up with
   `find_profile(UPLOAD_PROFILES, resource_type, collection_name)`, which
   resolves like the server did: the exact `"resource_type/collection_name"`
   profile, else the resource type's default (`"resource_type"`), else `None`
   (no policy applied to that pair). A plain
   `UPLOAD_PROFILES[policy_key(...)]` misses pairs the server served with the
   default policy. Pass `collection_name=` if you still want it recorded on
   the session.
3. **Enforce the rules in your app.** The server no longer checks MIME types,
   sizes or permissions for policy-less uploads: use `check_permissions()`
   and let `create_session`/`upload_file` validate, and `verify_asset()` for
   browser uploads.
4. **Remove the `sync_policies` init containers** (and their scripts, secrets
   and config) once no upload sends `resource_type`. Profiles ship with your
   code, so there is nothing to sync at startup.
5. **Use `client.processing`** instead of `client.policies.get_schema()` /
   `get_available_steps()`.
6. **Leave the policies in place** until the server removes them: assets
   uploaded through a policy keep reading its processing config (e.g. when
   reprocessed) until then. On a shared tenant, never prune other services'
   policies (`delete_stale`).

### Access grants → `can_access`

1. **Write `can_access(user, asset_ids)`** from your own data and create an
   `AccessGate` with the tenant's service token.
2. **Add the media endpoint**: `create_media_router(...)`, or call
   `gate.resolve()` / `gate.to_payload()` from your own framework.
3. **Point the frontend** at your endpoint instead of `/delivery` for
   `restricted` assets, or embed the links in your own responses (see
   [Recommended](#recommended-links-in-your-own-responses)). `public` and
   `private` assets can stay on `/delivery`.
4. **Delete `grant` / `revoke` / `bulk_grant` / `bulk_revoke` calls** and any
   enrollment-sync jobs that fed them.
5. Once the server's `asset_access_grant_api` and `restricted_user_resolve`
   usage counters read zero, the media library switches to
   `RESTRICTED_ASSETS_SERVICE_ONLY=true` and ignores grants.

## Deprecated: `client.policies` and `client.access`

Every method of `client.policies` (`ResourcePoliciesAPI`) and `client.access`
(`AssetAccessAPI`) emits a `DeprecationWarning` pointing at your call site and
naming its replacement:

| Deprecated | Replacement |
|------------|-------------|
| `client.policies.upsert/get/list/update/delete/...`, `sync_policies` | `UploadProfile` + `client.uploads` |
| `client.policies.get_schema()` | `client.processing.get_schema()`, in a different shape: camelCase keys (`mimeTypesByKind`, `pipelinesByKind`, `configurableSteps`), upload defaults instead of policy defaults, no `permission_schema` |
| `client.policies.get_available_steps()` | `client.processing.get_pipelines()` (it now calls this) |
| `client.access.grant/revoke/list/bulk_grant/bulk_revoke` | `AccessGate(client, can_access)` |

They keep working until the server removes the endpoints; then they will be
removed from this client. Python hides `DeprecationWarning` outside `__main__`
by default; run tests with `-W error::DeprecationWarning` to find remaining
calls.

## Context Manager

```python
with MediaLibraryClient(
    base_url="https://api.media-library.fritill.ae",
    tenant_id="my-tenant",
    token="jwt",
) as client:
    client.uploads.upload_file("cover.png", profile=COURSE_COVER)
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

All exceptions inherit from `MediaLibraryError` (and survive pickling, e.g.
across process pools) and include:
- `status_code` — HTTP status code (None for `UploadRejected`, raised before any request, and for `AccessGate` transport or decoding failures)
- `body` — Raw response body

| Exception | When |
|-----------|------|
| `AuthenticationError` | 401 / 403 |
| `NotFoundError` | 404 |
| `ConflictError` | 409, e.g. an upload checksum mismatch |
| `ValidationError` | 422 |
| `UploadRejected` | A file failed an `UploadProfile` check, or its MIME type is not `type/subtype` (subclass of `ValidationError`) |
| `ServerError` | 5xx |
| `MediaLibraryError` | Other 4xx; from `AccessGate`, also an unreachable media library or a malformed response (original exception in `__cause__`) |
