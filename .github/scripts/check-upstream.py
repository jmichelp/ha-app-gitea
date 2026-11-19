#!/usr/bin/env python3
"""Check upstream Alpine package versions for all packages installed in Dockerfile."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass

APK_PKG_VER_RE = re.compile(r"^(?P<name>.+)-(?P<version>[0-9][^-]*-r[0-9]+)$")
APK_PROVIDER_LINE_RE = re.compile(r"^<(?P<req>[^>]+)>\s+(?P<pkgver>\S+)")
APK_SIM_ONE_RE = re.compile(
    r"^\(\s*\d+/\d+\)\s+(?P<action>Installing|Reinstalling|Purging)\s+"
    r"(?P<name>\S+?)(?:@\S+)?\s+\((?P<version>[^)\s]+)\)"
)
APK_SIM_TWO_RE = re.compile(
    r"^\(\s*\d+/\d+\)\s+(?P<action>Upgrading|Downgrading|Replacing)\s+"
    r"(?P<name>\S+?)(?:@\S+)?\s+\([^)]+\s+->\s+(?P<version>[^)\s]+)\)"
)
PKG_SPEC_NAME_RE = re.compile(r"^([A-Za-z0-9_.+-]+)")

DEFAULT_BASE_IMAGE = "ghcr.io/home-assistant/base:latest"
DEFAULT_DEPLOYED_IMAGES = [
    "ghcr.io/jmichelp/app-gitea:latest",
    "ghcr.io/jmichelp/app-gitea:amd64-latest",
]

APK_GLOBAL_VALUE_FLAGS = {
    "-p",
    "--root",
    "-X",
    "--repository",
    "--keys-dir",
    "--repositories-file",
    "--cache-dir",
    "--arch",
    "--wait",
    "--progress-fd",
    "--cache-max-age",
}
APK_ADD_VALUE_FLAGS = {
    "-t",
    "--virtual",
} | APK_GLOBAL_VALUE_FLAGS


@dataclass
class DockerfileSpec:
    base_image: str
    package_specs: list[str]
    package_names: list[str]
    upgrade: bool


def strip_pkg_spec(spec: str) -> str:
    """Extract the bare Alpine package name from an apk add spec (e.g. 'foo>=1.0@edge' -> 'foo')."""
    spec = spec.strip()
    if spec.startswith("!"):
        return ""
    spec = re.split(r"[@<>=~]", spec, maxsplit=1)[0]
    match = PKG_SPEC_NAME_RE.match(spec)
    return match.group(1) if match else ""


def parse_dockerfile(dockerfile_path: str) -> DockerfileSpec:
    """Parse final build stage of Dockerfile to find base image and installed apk packages."""
    with open(dockerfile_path, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()

    # Join line continuations and drop pure comment lines
    instructions: list[str] = []
    current = ""
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):
            # Comment inside a multiline RUN continuation
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
        else:
            current += stripped
            if current.strip():
                instructions.append(current.strip())
            current = ""
    if current.strip():
        instructions.append(current.strip())

    args: dict[str, str] = {}
    base_image = DEFAULT_BASE_IMAGE
    stage_specs: list[str] = []
    virtual_groups: dict[str, list[str]] = {}
    deleted_names: set[str] = set()
    upgrade = False

    for instr in instructions:
        parts = instr.split(None, 1)
        if len(parts) < 2:
            continue
        keyword, rest = parts[0].upper(), parts[1].strip()

        if keyword == "ARG":
            if "=" in rest:
                key, val = rest.split("=", 1)
                args[key.strip()] = val.strip().strip("\"'")
            continue

        if keyword == "FROM":
            # New build stage resets stage-local packages
            stage_specs = []
            virtual_groups = {}
            deleted_names = set()
            upgrade = False
            tokens = rest.split()
            # Skip optional --platform=...
            idx = 0
            while idx < len(tokens) and tokens[idx].startswith("--"):
                idx += 1
            if idx < len(tokens):
                img = tokens[idx]
                for key, val in args.items():
                    img = img.replace(f"${{{key}}}", val).replace(f"${key}", val)
                if "$" not in img:
                    base_image = img
            continue

        if keyword != "RUN":
            continue

        # Strip BuildKit RUN flags (--mount=..., --network=..., --security=...)
        run_cmd = rest
        while run_cmd.startswith("--"):
            flag_parts = run_cmd.split(None, 1)
            if len(flag_parts) < 2:
                run_cmd = ""
                break
            run_cmd = flag_parts[1].strip()
        if not run_cmd:
            continue

        # Split chained shell commands (&&, ||, ;)
        subcommands = re.split(r"\s*(?:&&|\|\||;)\s*", run_cmd)
        for subcmd in subcommands:
            subcmd = subcmd.strip()
            if not subcmd:
                continue
            try:
                tokens = shlex.split(subcmd)
            except ValueError:
                tokens = subcmd.split()
            if not tokens or tokens[0] != "apk":
                continue

            # Walk global options to find apk subcommand
            i = 1
            while i < len(tokens) and tokens[i].startswith("-"):
                flag = tokens[i]
                if flag in APK_GLOBAL_VALUE_FLAGS and "=" not in flag:
                    i += 2
                else:
                    i += 1
            if i >= len(tokens):
                continue

            sub = tokens[i]
            i += 1

            if sub == "upgrade":
                upgrade = True
                continue

            if sub == "add":
                virtual_name: str | None = None
                added_here: list[str] = []
                while i < len(tokens):
                    tok = tokens[i]
                    if tok in ("-u", "--upgrade"):
                        upgrade = True
                        i += 1
                        continue
                    if tok in ("-t", "--virtual"):
                        if i + 1 < len(tokens):
                            virtual_name = tokens[i + 1]
                        i += 2
                        continue
                    if tok.startswith("--virtual="):
                        virtual_name = tok.split("=", 1)[1]
                        i += 1
                        continue
                    if tok.startswith("-"):
                        if tok in APK_ADD_VALUE_FLAGS and "=" not in tok:
                            i += 2
                        else:
                            i += 1
                        continue
                    # Skip redirections or variable expansions
                    if tok.startswith((">", "<", "$")) or tok.endswith(".apk"):
                        i += 1
                        continue
                    added_here.append(tok)
                    i += 1

                if virtual_name:
                    virtual_groups.setdefault(virtual_name, []).extend(added_here)
                stage_specs.extend(added_here)

            elif sub == "del":
                while i < len(tokens):
                    tok = tokens[i]
                    if tok.startswith("-"):
                        if tok in APK_GLOBAL_VALUE_FLAGS and "=" not in tok:
                            i += 2
                        else:
                            i += 1
                        continue
                    if tok in virtual_groups:
                        for vspec in virtual_groups[tok]:
                            vname = strip_pkg_spec(vspec)
                            if vname:
                                deleted_names.add(vname)
                    else:
                        dname = strip_pkg_spec(tok)
                        if dname:
                            deleted_names.add(dname)
                    i += 1

    # Deduplicate while preserving Dockerfile order, excluding deleted packages
    package_specs: list[str] = []
    package_names: list[str] = []
    seen: set[str] = set()
    for spec in stage_specs:
        name = strip_pkg_spec(spec)
        if not name or name in deleted_names or name in seen:
            continue
        seen.add(name)
        package_specs.append(spec)
        package_names.append(name)

    return DockerfileSpec(
        base_image=base_image,
        package_specs=package_specs,
        package_names=package_names,
        upgrade=upgrade,
    )


def parse_apk_list_installed(text: str) -> dict[str, str]:
    """Parse output of `apk list -I` into {package_name: version}."""
    packages: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split()[0]
        match = APK_PKG_VER_RE.match(token)
        if match:
            packages[match.group("name")] = match.group("version")
    return packages


def parse_apk_providers(text: str) -> dict[str, set[str]]:
    """Parse output of `apk list -P <pkg>` into {requested_name: {concrete_pkg_names}}."""
    providers: dict[str, set[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        match = APK_PROVIDER_LINE_RE.match(line)
        if not match:
            continue
        req = match.group("req")
        pkg_match = APK_PKG_VER_RE.match(match.group("pkgver"))
        if pkg_match:
            providers.setdefault(req, set()).add(pkg_match.group("name"))
    return providers


def apply_apk_simulation(base_installed: dict[str, str], sim_text: str) -> dict[str, str]:
    """Overlay `apk add --simulate` changes onto base_installed package map."""
    expected = dict(base_installed)
    for line in sim_text.splitlines():
        line = line.strip()
        match_two = APK_SIM_TWO_RE.match(line)
        if match_two:
            expected[match_two.group("name")] = match_two.group("version")
            continue
        match_one = APK_SIM_ONE_RE.match(line)
        if match_one:
            action = match_one.group("action")
            name = match_one.group("name")
            if action == "Purging":
                expected.pop(name, None)
            else:
                expected[name] = match_one.group("version")
    return expected


def split_sections(output: str) -> dict[str, str]:
    """Split tagged sections (`===NAME===`) from combined container output."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("===") and stripped.endswith("===") and len(stripped) > 6:
            current = stripped[3:-3]
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def inspect_upstream(spec: DockerfileSpec) -> tuple[dict[str, tuple[str, str]], dict[str, set[str]]]:
    """
    Query the base image and simulate `apk add` to determine exact upstream versions.

    Returns:
        resolved: {dockerfile_pkg_name: (concrete_pkg_name, expected_version)}
        providers: {dockerfile_pkg_name: {possible_concrete_pkg_names}}
    """
    print(f"Pulling base image {spec.base_image}...")
    pull = run_cmd(["docker", "pull", spec.base_image], check=False)
    if pull.returncode != 0:
        raise RuntimeError(f"Failed to pull base image {spec.base_image}: {pull.stderr.strip()}")

    upgrade_flag = "--upgrade " if spec.upgrade else ""
    shell_script = (
        "set -ef\n"
        'echo "===BASE_INSTALLED==="\n'
        "apk list -I\n"
        "apk update >/dev/null 2>&1\n"
        'echo "===PROVIDERS==="\n'
        "for pkg in $PKG_NAMES; do\n"
        '  apk list -P "$pkg" 2>/dev/null || true\n'
        "done\n"
        'echo "===SIMULATE==="\n'
        f'apk add {upgrade_flag}--simulate "$@"\n'
    )

    cmd = [
        "docker",
        "run",
        "--rm",
        "-e",
        f"PKG_NAMES={' '.join(spec.package_names)}",
        "--entrypoint",
        "/bin/sh",
        spec.base_image,
        "-c",
        shell_script,
        "--",
        *spec.package_specs,
    ]
    res = run_cmd(cmd, check=False)
    if res.returncode != 0:
        raise RuntimeError(
            f"Failed to simulate apk package resolution in {spec.base_image}:\n"
            f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        )

    sections = split_sections(res.stdout)
    base_installed = parse_apk_list_installed(sections.get("BASE_INSTALLED", ""))
    providers = parse_apk_providers(sections.get("PROVIDERS", ""))
    expected_all = apply_apk_simulation(base_installed, sections.get("SIMULATE", ""))

    resolved: dict[str, tuple[str, str]] = {}
    unresolved: list[str] = []
    for pkg_name in spec.package_names:
        if pkg_name in expected_all:
            resolved[pkg_name] = (pkg_name, expected_all[pkg_name])
            continue
        provider_matches = sorted(p for p in providers.get(pkg_name, set()) if p in expected_all)
        if provider_matches:
            concrete = provider_matches[0]
            resolved[pkg_name] = (concrete, expected_all[concrete])
        else:
            unresolved.append(pkg_name)

    if unresolved:
        raise RuntimeError(
            f"Could not determine upstream version for Dockerfile packages: {', '.join(unresolved)}"
        )

    return resolved, providers


