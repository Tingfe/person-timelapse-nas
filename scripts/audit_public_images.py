#!/usr/bin/env python3
"""Read-only privacy check of public image metadata and application COPY layers.

Large third-party runtime layers are not downloaded. Findings never print their
matched values; inspect them privately before deleting or replacing image tags.
"""

import io
import json
import re
import tarfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPOSITORY = "tingfe/person-timelapse-nas"
REGISTRY = f"https://registry-1.docker.io/v2/{REPOSITORY}"
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json"))


def fetch(url, token=None, accept=None):
    headers = {"User-Agent": "person-timelapse-public-privacy-audit"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            return response.read()
    except HTTPError as error:
        raise RuntimeError(f"Public image audit returned HTTP {error.code}") from None
    except (URLError, OSError):
        raise RuntimeError("Public image audit could not connect") from None


def findings(data):
    text = data.decode("utf-8", "replace")
    result = set()
    emails = re.findall(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    if any(not value.endswith("@users.noreply.github.com") for value in emails):
        result.add("email address requiring review")
    if re.search(r"/Users/[^/\s\"']+|/Volumes/(?!camera-recordings)[^/\s\"']+", text):
        result.add("personal host path")
    if re.search(r"(?<![\w.])192\.168\.\d{1,3}\.\d{1,3}(?![\w.])", text):
        result.add("private network address")
    if re.search(r"(?<!\w)1[3-9]\d{9}(?!\w)", text):
        result.add("phone-like identifier")
    if re.search(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                 r"dckr_pat_[A-Za-z0-9_-]{15,}|-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        result.add("credential pattern")
    return result


def audit():
    tags = []
    next_page = f"https://hub.docker.com/v2/repositories/{REPOSITORY}/tags?page_size=100"
    while next_page:
        page = json.loads(fetch(next_page))
        tags.extend(item["name"] for item in page["results"])
        next_page = page.get("next")
    token = json.loads(fetch("https://auth.docker.io/token?service=registry.docker.io&"
                             f"scope=repository:{REPOSITORY}:pull"))["token"]
    cache = {}
    inspected = set()
    problems = []
    application_layers = set()

    def blob(digest):
        if digest not in cache:
            cache[digest] = fetch(f"{REGISTRY}/blobs/{digest}", token)
        return cache[digest]

    def check(data, location):
        for category in sorted(findings(data)):
            problems.append((location, category))

    def manifest(reference):
        document = json.loads(fetch(f"{REGISTRY}/manifests/{reference}", token, ACCEPT))
        key = json.dumps(document, sort_keys=True)
        if key in inspected:
            return
        inspected.add(key)
        check(key.encode(), f"manifest {reference}")
        for item in document.get("manifests", []):
            manifest(item["digest"])
        if "config" not in document:
            return
        raw_config = blob(document["config"]["digest"])
        check(raw_config, f"config {reference}")
        config = json.loads(raw_config)
        layers = document.get("layers", [])
        for layer in layers:
            if layer.get("mediaType") == "application/vnd.in-toto+json":
                check(blob(layer["digest"]), f"attestation {reference}")
        layer_index = 0
        for entry in config.get("history", []):
            if entry.get("empty_layer"):
                continue
            if "COPY app/" in entry.get("created_by", ""):
                layer = layers[layer_index]
                if layer["size"] > 5_000_000:
                    raise RuntimeError("Application layer unexpectedly exceeds audit size limit")
                application_layers.add(layer["digest"])
                with tarfile.open(fileobj=io.BytesIO(blob(layer["digest"]))) as archive:
                    for member in archive.getmembers():
                        if member.isfile():
                            if member.size > 5_000_000:
                                raise RuntimeError("Unexpected large file in application layer")
                            check(archive.extractfile(member).read(), f"application {reference}")
                            if re.search(r"\.(?:mp4|mov|jpg|jpeg|png|sqlite3|db|log)$", member.name, re.I):
                                problems.append((f"application {reference}", "private data file type"))
            layer_index += 1

    for tag in tags:
        manifest(tag)
    print(f"Audited {len(tags)} tags, {len(inspected)} manifests, {len(application_layers)} application layers")
    print("Scope: public configs, attestations and application COPY layers; not third-party runtime contents")
    for location, category in problems:
        print(f"REVIEW: {category} at {location}")
    if problems:
        raise RuntimeError(f"Public image audit found {len(problems)} items requiring private review")
    print("No matching private data or credential patterns found in the checked image content")


if __name__ == "__main__":
    try:
        audit()
    except RuntimeError as error:
        raise SystemExit(str(error))
