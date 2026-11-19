#!/usr/bin/env python3
"""Bump app version (minor for Gitea updates, patch otherwise) and update CHANGELOG.md."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bump app version (minor or patch) and update changelog."
    )
    parser.add_argument(
        "--config",
        default="gitea/config.yaml",
        help="Path to config.yaml (default: gitea/config.yaml)",
    )
    parser.add_argument(
        "--changelog",
        default="gitea/CHANGELOG.md",
        help="Path to CHANGELOG.md (default: gitea/CHANGELOG.md)",
    )
    parser.add_argument(
        "--bump-type",
        choices=("auto", "minor", "patch"),
        default="auto",
        help="Version bump type (default: auto = minor iff gitea is updated, else patch)",
    )
    parser.add_argument(
        "--updated-packages",
        default="",
        help='JSON array of updated packages: [{"name": "...", "current": "...", "upstream": "..."}]',
    )
    parser.add_argument(
        "--upstream-version",
        default="",
        help="Upstream Gitea package version (legacy/manual override, e.g. 1.25.5-r0)",
    )
    parser.add_argument(
        "--current-version",
        default="",
        help="Current Gitea package version (used with --upstream-version)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write changes to disk, only print them.",
    )
    return parser.parse_args()


def bump_version(version_str: str, bump_type: str) -> str:
    """Bump semantic version string according to bump_type ('minor' or 'patch')."""
    parts = [int(p) for p in version_str.strip().split(".")]
    if not parts:
        raise ValueError(f"Unable to parse version string: {version_str}")

    if bump_type == "minor":
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1] + 1}.0"
        return f"{parts[0] + 1}.0.0"

    if bump_type == "patch":
        if len(parts) >= 3:
            return f"{parts[0]}.{parts[1]}.{parts[2] + 1}"
        if len(parts) == 2:
            return f"{parts[0]}.{parts[1]}.1"
        return f"{parts[0]}.0.1"

    raise ValueError(f"Unsupported bump_type: {bump_type}")


def resolve_packages(args: argparse.Namespace) -> list[dict[str, str]]:
    """Build normalized list of updated packages from CLI args."""
    if args.updated_packages:
        parsed = json.loads(args.updated_packages)
        if not isinstance(parsed, list):
            raise ValueError("--updated-packages must be a JSON array")
        packages: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict) or not item.get("name") or not item.get("upstream"):
                raise ValueError(f"Invalid package entry: {item!r}")
            packages.append(
                {
                    "name": str(item["name"]),
                    "package": str(item.get("package") or item["name"]),
                    "current": str(item.get("current") or "unknown"),
                    "upstream": str(item["upstream"]),
                }
            )
        packages.sort(key=lambda p: (0 if p["name"] == "gitea" else 1))
        return packages

    if args.upstream_version:
        return [
            {
                "name": "gitea",
                "package": "gitea",
                "current": args.current_version or "unknown",
                "upstream": args.upstream_version,
            }
        ]

    raise ValueError("Either --updated-packages or --upstream-version must be provided.")


def determine_bump_type(packages: list[dict[str, str]], requested: str) -> str:
    """Return 'minor' if and only if gitea is updated (unless explicitly overridden)."""
    if requested in ("minor", "patch"):
        return requested
    gitea_updated = any(p["name"] == "gitea" for p in packages)
    return "minor" if gitea_updated else "patch"


def update_config_file(
    config_path: str, bump_type: str, dry_run: bool = False
) -> tuple[str, str]:
    """Read config.yaml, bump version, and write back."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    match = re.search(r'^(version:\s*["\']?)([^"\'\n]+)(["\']?)', content, re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find 'version:' field in {config_path}")

    current_version = match.group(2)
    new_version = bump_version(current_version, bump_type)

    new_content = re.sub(
        r'^(version:\s*["\']?)[^"\'\n]+(["\']?)',
        rf"\g<1>{new_version}\g<2>",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    if not dry_run:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    return current_version, new_version


def format_changelog_bullets(packages: list[dict[str, str]]) -> list[str]:
    bullets: list[str] = []
    for pkg in packages:
        if pkg["name"] == "gitea":
            bullets.append(f"- Update Gitea to {pkg['upstream']}")
        else:
            bullets.append(f"- Update `{pkg['name']}` to {pkg['upstream']}")
    return bullets


def update_changelog_file(
    changelog_path: str,
    new_addon_version: str,
    packages: list[dict[str, str]],
    dry_run: bool = False,
) -> None:
    """Insert new version entry into CHANGELOG.md."""
    if not os.path.exists(changelog_path):
        raise FileNotFoundError(f"Changelog file not found: {changelog_path}")

    with open(changelog_path, "r", encoding="utf-8") as f:
        content = f.read()

    bullets = "\n".join(format_changelog_bullets(packages))
    new_entry = f"## {new_addon_version}\n\n{bullets}\n"

    # Preserve any top comments (e.g. <!-- ... -->)
    comment_match = re.search(r"^(<!--.*?-->\n*)", content, re.DOTALL)
    if comment_match:
        header = comment_match.group(1).rstrip() + "\n\n"
        rest = content[comment_match.end() :].lstrip()
        new_content = f"{header}{new_entry}\n{rest}"
    else:
        new_content = f"{new_entry}\n{content}"

    if not dry_run:
        with open(changelog_path, "w", encoding="utf-8") as f:
            f.write(new_content)


def build_pr_metadata(
    packages: list[dict[str, str]],
    new_addon_version: str,
    bump_type: str,
) -> tuple[str, str, str]:
    """Generate PR title, commit message, and markdown PR body."""
    gitea_pkg = next((p for p in packages if p["name"] == "gitea"), None)
    other_pkgs = [p for p in packages if p["name"] != "gitea"]

    if gitea_pkg and not other_pkgs:
        pr_title = f"Update Gitea to {gitea_pkg['upstream']}"
        commit_message = f"chore(gitea): update Gitea to {gitea_pkg['upstream']}"
    elif gitea_pkg and other_pkgs:
        pr_title = f"Update Gitea to {gitea_pkg['upstream']} and system packages"
        commit_message = (
            f"chore(gitea): update Gitea to {gitea_pkg['upstream']} and Alpine packages"
        )
    elif len(other_pkgs) == 1:
        pkg = other_pkgs[0]
        pr_title = f"Update {pkg['name']} to {pkg['upstream']}"
        commit_message = f"chore(gitea): update {pkg['name']} to {pkg['upstream']}"
    else:
        names = [p["name"] for p in other_pkgs]
        if len(names) <= 3:
            summary = ", ".join(names)
            pr_title = f"Update Alpine packages ({summary})"
        else:
            summary = ", ".join(names[:3])
            pr_title = f"Update {len(names)} Alpine packages ({summary}, ...)"
        commit_message = f"chore(gitea): update {len(names)} Alpine packages"

    table_lines = [
        "| Package | Current Version | Upstream Version |",
        "| --- | --- | --- |",
    ]
    for pkg in packages:
        pkg_label = (
            f"`{pkg['name']}` (`{pkg['package']}`)"
            if pkg["package"] != pkg["name"]
            else f"`{pkg['name']}`"
        )
        table_lines.append(f"| {pkg_label} | `{pkg['current']}` | `{pkg['upstream']}` |")

    pr_body = "\n".join(
        [
            "## Automated Package Update",
            "",
            "Updated Alpine package(s) were detected for `gitea/Dockerfile`:",
            "",
            *table_lines,
            "",
            "### Changes",
            f"- Bump {bump_type} version to `{new_addon_version}` in `gitea/config.yaml`",
            "- Document package updates in `gitea/CHANGELOG.md`",
            "",
            f"When this pull request is merged to `main`, the build workflow will automatically build and publish the updated Docker image tagged as `{new_addon_version}` and `latest`.",
        ]
    )

    return pr_title, commit_message, pr_body


def write_github_outputs(outputs: dict[str, str]) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return
    with open(github_output, "a", encoding="utf-8") as f:
        for key, value in outputs.items():
            if "\n" in value:
                delimiter = f"EOF_{secrets.token_hex(8)}"
                f.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                f.write(f"{key}={value}\n")


def main() -> int:
    args = parse_args()

    packages = resolve_packages(args)
    if not packages:
        print("No updated packages provided; nothing to bump.", file=sys.stderr)
        return 1

    bump_type = determine_bump_type(packages, args.bump_type)
    current_ver, new_ver = update_config_file(
        args.config, bump_type=bump_type, dry_run=args.dry_run
    )
    print(f"App version bumped ({bump_type}) from {current_ver} -> {new_ver} in {args.config}")

    update_changelog_file(
        args.changelog,
        new_ver,
        packages,
        dry_run=args.dry_run,
    )
    print(f"Updated {args.changelog} with release {new_ver}")

    pr_title, commit_message, pr_body = build_pr_metadata(packages, new_ver, bump_type)
    gitea_pkg = next((p for p in packages if p["name"] == "gitea"), None)
    upstream_gitea = gitea_pkg["upstream"] if gitea_pkg else args.upstream_version

    write_github_outputs(
        {
            "previous_addon_version": current_ver,
            "new_addon_version": new_ver,
            "bump_type": bump_type,
            "upstream_version": upstream_gitea,
            "pr_title": pr_title,
            "commit_message": commit_message,
            "pr_body": pr_body,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
