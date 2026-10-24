#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["packaging>=24"]
# ///
"""Generate port distfiles from a uv.lock."""

import argparse
import collections
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

import crates
from packaging.markers import Marker
from packaging.specifiers import SpecifierSet
from packaging.version import Version

UNIVERSAL_WHEEL_SUFFIXES = ("-py3-none-any.whl", "-py2.py3-none-any.whl")
ROOT_SOURCE_KEYS = {"virtual", "editable", "directory"}
FETCHABLE_SOURCE_KEYS = {"registry", "url"}
CLOSURE = "closure"
PATCHED = "patched"
CRATES = "crates"
RUNTIME = "runtime"
SERVICE = "service"


def parse_args(argv):
    """Return parsed command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", default="uv.lock", type=Path)
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="directory that receives one subdirectory of generated files per port",
    )
    parser.add_argument(
        "--ports-tree",
        type=Path,
        help="ports overlay checkout to install the generated files into",
    )
    parser.add_argument(
        "--ports",
        required=True,
        type=Path,
        help="TOML port manifest",
    )
    parser.add_argument("--dist-subdir", default="stackbsd")
    parser.add_argument("--python-full-version", default="3.12.13")
    parser.add_argument("--sys-platforms", default="freebsd15")
    parser.add_argument("--machines", default="amd64,arm64")
    parser.add_argument(
        "--crate-cache",
        default=Path(".cache/crates.json"),
        type=Path,
        help="JSON cache of sdist scans and crate distfile sizes",
    )
    parser.add_argument(
        "--timestamp",
        type=int,
        help="distinfo TIMESTAMP override for reproducible output",
    )
    return parser.parse_args(argv)


def candidate_environments(args):
    """Return the FreeBSD marker environments to project onto."""
    full = args.python_full_version
    short = ".".join(full.split(".")[:2])
    environments = []
    for platform in args.sys_platforms.split(","):
        major = platform.removeprefix("freebsd") or "0"
        for machine in args.machines.split(","):
            environments.append(
                {
                    "os_name": "posix",
                    "sys_platform": platform,
                    "platform_system": "FreeBSD",
                    "platform_release": f"{major}.0-RELEASE",
                    "platform_machine": machine,
                    "platform_python_implementation": "CPython",
                    "implementation_name": "cpython",
                    "python_version": short,
                    "python_full_version": full,
                    "implementation_version": full,
                    "extra": "",
                }
            )
    return environments


def entry_key(package):
    """Return the identity of one locked package entry."""
    return (package["name"], package.get("version"))


def build_index(packages):
    """Return package entries grouped by name."""
    index = {}
    for package in packages:
        index.setdefault(package["name"], []).append(package)
    return index


def edge_targets(edge, index, env):
    """Return the entries a dependency edge resolves to in one environment."""
    targets = []
    for candidate in index.get(edge["name"], []):
        if "version" in edge and candidate.get("version") != edge["version"]:
            continue
        markers = candidate.get("resolution-markers")
        if not markers or any(Marker(m).evaluate(environment=env) for m in markers):
            targets.append(candidate)
    return targets


def roots_of(packages):
    """Return the lock entries standing for the project itself."""
    return [p for p in packages if set(p.get("source", {})) & ROOT_SOURCE_KEYS]


def edge_nodes(edge, index, env):
    """Return the nodes an edge opens in one environment."""
    marker = edge.get("marker")
    if marker and not Marker(marker).evaluate(environment=env):
        return []
    extras = frozenset(edge.get("extra", []))
    return [(target, extras) for target in edge_targets(edge, index, env)]


def walk(seeds, index, env, passthrough=frozenset(), violations=None):
    """Return entry keys reachable from seed nodes in one environment."""
    stack = list(seeds)
    visited = set()
    included = set()
    while stack:
        package, extras = stack.pop()
        node = (entry_key(package), extras)
        if node in visited:
            continue
        visited.add(node)
        through = package["name"] in passthrough
        if not through:
            included.add(entry_key(package))
        edges = list(package.get("dependencies", []))
        optional = package.get("optional-dependencies", {})
        for extra in extras:
            edges.extend(optional.get(extra, []))
        for edge in edges:
            nodes = edge_nodes(edge, index, env)
            if nodes and not through and edge["name"] in passthrough:
                if violations is not None:
                    violations.add((package["name"], edge["name"]))
            stack.extend(nodes)
    return included


def project_version(packages, errors):
    """Return the requirements project version."""
    versions = {root.get("version") for root in roots_of(packages)} - {None}
    if len(versions) != 1:
        errors.append(
            "no single [project] version in the lock. Set version in pyproject.toml."
        )
        return None
    return versions.pop()


def top_level_names(packages):
    """Return the project's top-level requirement names in lock order."""
    names = []
    for root in roots_of(packages):
        for edge in root.get("dependencies", []):
            if edge["name"] not in names:
                names.append(edge["name"])
    return names