def inspect_deployed(
    candidate_images: list[str],
    changelog_path: str,
) -> tuple[dict[str, str], str | None, str | None]:
    """
    Inspect the currently deployed Docker image for installed Alpine packages.

    Returns:
        deployed_all: {concrete_pkg_name: version} from `apk list -I` (empty if unavailable)
        gitea_fallback: fallback gitea version from `gitea --version` or CHANGELOG.md if apk unavailable
        pulled_image: image tag that was successfully pulled (or None)
    """
    pulled_image: str | None = None
    for candidate in candidate_images:
        pull = run_cmd(["docker", "pull", candidate], check=False)
        if pull.returncode == 0:
            pulled_image = candidate
            break

    deployed_all: dict[str, str] = {}
    gitea_fallback: str | None = None

    if pulled_image:
        print(f"Inspecting deployed image {pulled_image}...")
        res = run_cmd(
            ["docker", "run", "--rm", "--entrypoint", "/bin/sh", pulled_image, "-c", "apk list -I"],
            check=False,
        )
        if res.returncode == 0:
            deployed_all = parse_apk_list_installed(res.stdout)

        if not deployed_all:
            gitea_res = run_cmd(
                ["docker", "run", "--rm", "--entrypoint", "gitea", pulled_image, "--version"],
                check=False,
            )
            if gitea_res.returncode == 0:
                m = re.search(r"Gitea version\s+(\S+)", gitea_res.stdout)
                if m:
                    gitea_fallback = m.group(1)

    if not deployed_all and not gitea_fallback and os.path.exists(changelog_path):
        print(f"Docker image unavailable, checking {changelog_path} for last Gitea version...")
        with open(changelog_path, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"Update Gitea to\s+([^\s]+)", content)
        if m:
            gitea_fallback = m.group(1)

    return deployed_all, gitea_fallback, pulled_image


