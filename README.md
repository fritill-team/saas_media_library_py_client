# pkg_media: media library client and gRPC contract

`pkg_media` (distribution `pkg-media`, formerly `saas-media-library`) is the
Python client for the ITQ media library, and the home of its gRPC contract
(`fritill.media.v1`: protos, committed stubs, servicer base classes, wire
paths and error reasons). The
media library stores, processes and serves your files. Your backend keeps the
rules: what may be uploaded, who may open what, and which browser may do
what. This client gives you the pieces for each: upload profiles, delivery
links with a cache, an access gate for restricted content, and short-lived
media grants for browsers.

Requires Python 3.11 or later. There is no async client.

Links to `docs/…` point to the media library server repository
(`fritill-team/saas_media_library`, private: ask for access).

## Contents

- [Installation](#installation)
- [Concepts](#concepts)
- [Quick start](#quick-start)
- [Configure the client](#configure-the-client)
- [Upload files](#upload-files)
- [Processing options](#processing-options)
- [Manage assets](#manage-assets)
- [Get delivery links](#get-delivery-links)
- [Protect restricted content](#protect-restricted-content)
- [Give browsers access with media grants](#give-browsers-access-with-media-grants)
- [FastAPI integration](#fastapi-integration)
- [Handle errors](#handle-errors)
- [Test your integration](#test-your-integration)
- [Models reference](#models-reference)
- [API reference](#api-reference)
- [Upgrading and server compatibility](#upgrading-and-server-compatibility)
- [Server compatibility](#server-compatibility)
- [Troubleshooting](#troubleshooting)
- [Versioning and releases](#versioning-and-releases)

## Installation

The package is installed from GitHub.

```bash
pip install "pkg-media @ git+https://github.com/fritill-team/saas_media_library_py_client.git"
```

With the optional FastAPI routers:

```bash
pip install "pkg-media[fastapi] @ git+https://github.com/fritill-team/saas_media_library_py_client.git"
```

With uv:

```bash
uv add "pkg-media[fastapi] @ git+https://github.com/fritill-team/saas_media_library_py_client.git"
```

Pin a release in production. Every release is a tag named
`pkg_media-v<version>`:

```bash
pip install "pkg-media @ git+https://github.com/fritill-team/saas_media_library_py_client.git@pkg_media-v0.20.0"
```

Releases before v0.26 were published as `saas-media-library` with tags
`saas_media_library-v<version>`; from v0.26 the distribution is `pkg-media`,
imported as `pkg_media`, with tags `pkg_media-v<version>`. See
[From v0.26: `saas_media_library` is now `pkg_media`](#from-v026-saas_media_library-is-now-pkg_media).

Dependencies: `httpx` (≥ 0.25, < 1.0), `pydantic` 2, `protobuf` (≥ 7.35.1,
< 8) and `grpcio` (≥ 1.84). The `fastapi` extra adds `fastapi` (≥ 0.100).
No code generator is installed: the gRPC stubs ship generated.

## Concepts

```
                 service token (ml_live_…)
Your backend ─────────────────────────────▶ Media library  ── /v1/manage/…   (uploads, assets, grants)
     │  ▲                                        ▲     │    ── /v1/delivery/… (links)
     │  │ your API                               │     │
     ▼  │                                        │     ▼
  Browser ──── media grant (mlg_…) ──────────────┘   delivery gate (R2) ──▶ file bytes
```

### Tenants and `X-Tenant`

Every asset belongs to one tenant. The client sends the tenant on every
request in the `X-Tenant` header, from the `tenant_id` you pass to
`MediaLibraryClient`. The server looks the tenant up **by its slug** (for
example `acme`), so pass the slug, not the UUID. Ids of assets, sessions and
grants are only valid within their tenant.

### Who is calling

The media library recognises three kinds of caller.

| Caller | Looks like | Where it lives | What it may do |
|---|---|---|---|
| **Service token** | `ml_live_…` / `ml_test_…` | Your backend only. Never sent to a browser | Everything in its tenant: uploads without a resource policy, links for `restricted` assets, issuing media grants |
| **User login** | A Keycloak JWT | The user's browser or app | Depends on the server settings below. Not trusted by default |
| **Media grant** | `mlg_…` | Issued by your backend for one browser user | Only its scopes, assets and upload sessions, for a few minutes |

**Service tokens** are created by the tenant's owner with the media
library's tenant-management API
(`POST /tenant-management/tenants/{tenant_id}/service-tokens`, owner login
required). The secret is shown once; store it in your backend's secret store
(a Kubernetes secret, Vault…). Rotating it invalidates the old secret
immediately.

**User logins** are only trusted when the server verifies them: for tenants
whose `auth_mode` is `token_exchange`, or when the server runs with
`JWT_VERIFICATION=log` or `enforce`. Otherwise the JWT is decoded without a
signature check, so anyone can claim any user and any role. See the server's
[`docs/security/token-verification.md`](https://github.com/fritill-team/saas_media_library/blob/dev/docs/security/token-verification.md).
Never grant anything based on an unverified login: use your own session and
your own rules, then hand out links or grants.

**Media grants** are short-lived, scoped bearer tokens signed by the media
library. Your backend asks for one with its service token after its own
check, and the browser uses it on `/manage` and `/delivery`. See
[Give browsers access with media grants](#give-browsers-access-with-media-grants).

### Visibility

Each asset has one of three visibility tiers.

| Visibility | On `/delivery` today (`DELIVERY_AUTH=off`, the default) | With `DELIVERY_AUTH=enforce` |
|---|---|---|
| `public` | Anyone. Gate link without a token, cached at the edge | Unchanged |
| `private` | Anyone who sends the tenant and knows the asset id. Not an access check | Only a service token, a verified admin, or a grant listing the asset |
| `restricted` | A service token, or a media grant listing the asset. Your app decides which users get the link. (Users holding a deprecated per-user access grant still do too, until the server sets `RESTRICTED_ASSETS_SERVICE_ONLY`) | Unchanged |

`GET /delivery/assets` (the tenant's asset list) is also open today and
needs a service token, an admin or a `manage:*` grant under
`DELIVERY_AUTH=enforce`. Use `restricted` for anything only some users may
open. See [`docs/security/media-grants.md`](https://github.com/fritill-team/saas_media_library/blob/dev/docs/security/media-grants.md).

### Assets, blobs, renditions and variants

- An **asset** is one uploaded file plus its metadata: kind, MIME type,
  visibility, status.
- A **blob** is the stored bytes. Blobs are content-addressed and
  deduplicated within a tenant, so identical uploads share one blob.
- A **rendition** is a derived output of processing, such as a thumbnail,
  resized images, an HLS stream or subtitles. It has a `rendition_type` and
  an `entrypoint`.
- A **variant** is one version inside a rendition, such as one resolution of
  an adaptive stream or one size of a thumbnail.

### Processing

After an upload is finalized, the asset runs through the fixed pipeline of
its kind, for example `virus_scan → image_derive` for images and
`virus_scan → video_package → video_thumbnail → video_transcription` for
videos. Steps that take options read them from the upload's
`ProcessingConfig.step_options`. A configurable step without options runs
with its defaults; no step waits for input. The asset's `status` moves from
`processing` to `ready` (or `failed`). See
[Processing options](#processing-options) for the exact steps and options.

## Quick start

```python
import os

from pkg_media import AssetKind, MediaLibraryClient, UploadProfile, Visibility

AVATAR = UploadProfile(
    name="avatar",
    kind=AssetKind.IMAGE,
    visibility=Visibility.PUBLIC,
    allowed_mime_types={"image/png", "image/jpeg"},
    max_size_bytes=2_000_000,
)

with MediaLibraryClient(
    base_url="https://media.example.com",     # API root, without /v1
    tenant_id="acme",                         # the tenant's slug
    token=os.environ["MEDIA_LIBRARY_TOKEN"],  # the tenant's service token
) as client:
    uploaded = client.uploads.upload_file("avatar.png", profile=AVATAR)
    [item] = client.delivery.resolve([uploaded.asset_id])
    print(item.status, item.asset.download_url)
```

`upload_file` checks the file against the profile, uploads it in parts and
returns once the media library has the file. Processing then runs in the
background.

## Configure the client

### Constructor

```python
import os

from pkg_media import MediaLibraryClient

client = MediaLibraryClient(
    base_url="https://media.example.com",
    tenant_id="acme",
    token=os.environ["MEDIA_LIBRARY_TOKEN"],
    timeout=30.0,
)
```

| Argument | Default | Meaning |
|---|---|---|
| `base_url` | required | The API root, **without** `/v1`: the client's paths start with `/v1/…`. A trailing `/` is removed |
| `tenant_id` | required | Sent as `X-Tenant`. The server resolves it by the tenant's **slug** |
| `token` | `None` | Sent as `Authorization: Bearer <token>`. Use the tenant's service token on a backend |
| `timeout` | `30.0` | Seconds, for the client's own `httpx.Client` |
| `http_client` | `None` | An `httpx.Client` to use instead. See below |

`client.tenant_id` returns the tenant it was created with.

The client has one attribute per API:

| Attribute | Class | For |
|---|---|---|
| `client.uploads` | `UploadsAPI` | Upload sessions and `upload_file` |
| `client.processing` | `ProcessingOptionsAPI` | Kinds, pipelines and step options |
| `client.assets` | `AssetsAPI` | Manage assets: get, list, delete, visibility |
| `client.delivery` | `DeliveryAPI` | Download and preview links |
| `client.grants` | `GrantsAPI` | Media grants for browsers |
| `client.permalinks` | `PermalinksAPI` | Stable public addresses for public assets |
| `client.policies` | `ResourcePoliciesAPI` | Deprecated: resource policies |
| `client.access` | `AssetAccessAPI` | Deprecated: per-user access grants |

### Use your own `httpx.Client`

Pass `http_client` for proxies, retries, custom transports or connection
limits. It is used **as is**: the client does not add `base_url`, `X-Tenant`,
`Authorization` or the timeout to it, so set them yourself.

```python
import httpx

http = httpx.Client(
    base_url="https://media.example.com",
    headers={
        "X-Tenant": "acme",
        "Authorization": f"Bearer {os.environ['MEDIA_LIBRARY_TOKEN']}",
    },
    timeout=httpx.Timeout(30.0, connect=5.0),
    transport=httpx.HTTPTransport(retries=3),  # retries failed connections only
)
custom_client = MediaLibraryClient(base_url="https://media.example.com", tenant_id="acme", http_client=http)
```

`close()` does not close a client you passed in; close it yourself.

### Change the token

`set_token` replaces the `Authorization` header for every later request, on
whichever `httpx.Client` the client uses:

```python
client.set_token(os.environ["MEDIA_LIBRARY_TOKEN"])
```

The header is shared by every thread using the client. If the new token
belongs to a different principal, `clear()` any
[`AssetUrlCache`](#cache-links-with-asseturlcache) it filled.

### Close the client

Use the client as a context manager, or call `close()` when done:

```python
with MediaLibraryClient(base_url="https://media.example.com", tenant_id="acme",
                        token=os.environ["MEDIA_LIBRARY_TOKEN"]) as short_lived:
    short_lived.processing.get_pipelines()

custom_client.close()
http.close()
```

Create one client per process and tenant, and reuse it: it keeps its
connections open.

## Upload files

### Define an upload profile

An `UploadProfile` describes one kind of upload in your app: what it accepts
and how the media library processes it. The media library no longer checks
MIME types, sizes or permissions for these uploads; the profile does, in your
process. Define profiles once, next to the feature that uses them.

```python
from pkg_media import AssetKind, ProcessingConfig, UploadProfile, Visibility

COURSE_COVER = UploadProfile(
    name="course-cover",
    kind=AssetKind.IMAGE,
    visibility=Visibility.PUBLIC,
    allowed_mime_types={"image/jpeg", "image/png", "image/webp"},
    max_size_bytes=5_000_000,
    required_permissions={"courses:write"},
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
            "video_package": {"crf": 23, "preset": "fast", "resolutions": ["360p", "720p", "1080p"]},
            "video_thumbnail": {"timestamp": 1.0},
        },
        enable_transcription=True,
    ),
)
```

Profiles are immutable pydantic models and reject unknown fields, so a typo
such as `max_size_byte=` fails at import time.

| Field | Default | Meaning |
|---|---|---|
| `kind` | required | `AssetKind`. Selects the processing pipeline |
| `visibility` | `Visibility.PRIVATE` | The new asset's visibility. See [Visibility](#visibility) |
| `allowed_mime_types` | `None` | `None` or a set containing `"*/*"` accepts any type. An empty set accepts none. Entries must be `type/subtype` |
| `min_size_bytes` | `None` | Inclusive minimum. When set, the file size must be known |
| `max_size_bytes` | `None` | Inclusive maximum. When set, the file size must be known |
| `allow_multiple` | `False` | Informational, for your forms. Nothing enforces it |
| `required_permissions` | `set()` | Permissions `check_permissions` requires |
| `allow_anonymous` | `False` | Lets `check_permissions` allow unauthenticated users |
| `processing_config` | `None` | `ProcessingConfig` sent with the upload. `None` uses the server defaults: no step options, transcription off |
| `name` | `None` | A label used in error messages and by `create_grant_router` |

Constructing a profile with `min_size_bytes` greater than `max_size_bytes`,
or a MIME entry that is not `type/subtype`, raises pydantic's
`ValidationError`.

#### MIME types

MIME types are normalised before they are compared and before they are sent:
parameters and surrounding whitespace are dropped and the type is lowercased.
`" Image/PNG; charset=binary"` is checked and uploaded as `"image/png"`, so
the media library's processors recognise the file. A file type that is not
`type/subtype` is always rejected.

`"*/*"` is the only wildcard on the client. An entry such as `"video/*"` is
compared literally and only matches a file whose type is exactly
`video/*`. (Grant profiles behave differently on the server, see
[`grant_profile_payload`](#grant_profile_payload-and-validate_file).)

#### Processing config

`ProcessingConfig` has three fields:

| Field | Default | Meaning |
|---|---|---|
| `step_options` | `None` | `{step: {option: value}}`. Step names must belong to the kind's pipeline, or the server answers 400. Steps left out run with their defaults |
| `use_filename_as_asset_id` | `False` | Use the uploaded filename as the asset id. An upload named after an existing asset's id replaces that asset: leave it off unless your backend controls filenames. `web_app` uploads always use the filename |
| `enable_transcription` | `False` | See below |

`enable_transcription` defaults to **off**, as on the server. A
`ProcessingConfig` that does not set it is sent with
`enableTranscription: false`. Set it to
`True` explicitly where you want subtitles. The server's
`ENABLE_TRANSCRIPTION` flag must also be on.

The option names and their allowed values come from the server: see
[Processing options](#processing-options).

### Check the user and the file

Check who may upload before you upload:

```python
from pkg_media import UploadRejected

if not COURSE_COVER.check_permissions(user.permissions, authenticated=user.is_authenticated):
    raise PermissionError("not allowed to upload course covers")

try:
    COURSE_COVER.validate_file("image/png", size_bytes=1_234_567)
except UploadRejected as exc:
    print(exc.reason)  # "mime_type" | "size_required" | "too_large" | "too_small" | "kind"
```

`check_permissions(permissions, *, authenticated=True)` applies these rules,
in order:

1. `allow_anonymous` → allowed.
2. No `required_permissions` → allowed, even when unauthenticated.
3. Unauthenticated → denied.
4. Allowed only if `permissions` contains every required permission. A single
   string counts as one permission.

`validate_file(mime_type, size_bytes=None)` raises `UploadRejected` when:

- the profile sets a size bound and `size_bytes` is `None` (`size_required`);
- the MIME type is not `type/subtype`, or not allowed (`mime_type`);
- the size is above `max_size_bytes` (`too_large`) or below `min_size_bytes`
  (`too_small`).

`verify_asset(asset)` checks an asset that was uploaded by a browser, after
the fact: kind (`kind`), MIME type, and size bounds when the asset carries a
size. `Asset` (from `client.assets.get`) has a size; `DeliveryAsset` does
not.

`UploadRejected` is a `ValidationError`, so one `except ValidationError`
handles both your profile's rules and the server's 422 responses.

### Who may upload

Uploads without a resource policy (all of `client.uploads`) are accepted only
from a **trusted caller**: the tenant's service token, a verified user (see
[Who is calling](#who-is-calling)), or a media grant with the `upload` scope.
This applies to every call on the session: create, get, parts, finalize and
abort. Anyone else gets 403 (`AuthenticationError`). The server setting
`POLICYLESS_UPLOADS_ALLOW_UNVERIFIED_USERS=true` relaxes this to any
authenticated caller; it is an escape hatch, not the intended setup.

Create the backend client with the service token.

### Upload a file

`upload_file` runs the whole flow:

```python
result = client.uploads.upload_file("cover.png", profile=COURSE_COVER)
print(result.asset_id)
```

The source may be a path, `bytes`, or a binary file object:

```python
with open("cover.png", "rb") as f:
    client.uploads.upload_file(f, profile=COURSE_COVER, collection_name="covers")

from pathlib import Path

png_bytes = Path("cover.png").read_bytes()
client.uploads.upload_file(png_bytes, profile=COURSE_COVER, filename="cover.png")
```

What it does:

1. Works out the size and the filename (from the path or the file object's
   `name`; pass `filename=` for bytes). The MIME type is guessed from the
   filename, falling back to `application/octet-stream`; pass `mime_type=` to
   set it.
2. Runs `profile.validate_file`. Nothing is sent if it fails.
3. Creates a session, uploads the parts with their SHA-256 checksums, and
   finalizes with the whole-file SHA-256 (unless `verify_checksum=False`).
   The file is read once.
4. On any failure after the session was created, aborts the session (best
   effort) and re-raises. After a failed finalize it only aborts when the
   session is confirmed not finalized, since aborting a finalized session
   deletes its blob.

Non-seekable streams, and paths that are not regular files with a known size
(FIFOs, `/dev/stdin`, `/proc` files), are first copied to a temporary file
to learn their size: in memory up to 8 MiB, on disk beyond.

`part_size_bytes` defaults to the part size the server returns. A smaller
value raises `ValueError` when it needs more parts than the session accepts.
`collection_name` is only a label stored on the session.

The returned `UploadFinalizeResult` carries `asset_id`. Processing runs
afterwards; poll `client.assets.get(asset_id).status` until it is `ready` if
you need the renditions.

### Low-level session API

The session methods map one-to-one to the endpoints:

```python
import hashlib

data = Path("cover.png").read_bytes()

session = client.uploads.create_session(
    profile=COURSE_COVER,
    mime_type="image/png",
    expected_size_bytes=len(data),
    original_filename="cover.png",
)
part = client.uploads.upload_part(session.id, 1, data, offset=0, total_size_bytes=len(data))
finalized = client.uploads.finalize(session.id, expected_sha256_hex=hashlib.sha256(data).hexdigest())
print(finalized.asset_id, finalized.checksum_verified)
```

- `create_session(*, mime_type, profile=None, kind=None, visibility=None,
  processing_config=None, original_filename=None, expected_size_bytes=None,
  expected_sha256_hex=None, collection_name=None, validate=True)`: the
  profile supplies `kind`, `visibility` and `processing_config`; explicit
  arguments override it. With a profile and `validate=True`, the file is
  checked first. Without a profile, `kind` is required (`TypeError`
  otherwise). The MIME type is always sent normalised.
- `upload_part(session_id, part_number, data, *, sha256_hex=None,
  offset=None, total_size_bytes=None)`: part numbers start at 1. The checksum
  is computed when not given. `offset` adds a `Content-Range` header.
  Sending a part again with the same checksum is idempotent.
- `finalize(session_id, *, expected_sha256_hex=None)`: with a checksum, a
  mismatch is a 409 (`ConflictError`).
- `get_session(session_id)`: state and progress.
- `abort(session_id)`: deletes the uploaded parts. Aborting a finalized
  session also deletes its blob.

A session's `state` is one of `initiated`, `in_progress`, `finalized`,
`failed`, `expired` or `aborted`. Sessions expire at `expires_at`.

### Resume an upload

`get_session` lists the parts the server already has, so an interrupted
upload continues where it stopped:

```python
video = bytes(12 * 1024 * 1024)  # stands in for a large file

session = client.uploads.create_session(
    profile=COURSE_VIDEO, mime_type="video/mp4",
    expected_size_bytes=len(video), original_filename="lesson.mp4",
)
size = session.part_size_bytes
client.uploads.upload_part(session.id, 1, video[:size], offset=0, total_size_bytes=len(video))
# … the process stops here, and later:

session = client.uploads.get_session(session.id)
for number in range(1, session.parts_expected + 1):
    if number in session.received_parts:
        continue
    start = (number - 1) * size
    client.uploads.upload_part(session.id, number, video[start:start + size],
                               offset=start, total_size_bytes=len(video))
client.uploads.finalize(session.id, expected_sha256_hex=hashlib.sha256(video).hexdigest())
```

Store the session id where you can find it again (your database, the
browser's storage). A session can be resumed until it expires.

### Uploads from the browser

Large files should go from the browser to the media library directly. Your
backend still decides; there are four ways to let the browser in.

**1. Backend creates the session, a grant covers it.** The backend checks the
user and the file, creates the session with its service token, and issues an
`upload` grant for that session only:

```python
from pkg_media import GrantScope

def start_video_upload(user, filename, mime_type, size_bytes):
    if not COURSE_VIDEO.check_permissions(user.permissions):
        raise PermissionError("not allowed")
    session = client.uploads.create_session(
        profile=COURSE_VIDEO,
        mime_type=mime_type,
        expected_size_bytes=size_bytes,  # checked against the profile
        original_filename=filename,
    )
    grant = client.grants.issue([GrantScope.UPLOAD], session_ids=[session.id], subject=str(user.id))
    return {"sessionId": str(session.id), "partSizeBytes": session.part_size_bytes,
            "grant": grant.grant, "expiresAt": grant.expires_at.isoformat()}

upload = start_video_upload(user, "lesson.mp4", "video/mp4", 50_000_000)
```

The browser then sends each part to
`PUT /v1/manage/uploads/sessions/{id}/parts/{n}` with
`Authorization: Bearer <grant>` and an `X-Part-SHA256` header, and finally
`POST …/finalize`.

**2. A grant with a profile.** The backend issues an `upload` grant carrying
the profile, and the browser creates the session itself
(`POST /v1/manage/uploads/sessions` with only `mime_type`,
`expected_size_bytes` and `original_filename`). The server takes kind,
visibility, collection and processing config from the grant's profile and
answers 403 if the browser sends a different `kind` or `visibility`, or any
processing config.

```python
grant = client.grants.issue(["upload"], profile=COURSE_COVER, subject=str(user.id), ttl_seconds=300)
```

**3. `create_grant_router` with `profiles=`.** The browser names one of your
profiles and your `authorize` callback decides. See
[Let the browser ask for grants](#let-the-browser-ask-for-grants).

**4. `create_upload_router`.** Way 1 packaged for FastAPI, with the attach
check included. Recommended when several services share one site: see
[Uploads started by your backend](#uploads-started-by-your-backend).

In every case the grant does not cover the asset the upload creates. The
browser sends the `sessionId` and the `assetId` from finalize to your
backend, which checks what arrived and stores the reference:

```python
finished = client.uploads.upload_file("cover.png", profile=COURSE_COVER)  # stands in for the browser's upload
asset = client.uploads.verify_upload(finished.session_id, finished.asset_id, profile=COURSE_COVER)
```

`verify_upload` raises `UploadRejected` unless the session is finalized, the
asset was created by that session (its `upload_session_id`), and the asset
has the profile's visibility and passes `profile.verify_asset`. Servers that
don't return `uploadSessionId` get the older check: the asset's blob is the
session's final blob. Blobs are deduplicated per tenant, so that one also
accepts another asset with the same bytes. `verify_upload` does not check
who started the session: `claim_upload` (below) does.

### Uploads started by your backend

When several services on the same tenant and the same site let users upload,
each service starts its own uploads and checks them when they are attached.
The browser talks to the media library directly, with a grant for one
session; the service never proxies the file.

1. The browser posts the file's metadata to the service
   (`POST /api/videos/uploads`). The service checks the file against a named
   `UploadProfile`, runs its own rule (`can_start`), creates the session with
   its service token, saves an `UploadRecord` (owner, profile name, context)
   and returns an `upload` grant for that session only.
2. The browser uploads the parts and finalizes with the media library. Before
   the grant expires, it asks the service for a new one.
3. The browser posts `{sessionId, assetId}` to the service's own attach
   endpoint. The service calls `claim_upload`, then stores the asset id.

The browser never chooses kind, visibility or processing (the service created
the session), the grant cannot create other sessions, and nobody can attach
another user's upload, or one upload twice.

```python
from pkg_media import InMemoryUploadStore, UploadRejected, claim_upload
from pkg_media.integrations.fastapi import create_upload_router

upload_store = InMemoryUploadStore()  # single process only: use your database in production

def get_current_user():
    """Your FastAPI auth dependency; see FastAPI integration for a full app."""
    return user

def can_start(user, request, profile):
    # request.context is what the browser sent, e.g. {"courseId": "…"}
    return profile.check_permissions(user.permissions)

upload_router = create_upload_router(
    client, can_start, get_current_user,
    profiles={"course-cover": COURSE_COVER},  # keyed by each profile's name
    store=upload_store,
    get_owner=lambda user: str(user.id),
    # upload_base_url: leave unset; the frontend chooses where parts go
    prefix="/courses/uploads",
)
```

The start request body (`StartUploadRequest`):

| Field | JSON key | Type | Notes |
|---|---|---|---|
| `mime_type` | `mimeType` | `str` | checked against the profile |
| `size_bytes` | `sizeBytes` | `int` | checked against the profile, sent as `expected_size_bytes` |
| `filename` | `filename` | `str` or `None` | sent as `original_filename` |
| `profile` | `profile` | name of one of `profiles` | optional when there is only one |
| `context` | `context` | `dict` | your own data, passed to `can_start` and kept in the record |

| Route | Response | When |
|---|---|---|
| `POST {prefix}` | 201 `{"sessionId", "grant", "grantId", "scopes", "expiresIn", "expiresAt", "tenant", "partSizeBytes"}`, plus `"uploadBaseUrl"` only when `upload_base_url` is set | Started |
| | 413 / 415 / 422 `{"detail", "reason"}` | The profile rejected the file (`reason` as [`UploadRejected.reason`](#uploadrejectedreason)) |
| | 403 | `can_start` returned a falsy value |
| | 422 | Unknown profile name, no name with several profiles, or an invalid body |
| | 502 | The media library failed or could not be reached; a session created before the grant failed is aborted |
| `POST {prefix}/{session_id}/grant` | 200 `{"sessionId", "grant", "grantId", "scopes", "expiresIn", "expiresAt"}` | A new grant for the same session |
| | 404 | Unknown session, or another user's |
| | 409 | The session is already attached |
| `POST {prefix}/{session_id}/attach` (with `on_attach`) | 200 `{"sessionId", "assetId"}` plus what `on_attach` returns | `claim_upload` passed and `on_attach` stored the asset |
| | 404 | Unknown session, another user's, or the media library has no such session or asset |
| | 403 `{"detail", "reason"}` | `check_attach` refused it (`CONTEXT_MISMATCH`) |
| | 409 `{"detail", "reason"}` | Already attached, or `profile` is not the session's |
| | 422 `{"detail", "reason"}` | The upload failed the check (`NOT_FINALIZED`, `ASSET_MISMATCH`, `VISIBILITY`…), or an unknown profile name |

`ttl_seconds` sets the grants' lifetime (server default 600, at most its
`GRANT_MAX_TTL`); the browser refreshes before it ends.

**Where the parts go is the frontend's decision.** The service never sends
its own address: `base_url` is where *your service* reaches the media
library (often an internal address) and is never put in a response. The
Nuxt module chooses the upload base from its config: its same-origin
pass-through route (`mediaLibrary.serviceUploads.proxy`, which forwards the
browser's grant untouched), or the media library's public address
(`serviceUploads.baseURL`, which needs your site in the media library's
`CORS_ALLOWED_ORIGINS`). Set `upload_base_url` (the public address, with
`/v1`) only for a frontend that cannot choose; it is then returned as
`uploadBaseUrl`.

With the Nuxt module (and `mediaLibrary.serviceUploads` set), the browser
side is one call:

```ts
const { upload } = useMLServiceUpload()
const { sessionId, assetId } = await upload(file, {
  start: '/api/courses/uploads',
  body: { profile: 'course-cover', context: { courseId } },
})
await $fetch(`/api/courses/${courseId}/cover`, { method: 'PUT', body: { sessionId, assetId } })
```

**Attach.** Pass `on_attach` and the router adds
`POST {prefix}/{session_id}/attach` with `{"assetId", "profile"}`
(`profile` optional; when sent it must be the session's). It calls
`claim_upload` with the session's profile and owner, then
`on_attach(user, claimed)`, where you store the asset id;
`claimed.record.context` is what the start request sent, so it says where
the asset goes. `check_attach(user, record)` runs before anything is
attached, e.g. that the user may still edit that course:

```python
upload_router = create_upload_router(
    client, can_start, get_current_user,
    profiles={"course-cover": COURSE_COVER},
    store=upload_store,
    get_owner=lambda user: str(user.id),
    prefix="/courses/uploads",
    check_attach=lambda user, record: user.can_edit_course(record.context.get("courseId")),
    on_attach=lambda user, claimed: set_course_cover(
        claimed.record.context["courseId"], claimed.asset.id,
    ),  # may return a dict; it is merged into the 200 response
)
```

`on_attach` runs after `store.mark_attached`. With a database store, run
both in the request's transaction so a failed `on_attach` leaves the
session unattached. This is the route the Nuxt module's upload tray calls
for files whose page was left before they finished.

Or write the attach endpoint yourself, e.g. when the asset id comes in with
other data (`PUT /courses/{id}` with the cover's `sessionId` and `assetId`):

```python
def attach_cover(user, course_id, session_id, asset_id):
    try:
        claimed = claim_upload(
            client, upload_store, session_id, asset_id,
            owner=str(user.id), profile=COURSE_COVER,
            # started for this course? checked before anything is attached
            check_record=lambda record: record.context.get("courseId") == course_id,
        )
    except UploadRejected as exc:
        return 400, {"detail": str(exc), "reason": exc.reason}
    # save claimed.asset.id as the course's cover here
    return 200, {"assetId": str(claimed.asset.id)}
```

`claim_upload` checks, in order:

1. the store has the session, started by `owner` (`UNKNOWN_SESSION`);
2. it was started for this profile, by name (`PROFILE_MISMATCH`);
3. `check_record(record)`, when given, accepts it (`CONTEXT_MISMATCH`): use
   it to compare `record.context` with the request, like the course above;
4. it is not attached yet (`ALREADY_ATTACHED`);
5. `client.uploads.verify_upload` (`NOT_FINALIZED`, `ASSET_MISMATCH`,
   `VISIBILITY`, or a profile reason);
6. `store.mark_attached` sets the asset, once (`ALREADY_ATTACHED` if another
   request won).

It returns `ClaimedUpload(record, asset)`.

`verify_upload` matches the asset to the session by `upload_session_id`.
On servers that don't return it, it falls back to the blob, which
deduplicated uploads share, so it would also accept another asset with the
same bytes. The owner check and attach-once close that gap; the user holds
those bytes anyway.

**Your store.** `InMemoryUploadStore` loses records on restart and is not
shared between workers. In production, implement `UploadSessionStore`
(`save`, `get`, `mark_attached`) on your database. `mark_attached` must be
atomic and return whether it set the asset:

```sql
UPDATE upload_records SET attached_asset_id = :asset_id
WHERE session_id = :session_id AND attached_asset_id IS NULL
```

Run it in the same transaction as the write that stores the asset id, so a
failed write does not leave the upload marked attached.

## Processing options

`client.processing` returns the metadata you need to build upload settings:
kinds, visibilities, known MIME types per kind, each kind's pipeline and the
options of each step. Both methods return plain dicts, so new server fields
never break the client.

```python
schema = client.processing.get_schema()
pipelines = client.processing.get_pipelines()
```

`get_schema()` (`GET /v1/manage/processing-options/schema`) returns:

| Key | Meaning |
|---|---|
| `kinds`, `visibilities` | Accepted values for `kind` and `visibility` |
| `mimeTypesByKind` | Known MIME types per kind. Informational: your profile validates uploads |
| `pipelinesByKind` | Per kind: `pipeline` (steps in order), `processors` (`{step: {"options": schema}}`) and `configurableSteps` |
| `autoStartSteps` | Configurable steps that run with their defaults when `step_options` has none. Today: every configurable step |
| `gatedSteps` | Step → `ProcessingConfig` flag that must be true for it to run, for example `{"video_transcription": "enable_transcription"}` |
| `transcriptionAvailable` | Whether the server's `ENABLE_TRANSCRIPTION` is on |
| `uploadDefaults` | What an upload uses for omitted fields: `visibility` and `processingConfig` |

`get_pipelines()` (`GET /v1/manage/processing-options/pipelines`) returns
`{kind: {"pipeline": [...], "processors": {step: {"options": {...}}}}}`.

Each option schema has a `type`, and may have `default`, `min`, `max`,
`allowed_values`, `items` and `description`. Build a form from it:

```python
def option_fields(schema, kind):
    pipeline = schema["pipelinesByKind"][kind]
    for step in pipeline.get("configurableSteps", []):
        for name, spec in pipeline["processors"][step]["options"].items():
            yield {"step": step, "option": name, "type": spec["type"],
                   "default": spec.get("default"), "choices": spec.get("allowed_values")}

for field in option_fields(schema, "image"):
    print(field)
```

## Manage assets

`client.assets` reads, lists and deletes assets, changes their
visibility and rolls back web apps (`/v1/manage/assets`). Its models carry
metadata, plus the HLS/DASH stream URLs when the server sends them (see
[Stream URLs](#stream-urls)); get download and preview links from
[`client.delivery`](#get-delivery-links).

```python
asset = client.assets.get(result.asset_id)
print(asset.status, asset.size_bytes, asset.mime_type)

page = client.assets.list(kind="image", status="ready", page=1, page_size=20)
print(page.total, [a.original_filename for a in page.items])

for item in client.assets.resolve([result.asset_id]):
    print(item.asset_id, item.status)  # "ok" | "not_found" | "error"
```

- `list(*, kind=None, status=None, query=None, resource_policy_id=None,
  page=1, page_size=10)` returns a `PaginatedAssets` page. `query` searches
  the server side; `resource_policy_id` filters assets uploaded through a
  policy.
- `resolve(asset_ids)` takes 1 to 100 ids in one request (the server rejects
  more), and returns one `BatchResolveResult` per id.
- `delete(asset_id)` deletes an asset and its related records.

### Change visibility

```python
from pkg_media import Visibility

client.assets.update_visibility(result.asset_id, Visibility.RESTRICTED)

bulk = client.assets.bulk_update_visibility([result.asset_id], Visibility.PRIVATE)
print(bulk.updated, bulk.failed, bulk.items)
```

`bulk_update_visibility` returns a `BulkVisibilityResult` with `updated`,
`failed` and the raw `items` (`{"assetId", "ok", "changed", "error"}` dicts).
The server accepts 1 to 500 ids.

- Visibility is only data: no bytes move, every file stays in the tenant's
  R2 bucket. Making an asset `public` lets its gate links go without a token
  and be cached at the edge; leaving `public` purges the edge copies. The
  bulk call counts unknown assets (`"error": "not_found"`) and server
  errors (`"internal_error"`) in `failed`.
- While the server runs with `RESTRICTED_ASSETS_SERVICE_ONLY` or
  `MANAGE_RESTRICTED_PREVIEWS_PRIVILEGED_ONLY`, only a privileged caller may
  move a `restricted` asset to another visibility: a service token, a grant
  listing the asset, or a verified user with one of the server's
  `MANAGE_PRIVILEGED_ROLES`. Anyone else gets 403 (`AuthenticationError`), or
  `"error": "forbidden"` in the bulk result. Making an asset `restricted` is
  never limited.
- After changing an asset, `invalidate` it in any `AssetUrlCache`.

### Roll back a web app

A web app (`kind="web_app"`) keeps its recent deploys. List them and make an
older one live again:

```python
page = client.assets.list_deploys(app_id, limit=50, offset=0)
print(page.live_deploy_id, page.next_offset)
for deploy in page.deploys:  # newest first, `deleted` and `failed` too
    print(deploy.id, deploy.status, deploy.is_live, deploy.scan_status, deploy.file_count)

page = client.assets.activate_deploy(app_id, previous_deploy_id)
assert page.live_deploy_id == previous_deploy_id
```

- `list_deploys(asset_id, *, limit=50, offset=0)` returns a
  `WebAppDeployList`. `limit` is 1 to 200; `next_offset` is `None` on the
  last page.
- `activate_deploy(asset_id, deploy_id)` makes a retained deploy the live
  one and returns the first page of the list. Nothing is deleted.
  Activating the live deploy is a no-op. The target must be `ready`, and
  its ZIP's scan status one the gate serves (`clean`, `legacy_unscanned`,
  `skipped_too_large`).
- Both need the `update` scope on the asset: a service token, a trusted
  admin, or a grant with `update` listing the asset.
- The next page load serves the activated deploy; pages already open keep
  theirs until reloaded.
- A reprocess of the asset builds a new deploy from its newest upload, which
  goes live and replaces the rollback. To fix the app, upload a corrected
  ZIP. A deploy retired by retention can no longer be activated.

Refusals raise `NotFoundError` (404) or `ConflictError` (409) with the
server's `detail.reason` in `exc.reason`:

| Status | `exc.reason` | When |
|---|---|---|
| 404 | `asset_not_found`, `not_a_web_app`, `deploy_not_found` | Unknown or deleted asset, not a web app, another asset's deploy |
| 409 | `deploy_deleted`, `deploy_failed`, `deploy_uploading` | The target can't be served (`deleted`: retired by retention) |
| 409 | `scan_not_allowed` | The target's ZIP is `pending`, `infected` or unknown |
| 409 | `deploy_changed` | A concurrent deploy changed the rows: retry |

```python
from pkg_media import ConflictError

try:
    client.assets.activate_deploy(app_id, deploy_id)
except ConflictError as exc:
    if exc.reason == "deploy_changed":
        ...  # retry
```

### When previews are withheld

The manage `Asset`, its renditions and their variants carry preview links
(`preview_url`, `preview_url_type`, `preview_url_expires_in`): gate links,
`None` when the file is not in the tenant's R2 bucket. With
`MANAGE_RESTRICTED_PREVIEWS_PRIVILEGED_ONLY=true`, a `restricted` asset's
preview links are `None` for callers that are not privileged (thumbnails
excepted). For links you hand to users, use `client.delivery`.

### Permalinks for public assets

A permalink is a stable public address, `{media host}/p/{name}`, that you
hand out once and re-point later: for example a user's avatar. Your app
picks the name, stores the **name** (not the URL), and re-points it when the
user uploads a new picture; every page that embedded the address follows.
Service token only.

```python
from pkg_media import permalink_url

link = client.permalinks.set("users/42/avatar", new_asset_id)  # create or re-point
link.url                                    # "https://media.example.com/p/users/42/avatar"
client.permalinks.get("users/42/avatar")    # NotFoundError when unknown
client.permalinks.delete("users/42/avatar")

# Rendering: build the URL from the stored name
src = permalink_url("https://media.example.com", "users/42/avatar")
```

- Only `public` assets: `set` raises `ValidationError` (422) for a
  `private` or `restricted` asset and `NotFoundError` for a missing one. A
  public asset that is not ready yet is accepted; `/p/{name}` answers 404
  until it is.
- `/p/{name}` serves the file directly, as a tokenless gate link
  (`?download=1` for an attachment). It answers 404 once the target is no
  longer public, ready or servable.
- Re-pointing or deleting purges the old edge copy.
- Names: segments of letters, digits, `.`, `_` and `-` (not starting with
  `.`) joined by `/`, at most 255 characters. The client raises
  `ValueError` for any other name before sending a request
  (`validate_permalink_name`).
- `permalink_url` needs the media host that serves files (the server's
  `VOLUME_BASE_URL`), not the API `base_url`. `Permalink.url` is built from
  it by the server.

## Get delivery links

`client.delivery` (`/v1/delivery/assets`) returns slim, client-facing
assets with download and preview links.

```python
asset_ids = [result.asset_id, finished.asset_id]

delivered = client.delivery.get(result.asset_id)
print(delivered.download_url, delivered.download_url_expires_in)

items = client.delivery.resolve(asset_ids)
for item in items:
    if item.status == "ok":
        print(item.asset_id, item.asset.download_url)

listing = client.delivery.list(kind="image", page_size=50)  # no links in the list
```

- `get(asset_id, *, grants=None)` returns a `DeliveryAsset`. A missing asset
  is a 404 (`NotFoundError`); an asset the caller may not open is a 403
  (`AuthenticationError`).
- `list(*, kind=None, status=None, query=None, page=1, page_size=10)` returns
  a `PaginatedDeliveryAssets` page without download links.
- `resolve(asset_ids, *, cache=None, grants=None)` returns one
  `DeliveryBatchResolveResult` per id, in input order:
  - any number of ids: the client sends batches of 100
    (`DeliveryAPI.RESOLVE_BATCH_SIZE`);
  - an empty input returns `[]` without a request;
  - duplicate ids each get a result.

### Resolve statuses

`DeliveryBatchResolveResult.status` is a plain `str` (so a newer server
status never breaks parsing). Compare it with `ResolveStatus`:

| `ResolveStatus` | Value | Meaning |
|---|---|---|
| `OK` | `"ok"` | `asset` is set, with its links |
| `NOT_FOUND` | `"not_found"` | No such asset in the tenant |
| `FORBIDDEN` | `"forbidden"` | The caller may not open it. `thumbnail_url` may still be set |
| `ERROR` | `"error"` | The asset exists but its link could not be resolved. `error` says why |

### Show a locked teaser

`thumbnail_url(result)` returns the asset's public thumbnail link: the
preview URL of its `thumbnail` rendition, or else the result's
`thumbnail_url`, which the server also sends for `forbidden` and `error`
items. It is never a download or other preview link.

```python
from pkg_media import thumbnail_url

for item in items:
    print(thumbnail_url(item))
```

### Download types

`DeliveryAsset.download_type` and every `preview_url_type` are plain `str`
(so a newer server value never breaks parsing). Compare them with
`DownloadType`:

| `DownloadType` | Value | Link |
|---|---|---|
| `GATE` | `"gate"` | R2 object behind the delivery gate (`/o/…`): with a token (expires), or without one for public objects the edge may cache (does not expire). The URL is `None` when the gate would refuse the object (for example a file not yet scanned) |
| `UNAVAILABLE` | `"unavailable"` | No link: the file is not in the tenant's R2 bucket. `download_url` is `None` |

### Stream URLs

For video and audio stored on R2 with a ready CMAF rendition, the server
also returns HLS and DASH entrypoints on its stream host, on both
`DeliveryAsset` and the manage `Asset`:

```python
asset = client.delivery.get(video_id)
if asset.hls_url:
    player_src = asset.hls_url        # or asset.dash_url
    ttl = asset.stream_url_expires_in  # seconds
```

`hls_url`, `dash_url` and `stream_url_expires_in` are `None` from servers
that don't send them, for assets without a stream, and when the server could
not mint the stream token. Both URLs share one token and one lifetime.

### Link expiry

Links are bound to the file, not to the user who asked. Each link comes with
its lifetime in seconds: `download_url_expires_in` and
`stream_url_expires_in` on the asset and `preview_url_expires_in` on each
rendition and variant. `None` or `0` means the link does not expire
(tokenless gate links of public assets). Cache one link per asset, not per user, until shortly before it
expires.

### Cache links with `AssetUrlCache`

`AssetUrlCache` keeps `ok` results per tenant and asset, until shortly before
their links expire:

```python
from pkg_media import AssetUrlCache

url_cache = AssetUrlCache(
    safety_margin=30,    # drop entries this many seconds before the shortest link expires
    default_ttl=300,     # lifetime of entries whose links never expire
    max_entries=10_000,  # optional; least recently used evicted first
)

items = client.delivery.resolve(asset_ids, cache=url_cache)  # still one per id, in order
```

With `cache=`, ids that have a live entry for the client's tenant are served
from it, and only the others are requested (each once, even when repeated).
An id the server returned nothing for gets an `error` result.

Rules:

- **Use a cache only with service-token clients, and never share it with a
  user-token client.** Entries are keyed by tenant and asset id, not by who
  asked, and a hit makes no request. A user-token client reading the cache
  would get the service token's results, `restricted` links included.
  `clear()` the cache if you `set_token` to a different principal.
- Only `ok` results for `ready` assets are cached. `not_found`, `forbidden`,
  `error` and assets still processing are always asked again.
- An entry lives until the shortest of its links (download URL, stream
  URLs, rendition and variant previews) expires, minus `safety_margin`. Results whose links expire
  within the margin are not cached. Entries whose links never expire live
  `default_ttl` seconds.
- Entries returned by `get`/`resolve` report the time their links have left:
  every `…_expires_in` is reduced by the entry's age. Treat them as
  read-only.
- One cache can serve clients of several tenants: `resolve(…, cache=)` and
  `AccessGate` store and look up under their client's tenant. When you call
  `get`/`put` directly, pass `tenant_id=`.
- The cache is thread-safe and lives in one process.
- Call `invalidate(asset_id)` after changing an asset. Without `tenant_id=`
  it drops the asset for every tenant.

```python
url_cache.invalidate(result.asset_id)
print(len(url_cache))  # live entries, over all tenants
```

### Resolve assets of several services with grants

A page may show assets owned by several services of the same tenant. Each
service returns a read grant with its data (see
[Several services on one page](#several-services-on-one-page)); pass them all
in one call:

```python
course_grant = client.grants.read([result.asset_id], subject=str(user.id))
profile_grant = client.grants.read([finished.asset_id], subject=str(user.id))

items = client.delivery.resolve(asset_ids, grants=[course_grant, profile_grant.grant])
```

`grants` takes `MediaGrant` objects or `mlg_…` strings, at most 10
(`DeliveryAPI.MAX_GRANTS`, `ValueError` beyond). They are sent in the
`X-Media-Grants` header. An asset listed by any grant resolves as for its
owning service, `private` and `restricted` included; the others resolve as
for the client's own token. The server ignores invalid or expired grants
instead of failing the batch. `grants` and `cache` cannot be combined
(`ValueError`).

## Protect restricted content

For `restricted` assets your backend decides who may open them, from its own
data (enrollments, purchases, roles), and hands out links resolved with its
service token. The media library still serves the files; your backend never
proxies bytes.

```
Browser ──▶ your backend ── can_access(user, asset_ids)?  (your data)
                  └─ service token ─▶ POST /v1/delivery/assets/resolve ─▶ links
Browser ◀── links ┘
Browser ──▶ media library delivery gate (R2) ─▶ file
```

### Recommended: links in your own responses

Authorize in the endpoint that serves the page, with the rules you already
have, and embed the links in its response:

1. **Store asset ids only** in your data (for example
   `Lesson.video_asset_id`), never links: links expire.
2. **Decide access in your endpoint**, as you do for the rest of the page.
3. **Embed links** from `client.delivery.resolve(ids, cache=...)` for the
   assets the user may open. For the others, embed only
   `thumbnail_url(result)` as a locked teaser, never the result itself.

```python
def course_page(user, course):
    enrolled = is_enrolled(user, course)  # your rules
    results = client.delivery.resolve([l.video_asset_id for l in course.lessons], cache=url_cache)
    return {"lessons": [
        {
            "title": lesson.title,
            "video": (r.asset.model_dump(mode="json", by_alias=True)
                      if enrolled and r.status == "ok" else None),
            "thumbnailUrl": thumbnail_url(r),
        }
        for lesson, r in zip(course.lessons, results)
    ]}

page = course_page(user, course)
```

### `AccessGate`: your rules over `/delivery`

When a frontend resolves arbitrary asset ids itself (a player that only
knows ids), put an `AccessGate` in front: it runs your `can_access` over
every id and answers with `/delivery`-shaped results.

```python
from pkg_media import AccessGate

def can_access(user, asset_ids):
    """Return the ids (of asset_ids) that user may open, in one query."""
    return db.allowed_asset_ids(user.id, asset_ids)

gate = AccessGate(client, can_access, cache=url_cache)

results = gate.resolve(user, asset_ids)       # one result per id, in order
one = gate.resolve_one(user, result.asset_id)
body = AccessGate.to_payload(results)         # the JSON of POST /delivery/assets/resolve
```

The `can_access(user, asset_ids)` contract:

- It is called once per request, with every distinct id as a list of
  `UUID`s, so it can be one database query.
- It returns an iterable of the allowed ids (UUIDs or strings), or a mapping
  `{asset_id: allowed}` where only truthy values allow. Returning `None`, a
  `bool` or a string raises `TypeError`.
- Its exceptions propagate unchanged.

What the gate returns, per requested id (duplicates included, in order):

- allowed ids: the media library's result (`ok`, `not_found` or `error`);
- denied ids: `forbidden` with `asset=None`, `error="Access denied"` and at
  most the public `thumbnail_url`. A denied id that does not exist is still
  `not_found`, as on the server.

The gate's client must use the tenant's **service token**: only it gets links
for `restricted` assets. With a user token the server answers `forbidden`
for them and the gate logs a warning. Every id is resolved with the service
token (through the cache, when given), including denied ones, which only
get their thumbnail. An empty input returns `[]` without calling
`can_access` or the media library.

Every failure of the media library call raises `MediaLibraryError`: error
responses as their usual subclass, transport errors and malformed responses
as `MediaLibraryError` with the original exception as `__cause__`. The gate
is thread-safe when `can_access` is.

### The media router

With FastAPI, `create_media_router(gate, get_current_user, *, prefix="/media")`
exposes the gate:

| Route | Same as | Responses |
|---|---|---|
| `POST {prefix}/resolve` with `{"assetIds": [...]}` (1 to 100) | `POST /delivery/assets/resolve` | 200 with `{"items", "total"}` |
| `GET {prefix}/{asset_id}` | `GET /delivery/assets/{id}` | 200 asset; 403 `{"detail", "thumbnailUrl"}`; 404; 502 |

`get_current_user` is your FastAPI dependency; what it returns is passed to
`can_access`. The routes are sync and run in FastAPI's threadpool.

Failures talking to the media library (error responses, connection errors,
timeouts, malformed responses) answer 502 with
`{"detail": "Media library request failed"}`, so an expired service token is
not mistaken for the user's own 401; the cause is logged. Exceptions from
`get_current_user` are never turned into a 502. Exceptions from `can_access`
stay 500 (or go to your exception handlers), even `httpx` or `pydantic`
errors, with one exception: a `MediaLibraryError` raised by `can_access`
(for example from a media library call it makes itself) cannot be told apart
from the gate's own and answers 502. A `GET` whose result is `error` answers
502 with the result's `error` and `thumbnailUrl`.

The frontend only changes its base URL for restricted assets. See
[FastAPI integration](#fastapi-integration) for a full app.

## Give browsers access with media grants

The browser can call the media library's `/manage` and `/delivery`
endpoints directly, without your backend re-implementing them. Your backend
checks the user with its own rules, then asks for a short-lived grant with
its service token. The media library enforces the grant. See the server's
[`docs/security/media-grants.md`](https://github.com/fritill-team/saas_media_library/blob/dev/docs/security/media-grants.md).

```python
from pkg_media import GrantScope

grant = client.grants.issue(
    [GrantScope.READ, GrantScope.UPDATE],
    asset_ids=[result.asset_id],  # what this user may act on
    subject=str(user.id),         # your user reference, logged by the server
    ttl_seconds=600,
)
response_body = grant.to_payload()  # {"grant", "grantId", "scopes", "expiresIn", "expiresAt"}
```

The browser sends `Authorization: Bearer mlg_…`. `X-Tenant` is optional with
a grant; when sent, it must be the grant's tenant (id or slug), or the
request gets 401.

### Scopes

| `GrantScope` | Value | Allows | Needs |
|---|---|---|---|
| `READ` | `read` | `GET /manage/assets/{id}`, `POST /manage/assets/resolve`, the asset's renditions and processing tasks; `/delivery` for the listed assets | `asset_ids` |
| `UPDATE` | `update` | `PATCH /manage/assets/{id}`, `POST /manage/assets/bulk-visibility`, `GET /manage/assets/{id}/deploys`, `POST /manage/assets/{id}/deploys/{deploy_id}/activate` | `asset_ids` |
| `DELETE` | `delete` | `DELETE /manage/assets/{id}` | `asset_ids` |
| `PROCESS` | `process` | Start processing, configure and retry tasks of the asset | `asset_ids` |
| `RENDITIONS` | `renditions` | `POST /manage/assets/{id}/renditions/*` | `asset_ids` |
| `UPLOAD` | `upload` | Create a session, then its parts, finalize, abort and get | `profile` (to create sessions) or `session_ids` (to continue them) |
| `MANAGE_ALL` | `manage:*` | Every `/manage` endpoint of the tenant except issuing grants, like a service token | — |

Endpoints that work across the tenant (listing assets, the processing
stream, resource policies, access grants) need `manage:*`. A request whose
body lists several assets is refused when any of them is not in the grant.

### Lifetime and limits

| | Default | Limit |
|---|---|---|
| `ttl_seconds` | 600 (server `GRANT_DEFAULT_TTL`) | 1 to `GRANT_MAX_TTL` (3600 by default); more is a 422 |
| `asset_ids` | — | 500 per grant |
| `session_ids` | — | 100 per grant |
| `subject` | — | 255 characters |

There is no revocation list. A grant works until it expires; rotating the
server's `GRANT_SIGNING_SECRET` revokes every grant.

### Issue a grant

`issue(scopes, *, asset_ids=None, profile=None, session_ids=None,
subject=None, ttl_seconds=None, collection_name=None)` calls
`POST /v1/manage/grants` and returns a `MediaGrant`:

| Field | JSON key | Meaning |
|---|---|---|
| `grant` | `grant` | The `mlg_…` token to give the browser |
| `grant_id` | `grantId` | Logged by the server with `subject` |
| `scopes` | `scopes` | The granted scopes |
| `expires_in` | `expiresIn` | Seconds |
| `expires_at` | `expiresAt` | `datetime` |

`grant.to_payload()` returns these as camelCase JSON for your response.
`collection_name` is stored in the grant's upload profile (it needs
`profile`).

`read(asset_ids, *, subject=None, ttl_seconds=None)` is a shortcut for a
`read` grant. It needs at least one id (`ValueError` otherwise).

Only a service token may issue grants. Errors: 422 (`ValidationError`) when
the server rejects the request, 401/403 (`AuthenticationError`) without a
service token, 503 (`ServerError`) when the server has no
`GRANT_SIGNING_SECRET`.

### `grant_profile_payload` and `validate_file`

An `upload` grant carries a profile, built from your `UploadProfile` by
`grant_profile_payload(profile)`:

```python
from pkg_media import grant_profile_payload

print(grant_profile_payload(COURSE_VIDEO))
```

The server applies it to every upload made with the grant: kind,
visibility, processing config and collection come from it, and the declared
`mime_type` and `expected_size_bytes` of each session must fit it. It differs from
`UploadProfile.validate_file`:

- On the server `"video/*"` matches every `video/…` type. The client compares
  it literally.
- `min_size_bytes` is not sent: the server has no minimum.
- `required_permissions` and `allow_anonymous` are your own check, before you
  issue the grant (`check_permissions`).
- `allowed_mime_types=None` (or containing `"*/*"`) is sent as "any type".
  An empty set, or `max_size_bytes=0`, raises `ValueError`: such a grant
  could upload nothing.
- With `max_size_bytes`, the browser must declare `expected_size_bytes` when
  it creates the session, and may not exceed it.

A mapping is passed through unchanged as the server's camelCase profile.

### Let the browser ask for grants

`create_grant_router` (FastAPI) gives the browser `POST {prefix}/grants`.
The browser asks, your `authorize` callback decides, the media library signs:

```python
from pkg_media.integrations.fastapi import create_grant_router

def get_current_user():
    """Your FastAPI auth dependency; see FastAPI integration for a full app."""
    return user

def authorize(user, request, profile):
    if request.profile:
        return profile.check_permissions(user.permissions)
    return all(user.can_edit(asset_id) for asset_id in request.asset_ids)

grant_router = create_grant_router(
    client, authorize, get_current_user,
    profiles={"course-cover": COURSE_COVER, "course-video": COURSE_VIDEO},
    get_subject=lambda user: str(user.id),
    ttl_seconds=600,
)
```

The request body (`GrantRequest`):

| Field | JSON key | Type | Limits |
|---|---|---|---|
| `scopes` | `scopes` | list of `GrantScope` values | at least 1 |
| `asset_ids` | `assetIds` | list of UUIDs | up to 100 |
| `session_ids` | `sessionIds` | list of UUIDs | up to 100 |
| `profile` | `profile` | the **name** of one of `profiles` | — |

The browser names a profile; it never sends a profile definition, so it
cannot choose kind, visibility or processing.

`authorize(user, request, profile)` is called for every request. `profile`
is the `UploadProfile` named by `request.profile`, or `None`. Return `True`
to issue exactly what was asked. Check `profile.check_permissions(...)` there
when the profile sets permissions, and check every asset id against your own
data.

| Response | When |
|---|---|
| 201 `{"grant", "grantId", "scopes", "expiresIn", "expiresAt"}` | Issued |
| 403 `{"detail": "Not allowed"}` | `authorize` returned a falsy value |
| 422 | Unknown profile name, `manage:*` requested, an invalid body, or a request the media library rejected |
| 502 `{"detail": "Media library request failed"}` | The media library could not be reached, refused the service token, or failed (for example 503 without `GRANT_SIGNING_SECRET`) |

`manage:*` is never issued through this router: it covers the whole tenant,
which no browser user should hold.

### Several services on one page

Every link comes from the media library, and each service decides only for
its own assets. Return a read grant with the data that references them:

```python
def lesson_response(user, lesson):
    grant = client.grants.read([lesson.video_asset_id], subject=str(user.id))
    return {"lesson": {"title": lesson.title, "videoAssetId": str(lesson.video_asset_id)},
            "mediaGrant": grant.grant}

payload = lesson_response(user, course.lessons[0])
```

The frontend collects the grants of every service on the page and resolves
everything in one request:

```
POST /v1/delivery/assets/resolve
X-Tenant: acme
X-Media-Grants: mlg_<course service>, mlg_<profile service>
{"assetIds": [...]}
```

An asset resolves only if a grant lists it (or it is visible to the caller
anyway). One service's missing grant only affects that service's assets. At
most 10 grants per request; more is a 400. From Python, pass `grants=` to
[`delivery.resolve`](#resolve-assets-of-several-services-with-grants).

Until assets record which service created them, any service token of the
tenant can issue a grant for any asset of the tenant: services of a tenant
trust each other.

### Security rules

- **Never expose a service token** to a browser, a mobile app or Nuxt public
  runtime config. It can do everything in the tenant.
- **Decide first, then issue.** Call `issue` only after your own check, for
  exactly what the user has on screen.
- **Keep grants short and scoped.** Grants are bearer tokens: anyone holding
  one may use it until it expires. Prefer the default 10 minutes or less.
- Never issue `manage:*` to a browser.
- The grant does not cover an asset an upload creates. Store the new asset id
  in your backend and issue a new grant if the user should keep working on
  it.

## FastAPI integration

Both routers in one app. Replace `get_current_user` with your own auth
dependency; whatever it returns is passed to `can_access` and `authorize`.

```python
import os
from dataclasses import dataclass, field

from fastapi import FastAPI, Header, HTTPException

from pkg_media import AccessGate, AssetUrlCache, MediaLibraryClient
from pkg_media.integrations.fastapi import create_grant_router, create_media_router

media = MediaLibraryClient(
    base_url="https://media.example.com",
    tenant_id="acme",
    token=os.environ["MEDIA_LIBRARY_TOKEN"],  # the tenant's service token
)

@dataclass
class AppUser:
    id: str
    permissions: set = field(default_factory=set)
    editable: set = field(default_factory=set)

    def can_edit(self, asset_id):
        return str(asset_id) in self.editable

USERS = {"alice": AppUser("alice", {"courses:write"})}  # stands in for your user store

def get_current_user(x_user: str = Header(default="")) -> AppUser:  # stands in for your auth
    user = USERS.get(x_user)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

def can_access(user, asset_ids):
    return [a for a in asset_ids if str(a) in user.editable]

def authorize(user, request, profile):
    if request.profile:
        return profile.check_permissions(user.permissions)
    return all(user.can_edit(a) for a in request.asset_ids)

app = FastAPI()
app.include_router(create_media_router(AccessGate(media, can_access, cache=AssetUrlCache()), get_current_user))
app.include_router(create_grant_router(
    media, authorize, get_current_user,
    profiles={"course-cover": COURSE_COVER},  # from Define an upload profile
    get_subject=lambda user: user.id,
))
```

This app serves:

- `POST /media/resolve` and `GET /media/{asset_id}`: links for the assets
  `can_access` allows;
- `POST /media/grants`: grants for what `authorize` allows.

A third router, `create_upload_router`, starts browser uploads and
refreshes their grants: see
[Uploads started by your backend](#uploads-started-by-your-backend).

## Handle errors

Every exception the client raises for a media library response inherits from
`MediaLibraryError`:

```
MediaLibraryError            status_code, body, reason
├── AuthenticationError      401, 403
├── NotFoundError            404
├── ConflictError            409
├── ValidationError          422
│   └── UploadRejected       a profile check failed (no request sent); reason
└── ServerError              5xx
```

All of them carry `status_code` (the HTTP status, or `None`) and `body` (the
parsed JSON body, or its text). The message is the response's `detail`.
`reason` is the response's `detail.reason` when the server sends one (for
example the [web-app deploy](#roll-back-a-web-app) routes), else `None`;
`UploadRejected` sets its own. They
survive pickling, for example across process pools.

| HTTP status | Exception |
|---|---|
| 401, 403 | `AuthenticationError` |
| 404 | `NotFoundError` |
| 409 | `ConflictError` (for example a finalize checksum mismatch) |
| 422 | `ValidationError` |
| 5xx | `ServerError` |
| Any other non-2xx (400, 405, 413, 429…) | `MediaLibraryError` |

```python
from pkg_media import AuthenticationError, MediaLibraryError, NotFoundError

try:
    client.assets.get("00000000-0000-0000-0000-000000000000")
except NotFoundError:
    print("no such asset")
except AuthenticationError as exc:
    print("check the token", exc.status_code)
except MediaLibraryError as exc:
    print(exc.status_code, exc.body)
```

**Transport errors are not wrapped.** Connection errors and timeouts raise
`httpx.HTTPError` subclasses (`httpx.ConnectError`, `httpx.TimeoutException`…)
from every method. `AccessGate` is the exception: it raises them as
`MediaLibraryError` (status `None`) with the original as `__cause__`.

Client-side errors raised before any request: `UploadRejected` (a profile
check), `TypeError` (`create_session` without a kind; `can_access` returning
the wrong type) and `ValueError` (invalid arguments such as too many grants,
`grants` with `cache`, or a part size that is too small).

### `UploadRejected.reason`

| Constant | Value | Meaning | Suggested HTTP status for your API |
|---|---|---|---|
| `UploadRejected.MIME_TYPE` | `mime_type` | Not `type/subtype`, or not allowed | 415 |
| `UploadRejected.SIZE_REQUIRED` | `size_required` | The profile sets size bounds and no size was given | 411 or 400 |
| `UploadRejected.TOO_LARGE` | `too_large` | Above `max_size_bytes` | 413 |
| `UploadRejected.TOO_SMALL` | `too_small` | Below `min_size_bytes` | 400 |
| `UploadRejected.KIND` | `kind` | `verify_asset`: the asset's kind differs | 400 |
| `UploadRejected.NOT_FINALIZED` | `not_finalized` | `verify_upload`: the session is not finalized | 409 |
| `UploadRejected.ASSET_MISMATCH` | `asset_mismatch` | `verify_upload`: the asset was not made from the session's blob | 400 |
| `UploadRejected.VISIBILITY` | `visibility` | `verify_upload`: the asset's visibility differs from the profile's | 400 |
| `UploadRejected.UNKNOWN_SESSION` | `unknown_session` | `claim_upload`: no record of the session, or another owner's | 404 |
| `UploadRejected.PROFILE_MISMATCH` | `profile_mismatch` | `claim_upload`: the session was started for another profile | 400 |
| `UploadRejected.CONTEXT_MISMATCH` | `context_mismatch` | `claim_upload`: `check_record` refused the record | 400 |
| `UploadRejected.ALREADY_ATTACHED` | `already_attached` | `claim_upload`: the session was attached before | 409 |

`UploadRejected.status_code` is `None`: the check ran in the client (for
`verify_upload`, on what the media library returned).

### 503 "Media grants are not configured"

The server has no `GRANT_SIGNING_SECRET`. `grants.issue` raises
`ServerError` (status 503), and every request that carries a grant gets 503
too. It is a server setup problem: ask the media library's operators.

### Retries

- **Safe to retry** with backoff: `ServerError` with 502, 503 or 504, and
  `httpx.TransportError`, for reads (`get`, `list`, `resolve`, processing
  options) and for `upload_part` (idempotent with the same checksum).
- **Check before retrying** `finalize`: call `get_session` first; if the
  state is `finalized`, the upload succeeded.
- **Do not blindly retry** `create_session` (each call creates a new
  session), `grants.issue` (issue a new grant only when needed) or
  `delete`.
- **Never retry** `AuthenticationError`, `NotFoundError`, `ValidationError`
  or `ConflictError`: the request itself is wrong.

For connection-level retries, give your own `httpx.Client` a transport with
`retries=` (see [Use your own `httpx.Client`](#use-your-own-httpxclient)).

## Test your integration

Mock the media library's HTTP API with [respx](https://lundberg.github.io/respx/),
as this package's own tests do. Routes are the client's paths under your
`base_url`.

```python
import httpx
import respx

from pkg_media import MediaLibraryClient, ResolveStatus

BASE = "https://media.test"

@respx.mock
def test_resolve_returns_links():
    asset_id = "11111111-1111-1111-1111-111111111111"
    respx.post(f"{BASE}/v1/delivery/assets/resolve").mock(
        return_value=httpx.Response(200, json={"items": [{
            "assetId": asset_id, "status": "ok", "thumbnailUrl": None, "error": None,
            "asset": {"id": asset_id, "kind": "image", "status": "ready", "visibility": "private",
                      "mimeType": "image/png", "downloadType": "gate",
                      "downloadUrl": "https://files.test/a.png", "downloadUrlExpiresIn": 3600,
                      "renditions": []},
        }], "total": 1}),
    )
    with MediaLibraryClient(base_url=BASE, tenant_id="acme", token="ml_test_x") as test_client:
        [item] = test_client.delivery.resolve([asset_id])
    assert item.status == ResolveStatus.OK
    assert item.asset.download_url == "https://files.test/a.png"
```

Test your access rules without the media library's links logic by mocking
the resolve call and checking what the gate hands out:

```python
from pkg_media import AccessGate

@respx.mock
def test_gate_denies_without_enrollment():
    asset_id = "22222222-2222-2222-2222-222222222222"
    respx.post(f"{BASE}/v1/delivery/assets/resolve").mock(
        return_value=httpx.Response(200, json={"items": [{
            "assetId": asset_id, "status": "forbidden", "thumbnailUrl": "https://cdn.test/t.webp",
            "asset": None, "error": None,
        }], "total": 1}),
    )
    with MediaLibraryClient(base_url=BASE, tenant_id="acme", token="ml_test_x") as test_client:
        gate = AccessGate(test_client, lambda user, ids: [])  # nobody is enrolled
        [item] = gate.resolve("bob", [asset_id])
    assert item.status == "forbidden" and item.asset is None
    assert item.thumbnail_url == "https://cdn.test/t.webp"
```

Stub grants by mocking `POST /v1/manage/grants`, and test your FastAPI
routes with `TestClient` and `dependency_overrides`:

```python
from fastapi.testclient import TestClient

@respx.mock
def test_grant_route_issues_for_allowed_profile():
    respx.post("https://media.example.com/v1/manage/grants").mock(
        return_value=httpx.Response(201, json={
            "grant": "mlg_test", "grantId": "33333333-3333-3333-3333-333333333333",
            "scopes": ["upload"], "expiresIn": 600, "expiresAt": "2030-01-01T00:00:00Z",
        }),
    )
    app.dependency_overrides[get_current_user] = lambda: AppUser("alice", {"courses:write"})
    try:
        response = TestClient(app).post("/media/grants", json={"scopes": ["upload"], "profile": "course-cover"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 201
    assert response.json()["grant"] == "mlg_test"
```

`app`, `AppUser` and `get_current_user` are from the
[FastAPI integration](#fastapi-integration) example.

`AssetUrlCache` takes a `clock` (a function returning monotonic seconds), so
expiry can be tested without waiting:

```python
from pkg_media import DeliveryBatchResolveResult

now = [0.0]
test_cache = AssetUrlCache(clock=lambda: now[0], safety_margin=0)
entry = DeliveryBatchResolveResult.model_validate({
    "assetId": "44444444-4444-4444-4444-444444444444", "status": "ok",
    "asset": {"id": "44444444-4444-4444-4444-444444444444", "kind": "image", "status": "ready",
              "visibility": "private", "mimeType": "image/png", "downloadType": "gate",
              "downloadUrl": "https://files.test/x", "downloadUrlExpiresIn": 60, "renditions": []},
})
assert test_cache.put(entry) is True
now[0] = 61
assert test_cache.get(entry.asset_id) is None
```

## Models reference

All models are pydantic v2 models. Fields are snake_case in Python; the
JSON keys are the server's (camelCase except for upload sessions). Every
model accepts either name when you construct it (`populate_by_name`), and
`model_dump(by_alias=True)` gives the server's JSON.

### Enums

All enums are `str` enums: they compare equal to their values.

| Enum | Values |
|---|---|
| `AssetKind` | `image`, `video`, `audio`, `document`, `web_app`, `other`, `archive`, `font`, `model_3d`, `image_batch` |
| `AssetStatus` | `pending`, `uploaded`, `processing`, `awaiting_input`, `ready`, `failed`, `archived`, `deleted` |
| `Visibility` | `restricted`, `private`, `public` |
| `ResolveStatus` | `ok`, `not_found`, `forbidden`, `error` |
| `DownloadType` | `gate`, `unavailable` |
| `GrantScope` | `upload`, `read`, `update`, `delete`, `process`, `renditions`, `manage:*` |

### Manage models

#### `Asset`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `id` | `id` | `UUID` | required |
| `kind` | `kind` | `AssetKind` | required |
| `title` | `title` | `str` | `None` |
| `original_filename` | `originalFilename` | `str` | required |
| `mime_type` | `mimeType` | `str` | required |
| `size_bytes` | `sizeBytes` | `int` | required |
| `hash_sha256` | `hashSha256` | `str` | required |
| `status` | `status` | `AssetStatus` | required |
| `visibility` | `visibility` | `Visibility` | required |
| `created_at` | `createdAt` | `datetime` | required |
| `blob_id` | `blobId` | `UUID` | `None` |
| `resource_policy_id` | `resourcePolicyId` | `UUID` | `None` |
| `upload_session_id` | `uploadSessionId` | `UUID` | `None` |
| `renditions` | `renditions` | `List[Rendition]` | `[]` |
| `preview_url` | `previewUrl` | `str` | `None` |
| `preview_url_type` | `previewUrlType` | `str` (`DownloadType`) | `None` |
| `preview_url_expires_in` | `previewUrlExpiresIn` | `int` | `None` |
| `hls_url` | `hlsUrl` | `str` | `None` |
| `dash_url` | `dashUrl` | `str` | `None` |
| `stream_url_expires_in` | `streamUrlExpiresIn` | `int` | `None` |

#### `Rendition`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `id` | `id` | `UUID` | required |
| `rendition_type` | `renditionType` | `str` | required |
| `entrypoint` | `entrypoint` | `str` | required |
| `storage_prefix` | `storagePrefix` | `str` | `None` |
| `blob_ids` | `blobIds` | `List[UUID]` | `[]` |
| `variants` | `variants` | `List[RenditionVariant]` | `[]` |
| `duration_seconds` | `durationSeconds` | `float` | `None` |
| `pages` | `pages` | `int` | `None` |
| `channels` | `channels` | `int` | `None` |
| `status` | `status` | `str` | required |
| `extras` | `extras` | `Dict[str, Any]` | `{}` |
| `created_at` | `createdAt` | `datetime` | required |
| `updated_at` | `updatedAt` | `datetime` | required |
| `preview_url` | `previewUrl` | `str` | `None` |
| `preview_url_type` | `previewUrlType` | `str` (`DownloadType`) | `None` |
| `preview_url_expires_in` | `previewUrlExpiresIn` | `int` | `None` |

#### `RenditionVariant`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `name` | `name` | `str` | required |
| `entrypoint` | `entrypoint` | `str` | required |
| `locale` | `locale` | `str` | `None` |
| `width` | `width` | `int` | `None` |
| `height` | `height` | `int` | `None` |
| `bitrate_kbps` | `bitrateKbps` | `int` | `None` |
| `frame_rate` | `frameRate` | `float` | `None` |
| `channels` | `channels` | `int` | `None` |
| `blob_ids` | `blobIds` | `List[UUID]` | `[]` |
| `extras` | `extras` | `Dict[str, Any]` | `{}` |
| `preview_url` | `previewUrl` | `str` | `None` |
| `preview_url_type` | `previewUrlType` | `str` (`DownloadType`) | `None` |
| `preview_url_expires_in` | `previewUrlExpiresIn` | `int` | `None` |

#### `BatchResolveResult`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `asset_id` | `assetId` | `UUID` | required |
| `status` | `status` | `str` | required |
| `asset` | `asset` | `Asset` | `None` |
| `error` | `error` | `str` | `None` |

#### `PaginatedAssets`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `items` | `items` | `List[Asset]` | required |
| `total` | `total` | `int` | required |
| `page` | `page` | `int` | required |
| `page_size` | `pageSize` | `int` | required |

#### `WebAppDeployList`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `live_deploy_id` | `liveDeployId` | `UUID` | `None` |
| `deploys` | `deploys` | `List[WebAppDeploy]` | `[]` |
| `limit` | `limit` | `int` | required |
| `offset` | `offset` | `int` | required |
| `next_offset` | `nextOffset` | `int` | `None` |

#### `WebAppDeploy`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `id` | `id` | `UUID` | required |
| `status` | `status` | `str` (`uploading`, `ready`, `live`, `failed`, `deleted`) | required |
| `created_at` | `createdAt` | `datetime` | required |
| `is_live` | `isLive` | `bool` | required |
| `blob_id` | `blobId` | `UUID` | `None` |
| `scan_status` | `scanStatus` | `str` | `None` |
| `file_count` | `fileCount` | `int` | required |

#### `Permalink`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `name` | `name` | `str` | required |
| `asset_id` | `assetId` | `UUID` | required |
| `url` | `url` | `str` | required |
| `created_at` | `createdAt` | `datetime` | required |
| `updated_at` | `updatedAt` | `datetime` | required |

### Delivery models

#### `DeliveryAsset`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `id` | `id` | `UUID` | required |
| `kind` | `kind` | `AssetKind` | required |
| `title` | `title` | `str` | `None` |
| `status` | `status` | `AssetStatus` | required |
| `visibility` | `visibility` | `Visibility` | required |
| `mime_type` | `mimeType` | `str` | required |
| `download_type` | `downloadType` | `str` | required |
| `download_url` | `downloadUrl` | `str` | `None` |
| `download_url_expires_in` | `downloadUrlExpiresIn` | `int` | `None` |
| `renditions` | `renditions` | `List[DeliveryRendition]` | `[]` |
| `hls_url` | `hlsUrl` | `str` | `None` |
| `dash_url` | `dashUrl` | `str` | `None` |
| `stream_url_expires_in` | `streamUrlExpiresIn` | `int` | `None` |

#### `DeliveryRendition`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `id` | `id` | `UUID` | required |
| `rendition_type` | `renditionType` | `str` | required |
| `status` | `status` | `str` | required |
| `entrypoint` | `entrypoint` | `str` | required |
| `duration_seconds` | `durationSeconds` | `float` | `None` |
| `variants` | `variants` | `List[DeliveryRenditionVariant]` | `[]` |
| `language` | `language` | `str` | `None` |
| `preview_url` | `previewUrl` | `str` | `None` |
| `preview_url_type` | `previewUrlType` | `str` | `None` |
| `preview_url_expires_in` | `previewUrlExpiresIn` | `int` | `None` |

#### `DeliveryRenditionVariant`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `name` | `name` | `str` | required |
| `width` | `width` | `int` | `None` |
| `height` | `height` | `int` | `None` |
| `bitrate_kbps` | `bitrateKbps` | `int` | `None` |
| `preview_url` | `previewUrl` | `str` | `None` |
| `preview_url_type` | `previewUrlType` | `str` | `None` |
| `preview_url_expires_in` | `previewUrlExpiresIn` | `int` | `None` |

#### `DeliveryBatchResolveResult`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `asset_id` | `assetId` | `UUID` | required |
| `status` | `status` | `str` | required |
| `thumbnail_url` | `thumbnailUrl` | `str` | `None` |
| `asset` | `asset` | `DeliveryAsset` | `None` |
| `error` | `error` | `str` | `None` |

#### `PaginatedDeliveryAssets`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `items` | `items` | `List[DeliveryAsset]` | required |
| `total` | `total` | `int` | required |
| `page` | `page` | `int` | required |
| `page_size` | `pageSize` | `int` | required |

### Upload models

#### `UploadSession`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `id` | `id` | `UUID` | required |
| `state` | `state` | `str` | required |
| `part_size_bytes` | `part_size_bytes` | `int` | required |
| `parts_expected` | `parts_expected` | `int` | `None` |
| `parts_received` | `parts_received` | `int` | required |
| `received_parts` | `received_parts` | `List[int]` | `[]` |
| `expires_at` | `expires_at` | `datetime` | required |
| `checksum_verified` | `checksum_verified` | `bool` | required |
| `final_blob_id` | `final_blob_id` | `UUID` | `None` |

#### `UploadPartResult`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `session_id` | `session_id` | `UUID` | required |
| `part_number` | `part_number` | `int` | required |
| `size_bytes` | `size_bytes` | `int` | required |
| `sha256_hex` | `sha256_hex` | `str` | required |
| `verified` | `verified` | `bool` | required |
| `parts_received` | `parts_received` | `int` | required |

#### `UploadFinalizeResult`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `session_id` | `session_id` | `UUID` | required |
| `final_blob_id` | `final_blob_id` | `UUID` | required |
| `asset_id` | `asset_id` | `UUID` | required |
| `checksum_verified` | `checksum_verified` | `bool` | required |
| `sha256_hex` | `sha256_hex` | `str` | required |

#### `UploadAbortResult`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `session_id` | `session_id` | `UUID` | required |
| `aborted` | `aborted` | `bool` | required |

### Grants and processing

#### `MediaGrant`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `grant` | `grant` | `str` | required |
| `grant_id` | `grantId` | `UUID` | required |
| `scopes` | `scopes` | `List[str]` | required |
| `expires_in` | `expiresIn` | `int` | required |
| `expires_at` | `expiresAt` | `datetime` | required |

#### `ProcessingConfig`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `step_options` | `stepOptions` | `Dict[str, Dict[str, Any]]` | `None` |
| `use_filename_as_asset_id` | `useFilenameAsAssetId` | `bool` | `False` |
| `enable_transcription` | `enableTranscription` | `bool` | `False` |

`BulkVisibilityResult` is a plain class with `items` (list of dicts),
`updated` (int) and `failed` (int).

### Deprecated models

Used by `client.policies` and `client.access`.

#### `ResourcePolicy`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `id` | `id` | `UUID` | required |
| `tenant_id` | `tenantId` | `UUID` | required |
| `resource_type` | `resourceType` | `str` | required |
| `collection_name` | `collectionName` | `str` | `None` |
| `file_constraints` | `fileConstraints` | `FileConstraints` | required |
| `permission_requirements` | `permissionRequirements` | `PermissionRequirements` | required |
| `processing_config` | `processingConfig` | `ProcessingConfig` | required |
| `kind` | `kind` | `AssetKind` | required |
| `visibility` | `visibility` | `Visibility` | required |
| `is_active` | `isActive` | `bool` | required |
| `version` | `version` | `int` | required |
| `created_at` | `createdAt` | `datetime` | required |
| `updated_at` | `updatedAt` | `datetime` | required |

#### `FileConstraints`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `allowed_mime_types` | `allowedMimeTypes` | `Set[str]` | required |
| `max_size_bytes` | `maxSizeBytes` | `int` | `None` |
| `min_size_bytes` | `minSizeBytes` | `int` | `None` |
| `allow_multiple` | `allowMultiple` | `bool` | `False` |

#### `PermissionRequirements`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `required_permissions` | `requiredPermissions` | `Set[str]` | `set()` |
| `allow_anonymous` | `allowAnonymous` | `bool` | `False` |

#### `AssetAccessGrant`

| Field | JSON key | Type | Default |
|---|---|---|---|
| `user_id` | `userId` | `UUID` | required |
| `expires_at` | `expiresAt` | `datetime` | `None` |
| `granted_at` | `grantedAt` | `datetime` | `None` |
| `granted_by_actor_type` | `grantedByActorType` | `str` | `None` |
| `granted_by_actor_id` | `grantedByActorId` | `str` | `None` |

## API reference

Every public name, generated from `__all__` and the API classes. HTTP paths
are relative to `base_url`.

### `MediaLibraryClient`

```
MediaLibraryClient(base_url, tenant_id, token=None, timeout=30.0, http_client=None)
```

| Member | Does | Returns |
|---|---|---|
| `tenant_id` | The tenant sent as `X-Tenant` (property) | `str` |
| `set_token(token)` | Replace the `Authorization` header | `None` |
| `close()` | Close the `httpx.Client` the client created (not one you passed) | `None` |
| `__enter__` / `__exit__` | Context manager; closes on exit | the client |
| `uploads`, `processing`, `assets`, `delivery`, `grants`, `permalinks`, `policies`, `access` | The API objects below | — |

### `UploadsAPI` (`client.uploads`)

`BASE = "/v1/manage/uploads/sessions"`.

| Method | HTTP | Returns | Raises | Does |
|---|---|---|---|---|
| `create_session(*, mime_type, profile=None, kind=None, visibility=None, processing_config=None, original_filename=None, expected_size_bytes=None, expected_sha256_hex=None, collection_name=None, validate=True)` | `POST BASE` | `UploadSession` | `TypeError` (no kind), `UploadRejected`, HTTP errors | Create an upload session |
| `get_session(session_id)` | `GET BASE/{id}` | `UploadSession` | HTTP errors | A session's state and received parts |
| `upload_part(session_id, part_number, data, *, sha256_hex=None, offset=None, total_size_bytes=None)` | `PUT BASE/{id}/parts/{n}` | `UploadPartResult` | HTTP errors | Upload one part, with its SHA-256 |
| `finalize(session_id, *, expected_sha256_hex=None)` | `POST BASE/{id}/finalize` | `UploadFinalizeResult` | `ConflictError` (checksum), HTTP errors | Assemble the parts into an asset |
| `abort(session_id)` | `POST BASE/{id}/abort` | `UploadAbortResult` | HTTP errors | Abort and delete the parts (and the blob of a finalized session) |
| `upload_file(source, *, profile, mime_type=None, filename=None, collection_name=None, part_size_bytes=None, verify_checksum=True)` | all of the above | `UploadFinalizeResult` | `UploadRejected`, `ValueError`, `MediaLibraryError` (short source), HTTP errors | Validate, upload and finalize a path, bytes or file object |
| `verify_upload(session_id, asset_id, *, profile)` | `GET BASE/{id}`, `GET /v1/manage/assets/{asset_id}` | `Asset` | `UploadRejected`, HTTP errors | Check that a browser upload's asset came from the session and fits the profile, see [Uploads from the browser](#uploads-from-the-browser) |

### `ProcessingOptionsAPI` (`client.processing`)

`BASE = "/v1/manage/processing-options"`.

| Method | HTTP | Returns | Raises | Does |
|---|---|---|---|---|
| `get_schema()` | `GET BASE/schema` | `dict` | HTTP errors | Kinds, visibilities, MIME types, pipelines, upload defaults |
| `get_pipelines()` | `GET BASE/pipelines` | `dict` | HTTP errors | Steps and options per kind |

### `AssetsAPI` (`client.assets`)

`MANAGE_BASE = "/v1/manage/assets"`.

| Method | HTTP | Returns | Raises | Does |
|---|---|---|---|---|
| `list(*, kind=None, status=None, query=None, resource_policy_id=None, page=1, page_size=10)` | `GET MANAGE_BASE` | `PaginatedAssets` | HTTP errors | List assets with full metadata |
| `get(asset_id)` | `GET MANAGE_BASE/{id}` | `Asset` | `NotFoundError`, HTTP errors | One asset's metadata and renditions |
| `resolve(asset_ids)` | `POST MANAGE_BASE/resolve` | `list[BatchResolveResult]` | HTTP errors (422 beyond 100 ids) | Metadata for up to 100 assets |
| `delete(asset_id)` | `DELETE MANAGE_BASE/{id}` | `None` | `NotFoundError`, HTTP errors | Delete an asset and its records |
| `update_visibility(asset_id, visibility)` | `PATCH MANAGE_BASE/{id}` | `Asset` | `AuthenticationError` (403), HTTP errors | Change one asset's visibility (no bytes move) |
| `bulk_update_visibility(asset_ids, visibility)` | `POST MANAGE_BASE/bulk-visibility` | `BulkVisibilityResult` | HTTP errors | Change visibility of up to 500 assets |
| `list_deploys(asset_id, *, limit=50, offset=0)` | `GET MANAGE_BASE/{id}/deploys` | `WebAppDeployList` | `NotFoundError`, HTTP errors | A web app's deploys, newest first |
| `activate_deploy(asset_id, deploy_id)` | `POST MANAGE_BASE/{id}/deploys/{deploy_id}/activate` | `WebAppDeployList` | `NotFoundError`, `ConflictError` (with `reason`), HTTP errors | Roll back a web app to a retained deploy |

### `DeliveryAPI` (`client.delivery`)

`BASE = "/v1/delivery/assets"`, `RESOLVE_BATCH_SIZE = 100`, `MAX_GRANTS = 10`,
`GRANTS_HEADER = "X-Media-Grants"`. `DeliveryAPI(http, *, tenant_id=None)`;
`tenant_id` scopes cache entries.

| Method | HTTP | Returns | Raises | Does |
|---|---|---|---|---|
| `list(*, kind=None, status=None, query=None, page=1, page_size=10)` | `GET BASE` | `PaginatedDeliveryAssets` | HTTP errors | List assets, without links |
| `get(asset_id, *, grants=None)` | `GET BASE/{id}` | `DeliveryAsset` | `NotFoundError`, `AuthenticationError`, `ValueError` (> 10 grants), HTTP errors | One asset with its links |
| `resolve(asset_ids, *, cache=None, grants=None)` | `POST BASE/resolve`, per 100 ids | `list[DeliveryBatchResolveResult]` | `ValueError` (> 10 grants, or both `cache` and `grants`), HTTP errors | Links for any number of assets |

### `GrantsAPI` (`client.grants`)

`BASE = "/v1/manage/grants"`.

| Method | HTTP | Returns | Raises | Does |
|---|---|---|---|---|
| `issue(scopes, *, asset_ids=None, profile=None, session_ids=None, subject=None, ttl_seconds=None, collection_name=None)` | `POST BASE` | `MediaGrant` | `ValidationError` (422), `AuthenticationError`, `ServerError` (503), `ValueError` (empty grant profile) | Issue a grant |
| `read(asset_ids, *, subject=None, ttl_seconds=None)` | `POST BASE` | `MediaGrant` | `ValueError` (no ids), as `issue` | Issue a `read` grant |

### `PermalinksAPI` (`client.permalinks`)

`BASE = "/v1/manage/permalinks"`. Service token only. Every method raises
`ValueError` for an invalid name before sending a request.

| Method | HTTP | Returns | Raises | Does |
|---|---|---|---|---|
| `set(name, asset_id)` | `PUT BASE/{name}` | `Permalink` | `NotFoundError` (asset), `ValidationError` (not public), `AuthenticationError`, HTTP errors | Create or re-point a name |
| `get(name)` | `GET BASE/{name}` | `Permalink` | `NotFoundError`, HTTP errors | One permalink |
| `delete(name)` | `DELETE BASE/{name}` | `None` | `NotFoundError`, HTTP errors | Remove a name; its edge copy is purged |

### `AccessGate`

```
AccessGate(client, can_access, *, cache=None)
```

`DENIED_ERROR = "Access denied"`. `cache` is an attribute.

| Method | HTTP | Returns | Raises | Does |
|---|---|---|---|---|
| `resolve(user, asset_ids)` | `POST /v1/delivery/assets/resolve` (via `client.delivery`) | `list[DeliveryBatchResolveResult]` | `MediaLibraryError`, `TypeError`, what `can_access` raises | Your check, then service-token links |
| `resolve_one(user, asset_id)` | as `resolve` | `DeliveryBatchResolveResult` | as `resolve` | `resolve` for one id |
| `to_payload(results)` (static) | — | `dict` | — | `{"items": [...], "total": n}` in the server's JSON |

### `AssetUrlCache`

```
AssetUrlCache(*, safety_margin=30.0, default_ttl=300.0, max_entries=None, clock=time.monotonic)
```

`DEFAULT_SAFETY_MARGIN = 30.0`, `DEFAULT_TTL = 300.0`. Raises `ValueError`
for a negative margin or TTL, or `max_entries` below 1. No HTTP calls.

| Method | Returns | Does |
|---|---|---|
| `get(asset_id, *, tenant_id=None)` | `DeliveryBatchResolveResult` or `None` | A live entry, with expiries reduced by its age |
| `get_many(asset_ids, *, tenant_id=None)` | `dict[UUID, DeliveryBatchResolveResult]` | Live entries only; misses left out |
| `put(result, *, tenant_id=None)` | `bool` | Cache one result; an uncacheable one drops the old entry |
| `put_many(results, *, tenant_id=None)` | `None` | `put` each |
| `invalidate(asset_id, *, tenant_id=None)` | `None` | Drop one tenant's entry, or every tenant's |
| `clear()` | `None` | Drop everything |
| `ttl_for(result)` | `float` or `None` | Seconds `result` may be cached, `None` if it must not be |
| `len(cache)` | `int` | Live entries over all tenants |

### `UploadProfile`

Fields: see [Define an upload profile](#define-an-upload-profile).

| Method | Returns | Raises | Does |
|---|---|---|---|
| `validate_file(mime_type, size_bytes=None)` | `None` | `UploadRejected` | Check a file before uploading |
| `check_permissions(permissions, *, authenticated=True)` | `bool` | — | Whether a user may upload with it |
| `verify_asset(asset)` | `None` | `UploadRejected` | Check an uploaded `Asset` or `DeliveryAsset` |
| `to_session_fields()` | `dict` | — | `kind`, `visibility` and `processingConfig` for `POST /manage/uploads/sessions` |

### `MediaGrant`

Fields: see [Issue a grant](#issue-a-grant). `to_payload()` returns the
camelCase JSON as a `dict`.

### Functions

| Function | Returns | Raises | Does |
|---|---|---|---|
| `thumbnail_url(result)` | `str` or `None` | — | A result's public thumbnail link |
| `permalink_url(media_base_url, name)` | `str` | `ValueError` (invalid name) | `{media_base_url}/p/{name}` |
| `validate_permalink_name(name)` | `str` | `ValueError` | `name` unchanged when the server accepts it |
| `grant_profile_payload(profile)` | `dict` | `ValueError` (empty MIME set, `max_size_bytes=0`) | The server-side grant profile for an `UploadProfile` |
| `policy_key(resource_type, collection_name=None)` | `str` | — | `"resource_type/collection_name"` or `"resource_type"` |
| `find_profile(profiles, resource_type, collection_name=None)` | `UploadProfile` or `None` | — | The profile replacing the policy the server used for that pair |
| `profiles_from_policies(policies)` | `dict[str, UploadProfile]` | `ValueError` | One profile per resource policy |
| `render_profiles_module(profiles)` | `str` | — | Python source defining `UPLOAD_PROFILES` |
| `claim_upload(client, store, session_id, asset_id, *, owner, profile, check_record=None)` | `ClaimedUpload` | `UploadRejected`, HTTP errors | Verify a browser upload and attach it once, see [Uploads started by your backend](#uploads-started-by-your-backend) |

### `pkg_media.integrations.fastapi`

Needs the `fastapi` extra.

| Name | Returns | Does |
|---|---|---|
| `create_media_router(gate, get_current_user, *, prefix="/media")` | `APIRouter` | `POST {prefix}/resolve`, `GET {prefix}/{asset_id}`, see [The media router](#the-media-router) |
| `create_grant_router(client, authorize, get_current_user, *, profiles=None, get_subject=None, ttl_seconds=None, prefix="/media")` | `APIRouter` | `POST {prefix}/grants`, see [Let the browser ask for grants](#let-the-browser-ask-for-grants) |
| `create_upload_router(client, can_start, get_current_user, *, profiles, store, get_owner, upload_base_url=None, ttl_seconds=None, prefix="/uploads")` | `APIRouter` | `POST {prefix}` and `POST {prefix}/{session_id}/grant`, see [Uploads started by your backend](#uploads-started-by-your-backend). `upload_base_url` is optional and returned to browsers as is; the client's `base_url` never is. Raises `ValueError` for no profiles, or a profile whose `name` is not its key |
| `GrantRequest` | — | The grant router's request body |
| `StartUploadRequest` | — | The upload router's start body |
| `ResolveRequest` | — | The media router's resolve body: `asset_ids` (`assetIds`), 1 to 100 UUIDs |
| `MAX_RESOLVE_IDS` | `100` | Ids per resolve request |

### `pkg_media.service_uploads`

All exported from the package root. See
[Uploads started by your backend](#uploads-started-by-your-backend).

| Name | Does |
|---|---|
| `UploadRecord(session_id, owner, profile, context={}, created_at=now, attached_asset_id=None)` | Frozen dataclass: who started a session, with which profile (by name) and context |
| `UploadSessionStore` | Protocol: `save(record)`, `get(session_id)` → record or `None`, `mark_attached(session_id, asset_id)` → `bool` (atomic, once) |
| `InMemoryUploadStore()` | An `UploadSessionStore` in process memory, thread-safe; tests and single-process development only |
| `ClaimedUpload(record, asset)` | What `claim_upload` returns: the attached record and the verified `Asset` |
| `claim_upload(...)` | See [Functions](#functions) |

### Exceptions

`MediaLibraryError(message, status_code=None, body=None, *, reason=None)`,
`NotFoundError`, `AuthenticationError`, `ValidationError`, `ConflictError`,
`ServerError` and `UploadRejected(message, *, reason)`. See
[Handle errors](#handle-errors).

### Deprecated: `ResourcePoliciesAPI` (`client.policies`)

`BASE = "/v1/manage/resource-policies"`. Every method emits a
`DeprecationWarning`. Methods that take `policy_id` also accept
`resource_type=` / `collection_name=` instead (`TypeError` for both or
neither).

| Method | HTTP | Returns | Replacement |
|---|---|---|---|
| `upsert(resource_type, kind, visibility, allowed_mime_types, *, collection_name=None, max_size_bytes=None, min_size_bytes=None, allow_multiple=False, required_permissions=None, allow_anonymous=False, step_options=None, use_filename_as_asset_id=False, enable_transcription=True, **deprecated_kwargs)` | `POST BASE` | `ResourcePolicy` | `UploadProfile` |
| `get(policy_id)` | `GET BASE/{id}` | `ResourcePolicy` | — |
| `get_by_name(resource_type, collection_name=None)` | `GET BASE` | `ResourcePolicy` (`NotFoundError` if none) | `find_profile` |
| `list(*, resource_type=None, collection_name=None, is_active=None)` | `GET BASE` | `list[ResourcePolicy]` | — |
| `update(policy_id=None, *, resource_type=None, collection_name=None, kind=None, visibility=None, file_constraints=None, permission_requirements=None, processing_config=None, is_active=None)` | `PUT BASE/{id}` | `ResourcePolicy` | Edit your `UploadProfile` |
| `delete(policy_id=None, *, resource_type=None, collection_name=None, hard=False)` | `DELETE BASE/{id}` | `None` | — |
| `activate(...)` / `deactivate(...)` | `PUT BASE/{id}` | `ResourcePolicy` | — |
| `get_schema()` | `GET BASE/schema` | `dict` | `client.processing.get_schema()` |
| `get_available_steps()` | `GET BASE/pipelines/available-steps` | `dict` | `client.processing.get_pipelines()` |
| `sync_policies(policies, *, delete_stale=False, hard_delete=False)` | several | `list[ResourcePolicy]` | Profiles in your code |

`upsert`'s `**deprecated_kwargs` accepts only `auto_process`, which is
ignored with a warning.

### Deprecated: `AssetAccessAPI` (`client.access`)

`BASE = "/v1/manage/assets"`, `BULK_BASE = "/v1/manage/access"`. Every
method emits a `DeprecationWarning`. Replacement: `AccessGate`.

| Method | HTTP | Returns |
|---|---|---|
| `grant(asset_id, user_id, expires_at=None)` | `POST BASE/{id}/access` | `AssetAccessGrant` |
| `revoke(asset_id, user_id)` | `DELETE BASE/{id}/access/{user_id}` | `None` |
| `list(asset_id)` | `GET BASE/{id}/access` | `list[AssetAccessGrant]` |
| `bulk_grant(asset_ids, user_id, expires_at=None)` | `POST BULK_BASE/bulk-grant` | `BulkGrantResult` (`items`, `granted`, `failed`) |
| `bulk_revoke(asset_ids, user_id)` | `POST BULK_BASE/bulk-revoke` | `BulkRevokeResult` (`revoked`) |

<a id="upgrading-and-server-compatibility"></a>
## Upgrading

### From v0.26: `saas_media_library` is now `pkg_media`

The package was renamed and now also carries the media gRPC contract.

- Install `pkg-media` instead of `saas-media-library` (same repository;
  tags are `pkg_media-v<version>`), and replace `saas_media_library` with
  `pkg_media` in imports: `from pkg_media import MediaLibraryClient`.
- `client.assets.delivery_list`, `delivery_get` and `delivery_resolve` are
  gone: call `client.delivery.list`, `get` and `resolve`.
- `client.processing.get_schema()` and `get_pipelines()` no longer fall back
  to the resource-policy routes on a 404: they raise `NotFoundError`. Every
  current server has `/manage/processing-options/*`.
- The deprecated `client.policies` and `client.access` methods are unchanged
  (they still warn on every call).
- New runtime dependencies: `protobuf` and `grpcio` (the stubs import them).

### From v0.25: R2-only server

- `DownloadType.PRESIGN`, `CDN` and `VOLUME_TOKEN` are gone (the server
  sends only `gate` or `unavailable`); add `DownloadType.UNAVAILABLE` where
  you handle a missing link. `download_type` stays a plain `str`.
- `ProcessingConfig.enable_transcription` defaults to `False`, as on the
  server. Uploads already sent `False` when unset; only code that read the
  model default directly sees a change. Resource policy responses always
  carry the field.
- Step names in `step_options` must be current: `video_package` replaces
  `video_transcode` and `video_adaptive_stream`; `relocate_to_bucket` and
  `relocate_to_volume` are gone.
- `update_visibility` no longer moves files, so it no longer fails with 400.

### From v0.23: `create_upload_router` no longer sends `base_url`

Without `upload_base_url`, the start response used to carry the client's
`base_url` + `/v1` as `uploadBaseUrl`. That is the service-side address
(often internal), so the response now leaves `uploadBaseUrl` out, and the
frontend chooses where parts go.

- Services that pass `upload_base_url`: nothing changes.
- Services that relied on the fallback: configure the frontend first. With
  the Nuxt module, set `mediaLibrary.serviceUploads` (`proxy: true` for the
  same-origin pass-through, or `baseURL` for the public media library) in a
  version that has it, then upgrade the service.

### From v0.18

Code written for v0.18 keeps working unchanged:

- The same exports and signatures, and the same requests for existing
  methods. `policies.get_available_steps()` still sends the legacy request.
- `requires-python >= 3.11` only corrects the metadata: v0.18 already needed
  3.11 (`StrEnum`).
- `client.policies.*` and `client.access.*` emit a `DeprecationWarning` on
  every call, pointing at your call site. Python hides them outside
  `__main__` by default. Apps or test suites that turn warnings into errors
  (`-W error`, pytest `filterwarnings = error`) should ignore
  `DeprecationWarning` from `pkg_media` until they migrate:

  ```toml
  [tool.pytest.ini_options]
  filterwarnings = [
      "error",
      "ignore::DeprecationWarning:pkg_media",
  ]
  ```

- To find remaining calls, run your tests once with
  `-W error::DeprecationWarning`.
- New features need a media library that includes them; see
  [Server compatibility](#server-compatibility).

### Resource policies → upload profiles

Resource policies are being retired on the server
([`docs/resource-policy/retirement.md`](https://github.com/fritill-team/saas_media_library/blob/dev/docs/resource-policy/retirement.md)).
During the migration window old and new calls work side by side.

1. **Generate profiles** from the tenant's current policies (needs a live
   media library and the tenant's service token):

   ```bash
   export MEDIA_LIBRARY_TOKEN=ml_live_…   # or pass --token
   python -m pkg_media.migrate_policies \
       --base-url https://media.example.com \
       --tenant acme > app/media_profiles.py
   ```

   This writes a module defining `UPLOAD_PROFILES`, keyed
   `"resource_type/collection_name"` (or `"resource_type"`), with every
   policy rule carried over, including `enable_transcription` as the policy
   had it. Only active policies are converted unless you pass
   `--include-inactive`. It exits with status 1 and a message on errors.
   Review the module and commit it. From code:

   ```python
   import warnings

   from pkg_media import find_profile, policy_key, profiles_from_policies, render_profiles_module

   with warnings.catch_warnings():
       warnings.simplefilter("ignore", DeprecationWarning)
       policies = client.policies.list(is_active=True)
   UPLOAD_PROFILES = profiles_from_policies(policies)
   source = render_profiles_module(UPLOAD_PROFILES)
   ```

   `profiles_from_policies` raises `ValueError` if two policies map to the
   same key or a policy allows a MIME type that is not `type/subtype`.

2. **Upload with profiles.** Replace uploads that sent `resource_type` with
   `upload_file(..., profile=...)` or `create_session(profile=...)`. Code
   that has the old pair at hand looks the profile up like the server did:

   ```python
   profile = find_profile(UPLOAD_PROFILES, "courses", "cover")
   ```

   `find_profile` returns the exact `"resource_type/collection_name"`
   profile, else the resource type's default (`"resource_type"`), else
   `None` (no policy applied to that pair). A plain
   `UPLOAD_PROFILES[policy_key(...)]` misses pairs the server served with the
   default policy. Pass `collection_name=` if you still want it recorded on
   the session.
3. **Enforce the rules in your app.** Use `check_permissions()`, let
   `create_session`/`upload_file` validate, and `verify_asset()` browser
   uploads.
4. **Remove the `sync_policies` init containers** (and their scripts,
   secrets and config) once no upload sends `resource_type`. Profiles ship
   with your code.
5. **Use `client.processing`** instead of `client.policies.get_schema()` /
   `get_available_steps()`.
6. **Leave the policies in place** until the server removes them: assets
   uploaded through a policy keep reading its processing config until then.
   On a shared tenant, never prune other services' policies
   (`delete_stale=True`). Services still on v0.16 run `sync_policies` with
   `delete_stale=True` by default: upgrade them.

### Access grants → `can_access`

Per-user access grants are being retired
([`docs/asset-access/retirement.md`](https://github.com/fritill-team/saas_media_library/blob/dev/docs/asset-access/retirement.md)).

1. **Write `can_access(user, asset_ids)`** from your own data and create an
   `AccessGate` with the tenant's service token.
2. **Add the media endpoint**: `create_media_router(...)`, or call
   `gate.resolve()` / `AccessGate.to_payload()` from your own framework.
3. **Point the frontend** at your endpoint instead of `/delivery` for
   `restricted` assets, or embed the links in your own responses (see
   [Recommended: links in your own responses](#recommended-links-in-your-own-responses)).
   `public` and `private` assets can stay on `/delivery`.
4. **Delete `grant` / `revoke` / `bulk_grant` / `bulk_revoke` calls** and
   any enrollment-sync jobs that fed them.
5. Once the server's `asset_access_grant_api` and `restricted_user_resolve`
   usage counters read zero, the media library switches to
   `RESTRICTED_ASSETS_SERVICE_ONLY=true` and ignores grants.

### Deprecated: `client.policies` and `client.access`

| Deprecated | Replacement |
|---|---|
| `client.policies.upsert/get/get_by_name/list/update/delete/activate/deactivate`, `sync_policies` | `UploadProfile` + `client.uploads` |
| `client.policies.get_schema()` | `client.processing.get_schema()`, in a different shape: camelCase keys (`mimeTypesByKind`, `pipelinesByKind`, `configurableSteps`), upload defaults instead of policy defaults, no `permission_schema` |
| `client.policies.get_available_steps()` | `client.processing.get_pipelines()` |
| `client.access.grant/revoke/list/bulk_grant/bulk_revoke` | `AccessGate(client, can_access)` |

They keep working until the server removes the endpoints; then they will be
removed from this client. See the [API reference](#deprecated-resourcepoliciesapi-clientpolicies)
for their signatures.

## Server compatibility

New features need a media library that includes them (the
resource-policy retirement release, see the server's
[`docs/deployment/upgrade-resource-policies-retirement.md`](https://github.com/fritill-team/saas_media_library/blob/dev/docs/deployment/upgrade-resource-policies-retirement.md)).

| Feature | Needs on the server | Against an older server |
|---|---|---|
| `client.uploads` without a resource policy (`UploadProfile`) | Policy-less uploads (`kind` on upload create) | `ValidationError` (422) |
| `client.processing` | `/manage/processing-options/*` | `NotFoundError` (404) |
| `client.grants`, `create_grant_router` | `POST /manage/grants` and `GRANT_SIGNING_SECRET` | `NotFoundError` (404); 503 without the secret |
| `delivery.resolve(grants=)` / `get(grants=)` | `X-Media-Grants` on `/delivery` | The header is ignored: assets a grant should cover come back `forbidden` |
| `AccessGate`, `restricted` links with a service token | Service tokens resolve `restricted` assets | Items come back `forbidden` |
| `hls_url`, `dash_url`, `stream_url_expires_in` | Stream URLs on resolve (and `STREAM_BASE_URL` set) | The fields are `None` |
| `download_type == "gate"` / `"unavailable"` | R2-only storage (the only types) | `presign`, `cdn` or `volume_token`, which this client no longer names |
| `preview_url*` on the manage `Asset`, `Rendition`, `RenditionVariant` | Preview links on `/manage` | The fields are `None` |
| `client.permalinks`, `permalink_url` | `/manage/permalinks` and `/p/` | `NotFoundError` (404) |
| `assets.list_deploys`, `assets.activate_deploy` | Web-app rollback routes | `NotFoundError` (404) with `reason` `None` |

Server settings that change what callers see, all off by default:

| Setting | Effect on this client |
|---|---|
| `JWT_VERIFICATION=enforce` | Invalid user tokens get 401 (503 if the signing keys are unreachable) |
| `MANAGE_AUTH=enforce` | `/manage` needs a service token, an admin or a grant: 401/403 otherwise |
| `DELIVERY_AUTH=enforce` | `private` assets come back `forbidden` without a service token, admin or grant; `delivery.list` needs a service token, an admin or a `manage:*` grant |
| `RESTRICTED_ASSETS_SERVICE_ONLY` | Access grants are ignored; only service tokens (and grants) get `restricted` links |
| `MANAGE_RESTRICTED_PREVIEWS_PRIVILEGED_ONLY` | Visibility changes of `restricted` assets need a privileged caller |

## Troubleshooting

**401 or 403?** 401 means the media library did not accept the credentials:
a missing, expired or forged token, an expired grant, or a grant sent with
another tenant's `X-Tenant`. 403 means it knows the caller but the caller may
not do this: a user token where a service token is required (uploads,
grants), a grant used outside its scopes or assets, or `MANAGE_AUTH=enforce`.
Both raise `AuthenticationError`; read `exc.status_code` and `exc.body`.

**Resolve returns `forbidden` items.** The caller may not open the asset:
it is `restricted` and the client does not use a service token (check
`token`), or the grant you passed does not list it. With `AccessGate`, a
`forbidden` item means your `can_access` denied it, or the gate's client is
not a service token (a warning is logged).

**`private` assets suddenly come back `forbidden`.** The server switched to
`DELIVERY_AUTH=enforce`. Resolve them with a service token, or send read
grants from the owning services (`grants=` / `X-Media-Grants`).

**422 on upload create.** The server rejected the request body: no `kind`
(and no profile), an invalid `kind` or `visibility` value, or a field of the
wrong type. A step name in `step_options` that is not in the kind's pipeline
is a 400 (`MediaLibraryError`) instead. Check `exc.body` for the field.
Against a server without policy-less uploads, every create is a 422: see
[Server compatibility](#server-compatibility).

**403 on upload create.** The caller is not trusted: use the service token,
or a grant with the `upload` scope. See [Who may upload](#who-may-upload).

**Grant 401 "X-Tenant does not match the grant's tenant".** The request
carries a grant and an `X-Tenant` of another tenant. Send no `X-Tenant` with
a grant, or the grant's own tenant (id or slug).

**Grant 401 "Invalid or expired media grant".** Issue a new grant. Grants
cannot be refreshed.

**503 "Media grants are not configured".** See
[503 "Media grants are not configured"](#503-media-grants-are-not-configured).

**Expired links.** A link stops working after its `…_expires_in` seconds.
Resolve again; with an `AssetUrlCache`, entries are dropped `safety_margin`
seconds before expiry, so a cached link always has some time left. If
browsers keep pages open for long, resolve links when they are needed
rather than when the page is built.

**"Tenant not found" or a missing-tenant error.** `tenant_id` must be the
tenant's slug, not its UUID.

**404 for every call.** `base_url` must be the API root without `/v1`: the
client adds `/v1/…` itself.

**`resolve` raises `ValueError`.** More than 10 grants, or `grants` and
`cache` together.

## Versioning and releases

Releases are cut by hand from the Actions tab. `auto-release.yml` runs the
tests and `tools/check_compat.py`, bumps the **minor** `version` in
`pyproject.toml`, moves `proto/baseline.binpb` forward, commits both, tags
`pkg_media-v<version>` and publishes a GitHub release with the wheel and
sdist. `release.yml` releases a chosen version (it does not move the
baseline). There is no separate changelog: the
[GitHub releases](https://github.com/fritill-team/saas_media_library_py_client/releases)
and the commit history between tags describe each version.

`pkg_media.__version__` reports the installed version.

### The gRPC contract (`proto/`)

The `fritill.media.v1` protos live in `proto/fritill/media/v1/` (one file per
service), next to `proto/fritill/conformance/v1/ping.proto`, which only
exercises the tooling. The generated stubs in `src/pkg_media/generated/`
are committed, together with `descriptor_set.binpb`; consumers never run a
code generator. After changing a `.proto`:

```bash
uv sync --all-groups
uv run --group codegen python tools/generate.py
uv run python tools/check_compat.py
uv run pytest
```

The package exports what both sides need:

- `pkg_media.contract`: the servicer base classes (`AssetsServicer`,
  `UploadsServicer`, `GrantsServicer`, `PermalinksServicer`,
  `DeliveryServicer`, `ProcessingServicer`), their `add_*_to_server`
  functions, the stubs, the message modules (`assets_pb2`…),
  `refusal_status(service, reason, message)` (a status carrying
  `google.rpc.ErrorInfo(domain=<service full name>, reason)`) and
  `error_info(rpc_error)`.
- `pkg_media.methods`: every method's wire path
  (`methods.Assets.GET_ASSET == "/fritill.media.v1.Assets/GetAsset"`),
  `MUTATING_METHODS` (they need the `idempotency-key` metadata) and
  `READ_ONLY_METHODS`.
- `pkg_media.reasons`: the reason constants, `REFUSALS` (each service's
  reasons with their gRPC status, as documented in the protos) and
  `grpc_status_for_http(code)` (400/422 → `INVALID_ARGUMENT`, 401 →
  `UNAUTHENTICATED`, 403 → `PERMISSION_DENIED`, 404 → `NOT_FOUND`, 409/410
  → `FAILED_PRECONDITION`, 429 → `RESOURCE_EXHAUSTED`, 503 → `UNAVAILABLE`,
  else `INTERNAL`).

The services are `Assets`, `Uploads`, `Grants`, `Permalinks`, `Delivery`
and `Processing`, unary only. File bytes never travel over gRPC:
`Uploads.OpenSession` returns a session and an upload grant for it, the
browser sends the parts to media's HTTP endpoints, and the service checks
the result with `Uploads.GetSession` and `Assets.GetAsset`. Resource
policies and asset access stay HTTP-only. `tests/test_media_contract.py`
checks the contract: no streaming, the error domain is the service name,
every refusal is documented, no request carries an idempotency key or a
credential.

`tests/certify/test_stubs_are_current.py` fails when the committed stubs are
stale. `tools/check_compat.py` compares the tree with `proto/baseline.binpb`
(the last release) and refuses a breaking change: a removed message,
service, method or field (without `reserved`), a changed field type or
label, a renumbered enum value, or a dropped reservation. Only add fields,
messages, methods or services; reserve what you remove. The release
workflow moves the baseline forward; by hand
(`tools/generate.py --baseline`) only for a signed-off major, in a commit
that does nothing else. `grpcio-tools` and `mypy-protobuf` are in the
`codegen` dependency group only, never a runtime dependency.
