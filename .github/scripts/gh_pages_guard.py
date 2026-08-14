#!/usr/bin/env python3
# *******************************************************************************
# Copyright (c) 2026 Contributors to the Eclipse Foundation
#
# See the NOTICE file(s) distributed with this work for additional
# information regarding copyright ownership.
#
# This program and the accompanying materials are made available under the
# terms of the Apache License Version 2.0 which is available at
# https://www.apache.org/licenses/LICENSE-2.0
#
# SPDX-License-Identifier: Apache-2.0
# *******************************************************************************

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PR_DIR_RE = re.compile(r"^pr-(\d+)$")


@dataclass(frozen=True)
class DirInfo:
    name: str
    size_bytes: int
    last_updated_epoch: int


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        root_path = Path(root)
        for file_name in files:
            file_path = root_path / file_name
            if file_path.is_symlink():
                continue
            total += file_path.stat().st_size
    return total


def _root_size_bytes(root: Path) -> int:
    total = 0
    for child in root.iterdir():
        if child.name == ".git":
            continue
        if child.is_file():
            total += child.stat().st_size
        elif child.is_dir():
            total += _dir_size_bytes(child)
    return total


def _git_last_update_epoch(root: Path, folder_name: str) -> int:
    cmd = ["git", "-C", str(root), "log", "-1", "--format=%ct", "--", folder_name]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        return 0
    value = proc.stdout.strip()
    return int(value) if value.isdigit() else 0


def _pr_dirs(root: Path) -> list[DirInfo]:
    dirs: list[DirInfo] = []
    for child in root.iterdir():
        if child.is_dir() and PR_DIR_RE.match(child.name):
            dirs.append(
                DirInfo(
                    name=child.name,
                    size_bytes=_dir_size_bytes(child),
                    last_updated_epoch=_git_last_update_epoch(root, child.name),
                )
            )
    return dirs


def _load_versions_json(path: Path) -> tuple[list[dict[str, object]], bool]:
    if not path.exists():
        return [], False
    content = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(content, list):
        rows = [row for row in content if isinstance(row, dict)]
        return rows, True
    return [], False


def _remove_versions(versions_path: Path, removed_folders: set[str]) -> None:
    versions, existed = _load_versions_json(versions_path)
    if not existed:
        return
    kept: list[dict[str, object]] = []
    for row in versions:
        version = str(row.get("version", ""))
        url = str(row.get("url", ""))
        if version in removed_folders:
            continue
        if any(f"/{folder}/" in url for folder in removed_folders):
            continue
        kept.append(row)
    versions_path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")


def _remove_index_links(index_path: Path, removed_folders: set[str]) -> None:
    if not index_path.exists():
        return
    text = index_path.read_text(encoding="utf-8")
    for folder in removed_folders:
        text = re.sub(
            rf"(?m)^.*(?:href|src)=['\"][^'\"]*{re.escape(folder)}/[^'\"]*['\"].*\n?",
            "",
            text,
        )
    index_path.write_text(text, encoding="utf-8")


def _human_mb(size_bytes: int) -> str:
    return f"{(size_bytes / (1024 * 1024)):.2f} MiB"


