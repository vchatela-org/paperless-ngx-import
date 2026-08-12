#!/usr/bin/env python3
"""Report when a newer stable Python base image exists than the one pinned.

Dependabot is constrained to patch bumps of the base image (see
.github/dependabot.yml), because unconstrained it offers release candidates --
it proposed 3.15.0rc1-alpine while 3.14.7 was still the newest stable. That
constraint costs us the notification when the next minor ships for real, so
this check supplies it: it compares the pinned tag against the newest stable
tag on Docker Hub and fails when it falls behind.

Prereleases are excluded by construction -- a stable Python tag is three
integers and nothing else, so 3.15.0rc1 / 3.15.0b4 / 3.15.0a8 never match.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

DOCKERFILE = "Dockerfile"
VARIANT = "alpine"
TAGS_URL = (
    "https://hub.docker.com/v2/repositories/library/python/tags"
    f"?page_size=100&name=-{VARIANT}"
)
# Only bare X.Y.Z-alpine. Anything carrying a letter in the version (rc, a, b)
# or a pinned Alpine version (-alpine3.24) is deliberately out of scope.
STABLE_TAG = re.compile(rf"^(\d+)\.(\d+)\.(\d+)-{VARIANT}$")
PINNED = re.compile(rf"^FROM python:(\d+\.\d+\.\d+-{VARIANT})@sha256:", re.MULTILINE)
PAGES = 3


def pinned_version() -> tuple[tuple[int, int, int], str]:
    with open(DOCKERFILE, encoding="utf-8") as handle:
        tags = set(PINNED.findall(handle.read()))
    if not tags:
        sys.exit(f"{DOCKERFILE}: no pinned python:X.Y.Z-{VARIANT} FROM line found")
    if len(tags) > 1:
        sys.exit(f"{DOCKERFILE}: build stages disagree on the base image: {sorted(tags)}")
    tag = tags.pop()
    return tuple(int(p) for p in STABLE_TAG.match(tag).groups()), tag  # type: ignore[return-value]


def newest_stable() -> tuple[tuple[int, int, int], str]:
    versions: dict[tuple[int, int, int], str] = {}
    url = TAGS_URL
    for _ in range(PAGES):
        if not url:
            break
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
        for result in payload.get("results", []):
            match = STABLE_TAG.match(result["name"])
            if match:
                versions[tuple(int(p) for p in match.groups())] = result["name"]
        url = payload.get("next")
    if not versions:
        sys.exit("Docker Hub returned no stable python tags; refusing to guess")
    latest = max(versions)
    return latest, versions[latest]


def main() -> int:
    pinned, pinned_tag = pinned_version()
    latest, latest_tag = newest_stable()

    if pinned >= latest:
        print(f"Base image is current: {pinned_tag} (newest stable: {latest_tag})")
        return 0

    kind = "minor" if pinned[:2] != latest[:2] else "patch"
    note = (
        f"Newer stable base image available: {pinned_tag} -> {latest_tag} ({kind}).\n\n"
        f"Dependabot only proposes patch bumps of the base image, so a {kind} "
        "release needs a manual pin (tag + digest) in the Dockerfile."
        if kind == "minor"
        else f"Newer stable base image available: {pinned_tag} -> {latest_tag} ({kind}).\n\n"
        "Dependabot should have opened a PR for this; check that it is not stuck."
    )
    print(note)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Base image out of date\n\n{note}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
