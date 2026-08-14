"""Resolve the newest published tag for each quip image the localdev stack uses.

`latest` is a release-only pointer: CI does not move it for rc builds, so a
localdev stack that pulls `latest` silently runs an old build. This script asks
the registry which tag each image published most recently and writes the
answers as `KEY=VALUE` lines that `make localdev` feeds to
`docker compose --env-file`.

    $ python3 scripts/newest-tags.py data/localdev.tags.env
    QUIP_MINER_TAG=v0.3.0-rc9
    QUIP_VALIDATOR_TAG=v0.2.2-rc6
    QUIP_DASHBOARD_TAG=v0.2.1
    QUIP_FAUCET_TAG=qui1028-rc1

With no path the lines go to stdout instead, which is useful to check what the
registry reports without touching the generated file.

Newest means most recently published, not highest version number. The four
repos do not share a tag scheme — the miner uses `v0.3.0-rcN`, the dashboard
interleaves semver with git SHAs, the faucet has no version line at all — so
publish time is the only ordering that applies to all of them.

The whole resolve costs two GraphQL requests. The REST registry API reports a
publish time only on a per-tag endpoint, which meant one request per tag and a
few hundred requests per run. GraphQL sorts by publish time on the server, so
asking for the newest few tags per image answers the question directly.

Pins win. When a `QUIP_*_TAG` variable is already set in the environment or in
`.env`, this script echoes that value back instead of querying, so the
generated env file never overrides a deliberate pin.

A registry that does not answer must not block a localdev run. On failure this
script falls back to the tags it resolved on the previous run, or to `latest`
when there is no previous run, warns on stderr, and exits 0.

Auth is anonymous, so this needs no `glab`, no docker login, and no token in
`.env`.

Stdlib-only, Python 3.11+.
"""

import datetime as dt
import http.client
import json
import os
import re
import sys
import time
import typing
import urllib.error
import urllib.request

GRAPHQL = "https://gitlab.com/api/graphql"

# Compose variable -> (project path, registry repository path). The cuda miner
# shares QUIP_MINER_TAG with the cpu miner and its tags are published in
# lockstep, so resolving the cpu repo covers both. Every key here is also used
# verbatim as a GraphQL field alias, which the names satisfy because GraphQL
# accepts /[_A-Za-z][_0-9A-Za-z]*/.
IMAGES = {
    "QUIP_MINER_TAG": (
        "quip.network/quip-miner",
        "quip.network/quip-miner/v0.3/quip-miner",
    ),
    "QUIP_VALIDATOR_TAG": (
        "quip.network/quip-validator",
        "quip.network/quip-validator/quip-network-node",
    ),
    "QUIP_DASHBOARD_TAG": (
        "quip.network/dashboard.quip.network",
        "quip.network/dashboard.quip.network",
    ),
    "QUIP_FAUCET_TAG": (
        "quip.network/faucet",
        "quip.network/faucet",
    ),
}

# Single-arch tags published beside every multi-arch manifest. Selecting one
# yields an image that will not run on the other architecture.
ARCH_SUFFIXES = ("-amd64", "-arm64")

# Newest tags to request per image. Only the most recent build matters, but its
# aliases must all land inside the window for the digest grouping below to see
# them. A build publishes about six tags, so 20 leaves room to spare.
TAG_WINDOW = 20

# Retries per request, and the seconds to wait for one. GitLab returns
# intermittent 503s and stalled reads, and a retry budget is cheaper than
# failing a localdev run.
RETRIES = 4
TIMEOUT = 20

# Used when the registry cannot be reached and no previous run left an answer.
# Stale for rc builds, which is the whole reason this script exists, but it
# matches the compose default and so keeps localdev running.
FALLBACK_TAG = "latest"

ENV_FILE = ".env"


class RegistryError(Exception):
    """The registry did not answer a query."""


def warn(message: str) -> None:
    print(f"newest-tags: {message}", file=sys.stderr)


def _graphql(query: str) -> dict:
    """POST a GraphQL query to GitLab and return the `data` object.

    Retries rate limits, server errors, and transport faults. Read timeouts
    surface as a bare TimeoutError rather than a URLError, so catch OSError,
    which covers both, plus the http.client faults that a dropped keep-alive
    connection raises.
    """
    body = json.dumps({"query": query}).encode()
    request = urllib.request.Request(
        GRAPHQL, data=body, headers={"Content-Type": "application/json"}
    )
    last = "unknown error"
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
                payload = json.load(resp)
            if payload.get("errors"):
                # A malformed query fails the same way every time, so do not
                # spend the retry budget on it.
                raise RegistryError(f"GraphQL error: {payload['errors']}")
            return payload["data"]
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code} {exc.reason}"
            if exc.code not in (429, 500, 502, 503, 504):
                raise RegistryError(last) from exc
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < RETRIES - 1:
            time.sleep(2**attempt)
    raise RegistryError(f"{RETRIES} attempts failed, last was {last}")