def _emit_outputs(path: Path, values: dict[str, object]) -> None:
    lines = [f"{k}={v}" for k, v in values.items()]
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cmd_prune(args: argparse.Namespace) -> int:
    root = Path(args.gh_pages_dir).resolve()
    max_pr_dirs = args.max_pr_dirs
    max_pr_bytes = args.max_pr_bytes

    dirs = _pr_dirs(root)
    dirs_sorted = sorted(dirs, key=lambda x: (x.last_updated_epoch, x.name))

    removed: list[DirInfo] = []
    kept = dirs_sorted.copy()

    def over_budget() -> bool:
        return len(kept) > max_pr_dirs or sum(d.size_bytes for d in kept) > max_pr_bytes

    while kept and over_budget():
        victim = kept.pop(0)
        target = root / victim.name
        if target.exists():
            shutil.rmtree(target)
        removed.append(victim)

    removed_names = {d.name for d in removed}
    if removed_names:
        _remove_versions(root / "versions.json", removed_names)
        _remove_index_links(root / "index.html", removed_names)

    current_total = _root_size_bytes(root)
    remaining_pr_dirs = _pr_dirs(root)
    current_pr_total = sum(d.size_bytes for d in remaining_pr_dirs)
    print(
        json.dumps(
            {
                "removed": [d.name for d in removed],
                "removed_count": len(removed),
                "removed_bytes": sum(d.size_bytes for d in removed),
                "remaining_pr_count": len(remaining_pr_dirs),
                "remaining_pr_bytes": current_pr_total,
                "current_total_bytes": current_total,
            }
        )
    )

    if args.github_output:
        _emit_outputs(
            Path(args.github_output),
            {
                "removed_count": len(removed),
                "removed_folders": ",".join(sorted(removed_names)),
                "removed_bytes": sum(d.size_bytes for d in removed),
                "remaining_pr_count": len(remaining_pr_dirs),
                "remaining_pr_bytes": current_pr_total,
                "current_total_bytes": current_total,
            },
        )
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    root = Path(args.gh_pages_dir).resolve()
    target_folder = args.target_folder
    new_size = args.new_size_bytes
    hard_limit = args.hard_limit_bytes
    warn_limit = args.warn_limit_bytes

    current_total = _root_size_bytes(root)
    target_path = root / target_folder
    current_target_size = _dir_size_bytes(target_path) if target_path.exists() else 0
    projected_total = current_total - current_target_size + new_size

    pr_rows = sorted(_pr_dirs(root), key=lambda x: x.size_bytes, reverse=True)
    print(f"Current gh-pages size: {_human_mb(current_total)} ({current_total} bytes)")
    print(
        f"Target folder: {target_folder}; current size: {_human_mb(current_target_size)}"
        f"; new size: {_human_mb(new_size)}"
    )
    print(
        f"Projected gh-pages size after publish: {_human_mb(projected_total)}"
        f" ({projected_total} bytes)"
    )
    print("PR preview size breakdown (largest first):")
    for row in pr_rows:
        print(
            f"  - {row.name}: {_human_mb(row.size_bytes)} ({row.size_bytes} bytes),"
            f" last_updated_epoch={row.last_updated_epoch}"
        )

    status = "ok"
    if projected_total > hard_limit:
        status = "fail"
        print(
            f"ERROR: projected gh-pages size exceeds hard limit "
            f"{_human_mb(hard_limit)} ({hard_limit} bytes)."
        )
    elif projected_total > warn_limit:
        status = "warn"
        print(
            f"WARNING: projected gh-pages size exceeds warning threshold "
            f"{_human_mb(warn_limit)} ({warn_limit} bytes)."
        )

    if args.github_output:
        _emit_outputs(
            Path(args.github_output),
            {
                "status": status,
                "current_total_bytes": current_total,
                "current_target_bytes": current_target_size,
                "new_size_bytes": new_size,
                "projected_total_bytes": projected_total,
                "hard_limit_bytes": hard_limit,
                "warn_limit_bytes": warn_limit,
                "pr_breakdown_json": json.dumps(
                    [
                        {
                            "name": r.name,
                            "size_bytes": r.size_bytes,
                            "last_updated_epoch": r.last_updated_epoch,
                        }
                        for r in pr_rows
                    ],
                    separators=(",", ":"),
                ),
            },
        )

    return 1 if status == "fail" else 0


def cmd_check_threshold(args: argparse.Namespace) -> int:
    root = Path(args.gh_pages_dir).resolve()
    warn_limit = args.warn_limit_bytes
    hard_limit = args.hard_limit_bytes
    total = _root_size_bytes(root)

    status = "ok"
    if total > hard_limit:
        status = "hard"
    elif total > warn_limit:
        status = "warn"

    print(json.dumps({"status": status, "total_bytes": total}))
    if args.github_output:
        _emit_outputs(
            Path(args.github_output),
            {
                "status": status,
                "total_bytes": total,
                "warn_limit_bytes": warn_limit,
                "hard_limit_bytes": hard_limit,
            },
        )
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="GitHub Pages quota guard and retention tool.")
    sub = p.add_subparsers(dest="command", required=True)

    prune = sub.add_parser("prune", help="Prune PR preview folders to retention budget.")
    prune.add_argument("--gh-pages-dir", required=True)
    prune.add_argument("--max-pr-dirs", type=int, required=True)
    prune.add_argument("--max-pr-bytes", type=int, required=True)
    prune.add_argument("--github-output", default="")
    prune.set_defaults(func=cmd_prune)

    guard = sub.add_parser("guard", help="Compute projected size and enforce quota.")
    guard.add_argument("--gh-pages-dir", required=True)
    guard.add_argument("--target-folder", required=True)
    guard.add_argument("--new-size-bytes", type=int, required=True)
    guard.add_argument("--hard-limit-bytes", type=int, required=True)
    guard.add_argument("--warn-limit-bytes", type=int, required=True)
    guard.add_argument("--github-output", default="")
    guard.set_defaults(func=cmd_guard)

    threshold = sub.add_parser(
        "check-threshold", help="Check current gh-pages size against warning/hard limits."
    )
    threshold.add_argument("--gh-pages-dir", required=True)
    threshold.add_argument("--warn-limit-bytes", type=int, required=True)
    threshold.add_argument("--hard-limit-bytes", type=int, required=True)
    threshold.add_argument("--github-output", default="")
    threshold.set_defaults(func=cmd_check_threshold)
    return p


def main() -> int:
    args = parser().parse_args()
    if not hasattr(args, "func"):
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