def closure_seeds(packages, index, env, seeds):
    """Return the seed nodes for the closure in one environment."""
    nodes = []
    for root in roots_of(packages):
        for edge in root.get("dependencies", []):
            if seeds is not None and edge["name"] not in seeds:
                continue
            nodes.extend(edge_nodes(edge, index, env))
    return nodes


def reachable(
    packages, index, env, seeds=None, passthrough=frozenset(), violations=None
):
    """Return entry keys reachable from the project roots in one environment."""
    nodes = closure_seeds(packages, index, env, seeds)
    return walk(nodes, index, env, passthrough, violations)


def partition(packages, index, environments, seeds, services, violations):
    """Return per-environment closure keys for the runtime and each service."""
    shared = set()
    own = {service: [] for service in services}
    everything = []
    for env in environments:
        base = reachable(packages, index, env, seeds, services, violations)
        closures = {
            service: walk(
                closure_seeds(packages, index, env, {service}),
                index,
                env,
                services - {service},
                violations,
            )
            for service in services
        }
        counts = collections.Counter(k for keys in closures.values() for k in keys)
        shared |= base | {k for k, n in counts.items() if n > 1}
        for service in services:
            own[service].append(closures[service])
        everything.append(base | set(counts))
    owned = set()
    for service in services:
        own[service] = [keys - shared for keys in own[service]]
        owned.update(*own[service])
    return [keys - owned for keys in everything], own


def full_closure_keys(packages, index, environments):
    """Return every entry key reachable from any top-level requirement."""
    keys = set()
    for env in environments:
        keys |= reachable(packages, index, env, None)
    return keys


def reachable_group(packages, index, env, group):
    """Return entry keys reachable from a dependency group in one environment."""
    seeds = []
    for root in roots_of(packages):
        for edge in root.get("dev-dependencies", {}).get(group, []):
            seeds.extend(edge_nodes(edge, index, env))
    return walk(seeds, index, env)


def group_names(packages):
    """Return the dependency groups the project declares in lock order."""
    names = []
    for root in roots_of(packages):
        for name in root.get("dev-dependencies", {}):
            if name not in names:
                names.append(name)
    return names


def projection_sets(packages, index, environments, spec, closures):
    """Return per-environment keys for the port closure and each group."""
    sets = {CLOSURE: closures[spec["label"]]}
    for group in group_names(packages):
        sets[group] = [
            reachable_group(packages, index, env, group) for env in environments
        ]
    return sets


def distfiles_var(name):
    """Return the make variable collecting one projection set's distfiles."""
    return "".join(c if c.isalnum() else "_" for c in name).upper() + "_DISTFILES"


def url_filename(url):
    """Return the decoded final path component of a URL."""
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def universal_wheels(package):
    """Return the package's wheels that install on any platform."""
    return [
        wheel
        for wheel in package.get("wheels", [])
        if url_filename(wheel["url"]).endswith(UNIVERSAL_WHEEL_SUFFIXES)
    ]


def sha256_of(hash_field, name, errors):
    """Return the hex digest of a uv.lock sha256 hash field."""
    algorithm, _, digest = hash_field.partition(":")
    if algorithm != "sha256":
        errors.append(f"{name}: distinfo requires sha256 but the lock has {algorithm}")
    return digest