def is_version_different(current: str | None, upstream: str) -> bool:
    """Compare deployed and upstream versions (handling non-r fallback versions)."""
    if not current:
        return True
    if current == upstream:
        return False
    # If fallback came from `gitea --version` (no Alpine `-rN` suffix), compare base version
    if "-r" not in current and upstream.split("-r", 1)[0] == current:
        return False
    return True


def compare_packages(
    package_names: list[str],
    upstream_resolved: dict[str, tuple[str, str]],
    providers: dict[str, set[str]],
    deployed_all: dict[str, str],
    gitea_fallback: str | None = None,
) -> tuple[list[dict[str, str]], str, str]:
    """
    Compare expected upstream versions against currently deployed versions.

    Returns:
        updated_packages: list of {"name", "package", "current", "upstream"} (gitea first if updated)
        gitea_upstream: upstream gitea version (or "")
        gitea_current: deployed gitea version (or "unknown")
    """
    updated: list[dict[str, str]] = []
    gitea_upstream = upstream_resolved.get("gitea", ("gitea", ""))[1]
    gitea_current = "unknown"

    if not deployed_all:
        # Degraded fallback: only compare gitea against fallback version
        gitea_current = gitea_fallback or "unknown"
        if gitea_upstream and is_version_different(gitea_fallback, gitea_upstream):
            updated.append(
                {
                    "name": "gitea",
                    "package": "gitea",
                    "current": gitea_current,
                    "upstream": gitea_upstream,
                }
            )
        return updated, gitea_upstream, gitea_current

    for pkg_name in package_names:
        concrete, upstream_ver = upstream_resolved[pkg_name]
        current_ver = deployed_all.get(concrete)
        current_pkg = concrete

        if current_ver is None:
            # Handle provider switch (e.g. a different virtual provider was installed previously)
            for alt in sorted(providers.get(pkg_name, set())):
                if alt in deployed_all:
                    current_ver = deployed_all[alt]
                    current_pkg = alt
                    break

        if pkg_name == "gitea":
            gitea_current = current_ver or "unknown"

        label = f"{pkg_name} (via {concrete})" if concrete != pkg_name else pkg_name
        if is_version_different(current_ver, upstream_ver):
            print(f"  [UPDATE]     {label}: {current_ver or 'not installed'} -> {upstream_ver}")
            updated.append(
                {
                    "name": pkg_name,
                    "package": concrete,
                    "current": current_ver or "not installed",
                    "upstream": upstream_ver,
                }
            )
        else:
            suffix = f" [provider {current_pkg}]" if current_pkg != concrete else ""
            print(f"  [up-to-date] {label}: {current_ver}{suffix}")

    # Put gitea first if updated, preserve Dockerfile order for everything else
    updated.sort(key=lambda item: (0 if item["name"] == "gitea" else 1))
    return updated, gitea_upstream, gitea_current


