#!/usr/bin/env python3
"""Publish the public README to Docker Hub without logging credentials."""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://github.com/Tingfe/person-timelapse-nas/blob/main/"
SHORT_DESCRIPTION = "Local person timelapses for NAS. ARM64, Web UI, resumable tasks and multi-day videos."


def build_payload(readme):
    def absolute_link(match):
        target = match.group(2)
        if re.match(r"(?:[a-z]+:|/|#)", target, re.I):
            return match.group(0)
        return f"{match.group(1)}({SOURCE_URL}{target})"

    content = re.sub(r"(\[[^\]\n]+\])\(([^\s)]+)\)", absolute_link, readme)
    if not content.strip() or len(content) > 25000:
        raise ValueError("Overview must contain 1 to 25000 characters")
    if len(SHORT_DESCRIPTION.encode("utf-8")) > 100:
        raise ValueError("Short description exceeds 100 bytes")
    return {"description": SHORT_DESCRIPTION, "full_description": content}


def request_json(url, method="GET", payload=None, token=None):
    headers = {"Content-Type": "application/json", "User-Agent": "person-timelapse-overview-sync"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        # Do not print response bodies: authentication errors may contain submitted data.
        raise RuntimeError(f"Docker Hub {method} returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError(f"Docker Hub {method} failed: connection unavailable") from None


def publish(payload, username, secret):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", username):
        raise ValueError("A valid DOCKERHUB_USERNAME is required")
    if not secret:
        raise ValueError("DOCKERHUB_TOKEN is required")
    repository = f"https://hub.docker.com/v2/repositories/{username}/person-timelapse-nas/"
    before = request_json(repository)
    print(f"Existing Overview: {len(before.get('full_description') or '')} characters")
    token = request_json("https://hub.docker.com/v2/auth/token", "POST",
                         {"identifier": username, "secret": secret}).get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Docker Hub did not return an access token")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{token}", flush=True)
    request_json(repository, "PATCH", payload, token)
    after = request_json(repository)
    if any(after.get(key) != value for key, value in payload.items()):
        raise RuntimeError("Docker Hub read-back does not match the published description")
    print(f"Overview verified: {len(payload['full_description'])} characters; "
          f"sha256={hashlib.sha256(payload['full_description'].encode()).hexdigest()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate locally; do not contact Docker Hub")
    args = parser.parse_args()
    payload = build_payload((ROOT / "README.md").read_text(encoding="utf-8"))
    if args.check:
        print(f"Overview valid: {len(payload['full_description'])} characters; no upload performed")
    else:
        publish(payload, os.environ.get("DOCKERHUB_USERNAME", ""), os.environ.get("DOCKERHUB_TOKEN", ""))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