def pick_artifact(package, errors):
    """Return the fetchable source artifact of one package."""
    name = f"{package['name']}=={package['version']}"
    source_url = package.get("source", {}).get("url")
    sdist = package.get("sdist")
    if sdist:
        artifact = sdist
        kind = "sdist"
    else:
        universal = universal_wheels(package)
        if not universal:
            errors.append(f"{name}: no sdist and no universal wheel on PyPI")
            return None
        artifact = universal[0]
        kind = "wheel"
    url = artifact.get("url") or source_url
    if url is None:
        errors.append(f"{name}: lock entry exposes no fetchable artifact url")
        return None
    if "size" in artifact:
        size = artifact["size"]
    elif source_url:
        size = crates.head_size(url)
        if size is None:
            errors.append(
                f"{name}: url source omits size in the lock and Content-Length "
                f"could not be fetched from {url}"
            )
            return None
    else:
        errors.append(f"{name}: lock entry has no size field. Re-lock with a newer uv.")
        return None
    digest = sha256_of(artifact["hash"], name, errors)
    return (kind, url, digest, size)


def group_name(name, used):
    """Return a unique make-safe fetch group for a package name."""
    group = "".join(c if c.isalnum() else "_" for c in name.lower())
    if not group[0].isalpha():
        group = "g" + group
    base, n = group, 2
    while group in used:
        group = f"{base}_{n}"
        n += 1
    used.add(group)
    return group


def check_lock_python(lock, args, warnings):
    """Warn when the projection python is outside requires-python."""
    spec = lock.get("requires-python")
    if spec and Version(args.python_full_version) not in SpecifierSet(spec):
        warnings.append(
            f"projection python {args.python_full_version} is outside the lock's "
            f"requires-python {spec}"
        )


def project(
    lock,
    environments,
    used_groups,
    errors,
    warnings,
    excluded,
    notes,
    spec,
    closures,
    global_keys,
):
    """Project one port's lock entries into distfile rows."""
    packages = lock["package"]
    index = build_index(packages)
    sets = projection_sets(packages, index, environments, spec, closures)

    rows = []
    scans = []
    seen = {}
    patched_seen = set()
    for package in packages:
        source = package.get("source", {})
        if set(source) & ROOT_SOURCE_KEYS:
            continue
        key = entry_key(package)
        vector = [
            any(key in per_env[i] for per_env in sets.values())
            for i in range(len(environments))
        ]
        if not any(vector):
            if key in global_keys:
                continue  # in another port's closure
            markers = "; ".join(package.get("resolution-markers", [])) or "unreachable"
            excluded.append(f"{package['name']}=={package['version']} [{markers}]")
            continue
        if not set(source) & FETCHABLE_SOURCE_KEYS:
            errors.append(
                f"{package['name']}: source {source} cannot be fetched as a distfile"
            )
            continue
        if not all(vector):
            warnings.append(
                f"{package['name']}: included on {sum(vector)} of {len(vector)} "
                "candidate environments"
            )
        if package["name"] in seen:
            errors.append(
                f"{package['name']}: versions {seen[package['name']]} and "
                f"{package['version']} both in projection"
            )
            continue
        seen[package["name"]] = package["version"]
        artifact = pick_artifact(package, errors)
        if artifact is None:
            continue
        kind, url, digest, size = artifact
        member = [
            name for name, per_env in sets.items() if any(key in inc for inc in per_env)
        ]
        if kind == "wheel":
            notes.append(f"{package['name']}: universal wheel")
        elif not universal_wheels(package):
            label = f"{package['name']}=={package['version']}"
            scans.append((label, url, digest))
        if package["name"] in spec["patched"] and CLOSURE in member:
            patched_seen.add(package["name"])
            if kind == "wheel":
                errors.append(
                    f"{package['name']}: listed as patched but has no sdist. "
                    "Remove it from patched in the manifest."
                )
            else:
                member.append(PATCHED)
        rows.append(
            {
                "name": package["name"],
                "kind": kind,
                "sets": member,
                "group": group_name(package["name"], used_groups),
                "site": url.rsplit("/", 1)[0] + "/",
                "file": url_filename(url),
                "sha256": digest,
                "size": size,
            }
        )
    for name in sorted(spec["patched"] - patched_seen):
        errors.append(f"{spec['label']}: patched package {name} is not in the closure")
    if spec["kind"] == SERVICE:
        if spec["package"] not in seen:
            errors.append(
                f"{spec['label']}: {spec['package']} is not in its own closure"
            )
        else:
            spec["version"] = seen[spec["package"]]
            notes.append(f"service package {spec['package']}=={spec['version']}")
    order = [*sets, PATCHED]
    rows.sort(key=lambda r: (order.index(r["sets"][0]), r["name"], r["file"]))
    return rows, order, scans