def repo_ids(wanted: list[str]) -> dict[str, int]:
    """Registry repository id per compose variable, in one query.

    A project holds several registry repositories — buildcache images, the cuda
    variant — so match on the exact repository path rather than taking the
    first.
    """
    fields = " ".join(
        f'{var}: project(fullPath: "{IMAGES[var][0]}") '
        "{ containerRepositories(first: 50) { nodes { id path } } }"
        for var in wanted
    )
    data = _graphql(f"{{ {fields} }}")
    ids: dict[str, int] = {}
    for var in wanted:
        project, repo_path = IMAGES[var]
        node = data.get(var)
        if not node:
            warn(f"{var}: project {project} is not readable")
            continue
        match = [
            n for n in node["containerRepositories"]["nodes"] if n["path"] == repo_path
        ]
        if not match:
            warn(f"{var}: {project} has no registry repository at {repo_path}")
            continue
        ids[var] = int(match[0]["id"].rsplit("/", 1)[-1])
    return ids


class Tag(typing.NamedTuple):
    name: str
    published: dt.datetime
    digest: str


def newest_tags(ids: dict[str, int]) -> dict[str, list[Tag]]:
    """The newest tags per image, in one query, already sorted by publish time."""
    fields = " ".join(
        f'{var}: containerRepository(id: "gid://gitlab/ContainerRepository/{rid}") '
        f"{{ tags(sort: PUBLISHED_AT_DESC, first: {TAG_WINDOW}) "
        "{ nodes { name publishedAt digest } } }"
        for var, rid in ids.items()
    )
    data = _graphql(f"{{ {fields} }}")
    found: dict[str, list[Tag]] = {}
    for var in ids:
        node = data.get(var)
        if not node:
            warn(f"{var}: registry repository is not readable")
            continue
        tags = [
            Tag(n["name"], dt.datetime.fromisoformat(n["publishedAt"]), n["digest"] or "")
            for n in node["tags"]["nodes"]
            if n["publishedAt"] and not n["name"].endswith(ARCH_SUFFIXES)
        ]
        if tags:
            found[var] = tags
        else:
            warn(f"{var}: registry reported no dated multi-arch tags")
    return found


def _name_rank(name: str) -> tuple:
    """Preference among tags that name the same image. Lower sorts first.

    Version tags win, and among them the most specific one: `v0.2.1` beats the
    floating `v0.2`, and a release beats its own prerelease.
    """
    if re.fullmatch(r"v\d[\w.\-]*", name):
        base, _, pre = name.partition("-")
        return (0, -base.count("."), bool(pre), name)
    if name == "latest":
        return (2, 0, False, name)
    if re.fullmatch(r"(sha-)?[0-9a-f]{7,40}", name):
        return (3, 0, False, name)  # sha-1d01b30a — a commit alias
    return (1, 0, False, name)


def pick_newest(tags: list[Tag]) -> str:
    """The best-named tag on the most recently published image.

    CI publishes several tags per build — a version, a `sha-` commit alias, and
    sometimes `latest` — seconds apart. Ordering on publish time alone picks
    whichever won the race, so group by digest first and then prefer the most
    descriptive name pointing at the winning image.
    """
    newest = max(tags, key=lambda t: t.published)
    aliases = [t for t in tags if t.digest and t.digest == newest.digest] or [newest]
    return min(aliases, key=lambda t: _name_rank(t.name)).name


def read_env_file(path: str) -> dict[str, str]:
    """The KEY=VALUE assignments in a compose .env file. Missing file is empty."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return values


def resolve(wanted: list[str]) -> dict[str, str]:
    """Newest tag per variable. Best effort — a missing key means no answer."""
    try:
        ids = repo_ids(wanted)
        if not ids:
            return {}
        return {var: pick_newest(tags) for var, tags in newest_tags(ids).items()}
    except RegistryError as exc:
        warn(f"registry lookup failed: {exc}")
        return {}


def write(path: str | None, lines: list[str]) -> None:
    """Write the env file, replacing it atomically so a crash leaves the old one."""
    text = "".join(f"{line}\n" for line in lines)
    if path is None:
        sys.stdout.write(text)
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    out = argv[1] if len(argv) > 1 else None
    # Read before writing: the previous run's answers are the first fallback if
    # the registry is unreachable now.
    previous = read_env_file(out) if out else {}
    pins = read_env_file(ENV_FILE)

    resolved: dict[str, str] = {}
    wanted: list[str] = []
    for var in IMAGES:
        pinned = os.environ.get(var) or pins.get(var)
        if pinned:
            warn(f"{var} pinned to {pinned}, skipping lookup")
            resolved[var] = pinned
        else:
            wanted.append(var)

    if wanted:
        resolved.update(resolve(wanted))

    for var in wanted:
        if var in resolved:
            continue
        fallback = previous.get(var)
        if fallback:
            warn(f"{var}: unresolved, keeping {fallback} from the previous run")
        else:
            fallback = FALLBACK_TAG
            warn(f"{var}: unresolved and no previous run, falling back to {fallback}")
        resolved[var] = fallback

    write(out, [f"{var}={resolved[var]}" for var in IMAGES])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
