#!/usr/bin/env python3
"""Remove flagged attestations from tag references without changing runtime layers.

Read-only by default. --apply requires Docker Hub credentials and rewrites only
tags whose public attestations match the privacy audit. Old digest-addressed
objects and previously downloaded copies may still require platform support.
"""

import argparse
import base64
import hashlib
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from audit_public_images import ACCEPT, REGISTRY, REPOSITORY, fetch, findings


def platform_reference(document):
    platforms = [item for item in document.get("manifests", [])
                 if item.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest"]
    platform = platforms[0].get("platform", {}) if len(platforms) == 1 else {}
    if platform.get("architecture") != "arm64" or platform.get("os") != "linux":
        raise RuntimeError("Refusing to rewrite an unexpected platform layout")
    return platforms[0]["digest"]


def plan_changes():
    tags = []
    next_page = f"https://hub.docker.com/v2/repositories/{REPOSITORY}/tags?page_size=100"
    while next_page:
        page = json.loads(fetch(next_page))
        tags.extend(item["name"] for item in page["results"])
        next_page = page.get("next")
    token = json.loads(fetch("https://auth.docker.io/token?service=registry.docker.io&"
                             f"scope=repository:{REPOSITORY}:pull"))["token"]
    cache = {}

    def read(url):
        if url not in cache:
            cache[url] = fetch(url, token, ACCEPT)
        return cache[url]

    changes = []
    email_fields = set()

    def inspect_fields(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                inspect_fields(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect_fields(child, f"{path}/{index}")
        elif isinstance(value, str) and "email address requiring review" in findings(value.encode()):
            email_fields.add(path)

    for tag in tags:
        raw = read(f"{REGISTRY}/manifests/{tag}")
        document = json.loads(raw)
        flagged = False
        for descriptor in document.get("manifests", []):
            if descriptor.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest":
                continue
            attestation = json.loads(read(f"{REGISTRY}/manifests/{descriptor['digest']}"))
            for layer in attestation.get("layers", []):
                if layer.get("mediaType") != "application/vnd.in-toto+json":
                    continue
                data = read(f"{REGISTRY}/blobs/{layer['digest']}")
                if findings(data):
                    flagged = True
                    inspect_fields(json.loads(data))
        if flagged:
            print(f"Flagged tag: {tag}")
            clean = dict(document)
            clean["manifests"] = [entry for entry in document["manifests"]
                                  if entry.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest"]
            changes.append({"tag": tag, "before": "sha256:" + hashlib.sha256(raw).hexdigest(),
                            "platform": platform_reference(document), "clean": clean})
    # Field names only: never print their email values or the event payload.
    for path in sorted(email_fields):
        print(f"Flagged email field: {path}")
    return changes, token


def request(request):
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except HTTPError as error:
        raise RuntimeError(f"Registry cleanup returned HTTP {error.code}") from None
    except (URLError, OSError):
        raise RuntimeError("Registry cleanup could not connect; inspect tag state before retrying") from None


def push_token():
    username = os.environ.get("DOCKERHUB_USERNAME", "")
    secret = os.environ.get("DOCKERHUB_TOKEN", "")
    if not username or not secret:
        raise RuntimeError("Apply requires Docker Hub credentials from the protected runner environment")
    authorization = base64.b64encode(f"{username}:{secret}".encode()).decode()
    url = ("https://auth.docker.io/token?service=registry.docker.io&"
           f"scope=repository:{REPOSITORY}:pull,push")
    return json.loads(request(Request(url, headers={"Authorization": f"Basic {authorization}"})))["token"]


def push_index(tag, document, token):
    data = json.dumps(document, separators=(",", ":")).encode()
    media_type = document.get("mediaType", "application/vnd.oci.image.index.v1+json")
    request(Request(f"{REGISTRY}/manifests/{tag}", data=data, method="PUT",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": media_type}))
    return "sha256:" + hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Rewrite flagged tag references")
    args = parser.parse_args()
    changes, token = plan_changes()
    print(f"Flagged tags: {len(changes)}")
    if not args.apply:
        print("Read-only preview; no tag changes made")
        return
    writable_token = push_token() if changes else None
    for item in changes:
        # Ensure the tag has not moved since the read-only planning phase.
        current = fetch(f"{REGISTRY}/manifests/{item['tag']}", token, ACCEPT)
        if "sha256:" + hashlib.sha256(current).hexdigest() != item["before"]:
            raise RuntimeError("Tag changed during cleanup; retry after publication finishes")
        expected = push_index(item["tag"], item["clean"], writable_token)
        for attempt in range(5):
            after_raw = fetch(f"{REGISTRY}/manifests/{item['tag']}", token, ACCEPT)
            if "sha256:" + hashlib.sha256(after_raw).hexdigest() == expected:
                break
            if attempt == 4:
                raise RuntimeError("Tag read-back did not match the clean index; inspect before retrying")
            time.sleep(1)
        after = json.loads(after_raw)
        if platform_reference(after) != item["platform"] or any(
                entry.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest"
                for entry in after.get("manifests", [])):
            raise RuntimeError("Tag read-back failed: runtime digest or attestation layout differs")
        print(f"Verified cleaned tag {item['tag']}: original ARM64 runtime digest preserved")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error))