def set_counts(rows, order, crate_rows=()):
    """Return the number of distfiles in each set in projection order."""
    counts = {name: 0 for name in order}
    for row in rows:
        for name in row["sets"]:
            counts[name] += 1
    counts[CRATES] = len(crate_rows)
    return {name: count for name, count in counts.items() if count}


def render_makefile(rows, order, crate_rows, args, spec, runtime_version):
    """Return the Makefile.distfiles fragment text."""
    patched = [r["file"] for r in rows if PATCHED in r["sets"]]
    lines = []
    if spec["kind"] == RUNTIME:
        lines.extend([f"DISTVERSION=\t{runtime_version}", ""])
    if args.dist_subdir:
        lines.append(f"DIST_SUBDIR=\t{args.dist_subdir}")
    # extract only the patched sdists as the rest are wheeled from the tarball
    lines.append("EXTRACT_ONLY=\t" + " ".join(patched))
    for name in [*order, CRATES]:
        lines.append(f"{distfiles_var(name)}=")
    lines.append("")
    previous = None
    for row in rows:
        if previous is not None and row["sets"][0] != previous:
            lines.append("")
        previous = row["sets"][0]
        lines.append(f"MASTER_SITES+=\t{row['site']}:{row['group']}")
        lines.append(f"DISTFILES+=\t{row['file']}:{row['group']}")
        for name in row["sets"]:
            lines.append(f"{distfiles_var(name)}+=\t{row['file']}")
    if crate_rows:
        lines.append("")
        lines.append(
            f"# crates for the rust sdists fetched into {crates.CRATES_SUBDIR}/"
        )
    for row in crate_rows:
        lines.append(f"MASTER_SITES+=\t{row['site']}:{row['group']}")
        lines.append(f"DISTFILES+=\t{row['file']}:{row['group']}")
        lines.append(f"{distfiles_var(CRATES)}+=\t{row['file']}")
    return "\n".join(lines) + "\n"


def render_distinfo(rows, crate_rows, args, dist_subdir):
    """Return the distinfo text for every emitted distfile."""
    timestamp = args.timestamp if args.timestamp is not None else int(time.time())
    prefix = f"{dist_subdir}/" if dist_subdir else ""
    lines = [f"TIMESTAMP = {timestamp}"]
    for row in rows + crate_rows:
        lines.append(f"SHA256 ({prefix}{row['file']}) = {row['sha256']}")
        lines.append(f"SIZE ({prefix}{row['file']}) = {row['size']}")
    return "\n".join(lines) + "\n"


def report(rows, order, crate_rows, excluded, notes, warnings, stream, label):
    """Write the projection summary to a stream."""
    sets = ", ".join(
        f"{name} {count}" for name, count in set_counts(rows, order, crate_rows).items()
    )
    total = len(rows) + len(crate_rows)
    print(f"{label}: emitted {total} distfiles ({sets})", file=stream)
    for kind, items in (
        ("not in projection", excluded),
        ("note", notes),
        ("warning", warnings),
    ):
        for item in items:
            print(f"{kind}: {item}", file=stream)


def crate_rows_of(locked, used_groups):
    """Return the distfile rows for a port's locked crates."""
    return [
        {
            "group": group_name(f"crate-{name}-{version}", used_groups),
            "site": crates.crate_site(name, version),
            "file": crates.crate_file(name, version),
            "sha256": checksum,
            "size": size,
        }
        for name, version, checksum, size in locked
    ]


