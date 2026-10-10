#!/usr/bin/env python3
"""Remove flagged attestations from tag references without changing runtime layers.

Read-only by default. --apply requires an existing Docker login and rewrites only
tags whose public attestations match the privacy audit. Old digest-addressed
objects and previously downloaded copies may still require platform support.
"""

import argparse
import hashlib
import json
import subprocess

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
            changes.append({"tag": tag, "before": "sha256:" + hashlib.sha256(raw).hexdigest(),
                            "platform": platform_reference(document)})
    # Field names only: never print their email values or the event payload.
    for path in sorted(email_fields):
        print(f"Flagged email field: {path}")
    return changes, token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Rewrite flagged tag references")
    args = parser.parse_args()
    changes, token = plan_changes()
    print(f"Flagged tags: {len(changes)}")
    if not args.apply:
        print("Read-only preview; no tag changes made")
        return
    for item in changes:
        tag = f"{REPOSITORY}:{item['tag']}"
        # Ensure the tag has not moved since the read-only planning phase.
        current = fetch(f"{REGISTRY}/manifests/{item['tag']}", token, ACCEPT)
        if "sha256:" + hashlib.sha256(current).hexdigest() != item["before"]:
            raise RuntimeError("Tag changed during cleanup; retry after publication finishes")
        try:
            result = subprocess.run(["docker", "buildx", "imagetools", "create", "--tag", tag,
                                     f"{REPOSITORY}@{item['platform']}"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Docker cleanup could not finish; inspect tag state before retrying") from None
        if result.returncode:
            raise RuntimeError("Docker could not update tag metadata; successful previous tags are retained")
        after = json.loads(fetch(f"{REGISTRY}/manifests/{item['tag']}", token, ACCEPT))
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
