"""Print a tenant's resource policies as a Python module of upload profiles.

Usage::

    python -m saas_media_library.migrate_policies \\
        --base-url https://api.media-library.example.com \\
        --tenant my-tenant --token "$SERVICE_TOKEN" > app/media_profiles.py

``--token`` defaults to the ``MEDIA_LIBRARY_TOKEN`` environment variable, which
keeps the token out of shell history. Only active policies are converted
unless ``--include-inactive`` is given.
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from typing import Optional, Sequence

import httpx

from saas_media_library.client import MediaLibraryClient
from saas_media_library.exceptions import MediaLibraryError
from saas_media_library.migration import profiles_from_policies, render_profiles_module

TOKEN_ENV_VAR = "MEDIA_LIBRARY_TOKEN"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m saas_media_library.migrate_policies",
        description=(
            "Print a Python module defining UPLOAD_PROFILES, converted from the "
            "tenant's resource policies."
        ),
    )
    parser.add_argument("--base-url", required=True, help="Media library API base URL")
    parser.add_argument("--tenant", required=True, help="Tenant id (sent as X-Tenant)")
    parser.add_argument(
        "--token",
        default=os.environ.get(TOKEN_ENV_VAR),
        help=f"Service token (default: ${TOKEN_ENV_VAR})",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Also convert deactivated policies",
    )
    args = parser.parse_args(argv)
    if not args.token:
        parser.error(f"--token is required (or set {TOKEN_ENV_VAR})")

    try:
        with MediaLibraryClient(
            base_url=args.base_url, tenant_id=args.tenant, token=args.token
        ) as client, warnings.catch_warnings():
            # Reading the policies is the point here; don't warn about it.
            warnings.simplefilter("ignore", DeprecationWarning)
            policies = client.policies.list(is_active=None if args.include_inactive else True)
        source = render_profiles_module(profiles_from_policies(policies))
    except (MediaLibraryError, httpx.HTTPError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