def check_patched_roots(rows, cache, errors):
    """Error when a patched sdist root differs from its filename stem."""
    roots = cache.setdefault("roots", {})
    for row in rows:
        if PATCHED not in row["sets"]:
            continue
        stem = row["file"].removesuffix(".tar.gz")
        if stem == row["file"]:
            errors.append(
                f"{row['name']}: patched sdist {row['file']} is not a .tar.gz"
            )
            continue
        if row["sha256"] not in roots:
            try:
                roots[row["sha256"]] = crates.sdist_root_of(row["site"] + row["file"])
            except OSError as failure:
                errors.append(f"{row['name']}: sdist could not be fetched ({failure})")
                continue
        if roots[row["sha256"]] != stem:
            errors.append(
                f"{row['name']}: sdist root directory {roots[row['sha256']]!r} is "
                f"not {stem!r}"
            )


def project_port(spec, lock, environments, args, closures, global_keys, cache, errors):
    """Return the projection result of one port."""
    warnings, excluded, notes = [], [], []
    used_groups = set()
    rows, order, scans = project(
        lock,
        environments,
        used_groups,
        errors,
        warnings,
        excluded,
        notes,
        spec,
        closures,
        global_keys,
    )
    check_patched_roots(rows, cache, errors)
    locked = crates.collect(scans, cache, errors, warnings, notes)
    return {
        "spec": spec,
        "rows": rows,
        "order": order,
        "crate_rows": crate_rows_of(locked, used_groups),
        "warnings": warnings,
        "excluded": excluded,
        "notes": notes,
    }


def write_port(out_dir, result, args, runtime_version):
    """Render one projected port's generated files."""
    spec = result["spec"]
    out_dir.mkdir(parents=True, exist_ok=True)
    makefile = out_dir / "Makefile.distfiles"
    makefile.write_text(
        render_makefile(
            result["rows"],
            result["order"],
            result["crate_rows"],
            args,
            spec,
            runtime_version,
        )
    )
    distinfo = out_dir / "distinfo"
    distinfo.write_text(
        render_distinfo(result["rows"], result["crate_rows"], args, args.dist_subdir)
    )
    written = [makefile, distinfo]
    report(
        result["rows"],
        result["order"],
        result["crate_rows"],
        result["excluded"],
        result["notes"],
        result["warnings"],
        sys.stderr,
        spec["label"],
    )
    print("wrote " + " and ".join(str(path) for path in written), file=sys.stderr)
    return written


def distfile_lines(text):
    """Return the make lines of a Makefile.distfiles fragment."""
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def update_service_makefile(
    makefile, version, runtime_version, contents_changed, errors
):
    """Rewrite a service Makefile's versions."""
    text = makefile.read_text()
    match = re.search(r"^DISTVERSION=\t(.*)$", text, flags=re.MULTILINE)
    versions = len(re.findall(r"^DISTVERSION=\t", text, flags=re.MULTILINE))
    if versions != 1:
        errors.append(f"{makefile}: expected one DISTVERSION line, found {versions}")
        return False
    updated = text.replace(match.group(0), f"DISTVERSION=\t{version}")
    revision = re.search(r"^PORTREVISION=\t(\d+)\n", updated, flags=re.MULTILINE)
    if match.group(1) != version:
        if revision:
            updated = updated.replace(revision.group(0), "")
    elif contents_changed:
        # bump so pkg reinstalls the service alongside the new runtime
        if revision:
            bumped = int(revision.group(1)) + 1
            updated = updated.replace(revision.group(0), f"PORTREVISION=\t{bumped}\n")
        else:
            updated = updated.replace(
                f"DISTVERSION=\t{version}\n",
                f"DISTVERSION=\t{version}\nPORTREVISION=\t1\n",
            )
    updated, floors = re.subn(
        r"openstack-runtime>=[0-9][0-9.]*",
        f"openstack-runtime>={runtime_version}",
        updated,
    )
    if floors == 0:
        errors.append(f"{makefile}: no openstack-runtime>=<version> dependency")
        return False
    if updated == text:
        return False
    makefile.write_text(updated)
    return True