def write_github_outputs(outputs: dict[str, str]) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as f:
        for key, value in outputs.items():
            f.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check upstream Alpine versions for all packages installed in Dockerfile."
    )
    parser.add_argument(
        "--dockerfile",
        default="gitea/Dockerfile",
        help="Path to Dockerfile (default: gitea/Dockerfile)",
    )
    parser.add_argument(
        "--changelog",
        default="gitea/CHANGELOG.md",
        help="Path to CHANGELOG.md fallback (default: gitea/CHANGELOG.md)",
    )
    parser.add_argument(
        "--base-image",
        default="",
        help="Override base Docker image (default: parsed from Dockerfile BUILD_FROM/FROM)",
    )
    parser.add_argument(
        "--image",
        action="append",
        dest="images",
        help="Candidate deployed Docker image(s) to check (repeatable)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print(f"=== Parsing packages from {args.dockerfile} ===")
    spec = parse_dockerfile(args.dockerfile)
    if args.base_image:
        spec.base_image = args.base_image

    if not spec.package_names:
        print(f"::error::No apk packages found in {args.dockerfile}", file=sys.stderr)
        return 1

    print(f"Base image: {spec.base_image}")
    print(f"Tracked packages ({len(spec.package_names)}): {', '.join(spec.package_names)}")

    print("\n=== Checking Upstream Alpine Package Versions ===")
    upstream_resolved, providers = inspect_upstream(spec)

    print("\n=== Checking Deployed Docker Image Packages ===")
    candidate_images = args.images if args.images else DEFAULT_DEPLOYED_IMAGES
    deployed_all, gitea_fallback, _ = inspect_deployed(candidate_images, args.changelog)

    print("\n=== Version Comparison ===")
    updated_packages, gitea_upstream, gitea_current = compare_packages(
        spec.package_names,
        upstream_resolved,
        providers,
        deployed_all,
        gitea_fallback=gitea_fallback,
    )

    update_available = len(updated_packages) > 0
    gitea_updated = any(p["name"] == "gitea" for p in updated_packages)
    bump_type = ("minor" if gitea_updated else "patch") if update_available else ""

    if update_available:
        names = ", ".join(p["name"] for p in updated_packages)
        print(
            f"\nUpdates available ({len(updated_packages)} package(s): {names}). "
            f"Bump type: {bump_type} (gitea updated: {str(gitea_updated).lower()})."
        )
    else:
        print("\nAll Dockerfile packages are up to date. No update needed.")

    outputs = {
        "update_available": "true" if update_available else "false",
        "bump_type": bump_type,
        "gitea_updated": "true" if gitea_updated else "false",
        "updated_packages": json.dumps(updated_packages, separators=(",", ":")),
        "upstream_version": gitea_upstream,
        "current_version": gitea_current,
    }
    write_github_outputs(outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
