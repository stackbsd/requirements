"""crates.io distfile collector for rust sdists."""

import json
import re
import tarfile
import tomllib
from io import BytesIO
from urllib.request import Request, urlopen

CRATES_SUBDIR = "rust/crates"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
RUST_BACKENDS = {"maturin", "setuptools-rust"}
USER_AGENT = "stackbsd-lock2port"


def crate_url(name, version):
    """Return the crates.io download URL for one crate release."""
    return f"https://static.crates.io/crates/{name}/{name}-{version}.crate"


def crate_file(name, version):
    """Return the distfile path one crate release is fetched to."""
    return f"{CRATES_SUBDIR}/{name}-{version}.crate"


def crate_site(name, version):
    """Return a master site that ignores the appended distfile path."""
    return f"{crate_url(name, version)}?dummy=/"


def load_cache(path):
    """Return the scan cache held at a path."""
    if path.exists():
        return json.loads(path.read_text())
    return {"sdists": {}, "sizes": {}}


def save_cache(path, cache):
    """Write the scan cache to a path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")


def head_size(url):
    """Return the Content-Length of a URL."""
    request = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
    except OSError:
        return None
    return int(length) if length else None


def download(url):
    """Return the bytes served at a URL."""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        return response.read()


def normalized(name):
    """Return a distribution name in PEP 503 comparison form."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def requirement_name(requirement):
    """Return the distribution name a requirement string opens with."""
    return re.split(r"[\s<>=!~;\[(]", requirement, maxsplit=1)[0]


def member_text(tar, path):
    """Return the decoded contents of a tar member."""
    try:
        member = tar.extractfile(path)
    except KeyError:
        return None
    return member.read().decode() if member is not None else None


def sdist_root(tar):
    """Return the single top-level directory an sdist's members sit under."""
    roots = {name.split("/", 1)[0] for name in tar.getnames()}
    return roots.pop() if len(roots) == 1 else None


def builds_with_cargo(tar, root):
    """Return whether an sdist declares a build backend that runs cargo."""
    text = member_text(tar, f"{root}/pyproject.toml")
    if text is None:
        return False
    table = tomllib.loads(text).get("build-system", {})
    names = {normalized(requirement_name(r)) for r in table.get("requires", [])}
    names.add(normalized(table.get("build-backend", "")))
    return bool(names & RUST_BACKENDS)


def lock_members(tar):
    """Return the Cargo.lock members of a tarball that sit beside a Cargo.toml."""
    names = set(tar.getnames())
    return sorted(
        name
        for name in names
        if name.endswith("/Cargo.lock") and name.removesuffix("lock") + "toml" in names
    )


def lock_crates(text, label, errors):
    """Return the crates.io pins of a Cargo.lock."""
    crates = []
    for package in tomllib.loads(text).get("package", []):
        if package.get("source") != REGISTRY:
            continue
        if "checksum" not in package:
            errors.append(
                f"{label}: crate {package['name']} carries no checksum as "
                "the Cargo.lock predates format version 2"
            )
            continue
        crates.append((package["name"], package["version"], package["checksum"]))
    return crates


def scan_sdist(label, url, errors, warnings):
    """Return the Cargo.lock paths and crates an sdist carries."""
    empty = {"locks": [], "crates": []}
    try:
        blob = download(url)
    except OSError as failure:
        errors.append(f"{label}: sdist could not be fetched from {url} ({failure})")
        return None
    try:
        archive = tarfile.open(fileobj=BytesIO(blob))
    except tarfile.ReadError:
        warnings.append(
            f"{label}: sdist is not a tar archive so it is not scanned for rust"
        )
        return empty
    with archive as tar:
        root = sdist_root(tar)
        if root is None:
            warnings.append(
                f"{label}: sdist has no single root directory so it is not scanned"
            )
            return empty
        if not builds_with_cargo(tar, root):
            return empty
        locks = lock_members(tar)
        if not locks:
            errors.append(
                f"{label}: builds with cargo but ships no Cargo.lock beside a "
                "Cargo.toml"
            )
            return empty
        crates = []
        for lock in locks:
            crates.extend(lock_crates(member_text(tar, lock), label, errors))
        paths = [lock.removeprefix(f"{root}/") for lock in locks]
    return {"locks": paths, "crates": crates}


def crate_size(name, version, cache, errors):
    """Return the distfile size of one crate release."""
    key = f"{name}-{version}"
    if key not in cache["sizes"]:
        size = head_size(crate_url(name, version))
        if size is None:
            errors.append(f"crate {key}: crates.io served no Content-Length")
            return None
        cache["sizes"][key] = size
    return cache["sizes"][key]


def collect(sdists, cache, errors, warnings, notes):
    """Return the sorted crates locked by a set of sdists."""
    found = {}
    for label, url, digest in sdists:
        result = cache["sdists"].get(digest)
        if result is None:
            result = scan_sdist(label, url, errors, warnings)
            if result is None:
                continue
            cache["sdists"][digest] = result
        if result["crates"]:
            notes.append(
                f"{label}: {len(result['crates'])} crates locked by "
                f"{', '.join(result['locks'])}"
            )
        for name, version, checksum in result["crates"]:
            found[(name, version)] = checksum
    rows = []
    for (name, version), checksum in sorted(found.items()):
        size = crate_size(name, version, cache, errors)
        if size is not None:
            rows.append((name, version, checksum, size))
    return rows


def sdist_root_of(url):
    """Return the single root directory of the sdist served at a URL."""
    with tarfile.open(fileobj=BytesIO(download(url))) as tar:
        return sdist_root(tar)