def install_port(tree, result, written, runtime_version, errors):
    """Install a port's generated files into the ports tree."""
    spec = result["spec"]
    port_dir = tree / "net" / spec["label"]
    if not port_dir.is_dir():
        print(f"{spec['label']}: no port at {port_dir}, skipped", file=sys.stderr)
        return
    contents_changed = False
    old = port_dir / "Makefile.distfiles"
    for path in written:
        if path.name == old.name and old.is_file():
            contents_changed = distfile_lines(old.read_text()) != distfile_lines(
                path.read_text()
            )
        shutil.copyfile(path, port_dir / path.name)
    if spec["kind"] == SERVICE:
        makefile = port_dir / "Makefile"
        if not makefile.is_file():
            errors.append(f"{spec['label']}: {makefile} is missing")
            return
        if update_service_makefile(
            makefile, spec["version"], runtime_version, contents_changed, errors
        ):
            print(f"updated {makefile}", file=sys.stderr)
    print(f"installed {spec['label']} into {port_dir}", file=sys.stderr)


def load_manifest(path, available, errors):
    """Return the port specs the manifest describes with the runtime first."""
    manifest = tomllib.loads(path.read_text())
    runtime = None
    services = []
    for label, cfg in manifest.items():
        if cfg.get("runtime"):
            if runtime is not None:
                errors.append(
                    f"port {label}: {runtime['label']} is already the runtime"
                )
                continue
            runtime = {
                "label": label,
                "kind": RUNTIME,
                "package": None,
                "seeds": [crates.normalized(s) for s in cfg.get("seeds", [])],
                "patched": {crates.normalized(s) for s in cfg.get("patched", [])},
            }
        elif "package" in cfg:
            services.append(
                {
                    "label": label,
                    "kind": SERVICE,
                    "package": crates.normalized(cfg["package"]),
                    "seeds": [],
                    "patched": {crates.normalized(s) for s in cfg.get("patched", [])},
                }
            )
        else:
            errors.append(
                f"port {label}: set runtime = true or name the package it ships"
            )
    if runtime is None:
        errors.append(f"{path}: no stanza sets runtime = true")
        return []
    claims = [(seed, runtime["label"]) for seed in runtime["seeds"]]
    claims += [(spec["package"], spec["label"]) for spec in services]
    owner = {}
    for name, label in claims:
        if name not in available:
            errors.append(
                f"port {label}: {name} is not a top-level requirement in the lock; "
                "add it to pyproject.toml or fix the manifest"
            )
        elif name in owner:
            errors.append(f"{name}: claimed by both {owner[name]} and {label}")
        owner[name] = label
    for name in available:
        if name not in owner:
            errors.append(
                f"top-level requirement {name} is claimed by no port; make it a "
                "service package or a runtime seed"
            )
    return [runtime, *services]


def main(argv=None):
    """Generate the ports' distfile metadata from a uv.lock."""
    args = parse_args(argv)
    lock = tomllib.loads(args.lock.read_text())
    environments = candidate_environments(args)
    index = build_index(lock["package"])

    errors, warnings = [], []
    check_lock_python(lock, args, warnings)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    available = set(top_level_names(lock["package"]))
    runtime_version = project_version(lock["package"], errors)
    specs = load_manifest(args.ports, available, errors)
    services = {spec["package"] for spec in specs if spec["kind"] == SERVICE}
    global_keys = full_closure_keys(lock["package"], index, environments)

    plan = []
    cache = crates.load_cache(args.crate_cache)
    if not errors:
        violations = set()
        runtime, own = partition(
            lock["package"],
            index,
            environments,
            specs[0]["seeds"],
            services,
            violations,
        )
        for source, target in sorted(violations):
            errors.append(
                f"{source} depends on service package {target}. "
                f"Make {target} a runtime seed instead of a service."
            )
        closures = {specs[0]["label"]: runtime}
        closures.update({spec["label"]: own[spec["package"]] for spec in specs[1:]})
    if not errors:
        for spec in specs:
            result = project_port(
                spec, lock, environments, args, closures, global_keys, cache, errors
            )
            plan.append((args.out / spec["label"], result))
    crates.save_cache(args.crate_cache, cache)

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    for out_dir, result in plan:
        written = write_port(out_dir, result, args, runtime_version)
        if args.ports_tree is not None:
            install_port(args.ports_tree, result, written, runtime_version, errors)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
