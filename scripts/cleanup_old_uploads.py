#!/usr/bin/env python3

"""Free remote storage by deleting the oldest eligible upload directories."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass


class CleanupError(Exception):
    """Raised when remote information cannot be safely interpreted."""


@dataclass(frozen=True)
class RemoteUsage:
    used: int
    total: int
    free: int
    trashed: int

    @property
    def occupied(self) -> int:
        return self.total - self.free


@dataclass(frozen=True)
class IssueUploadDirectory:
    issue_number: int
    relative_path: str


ISSUE_DIRECTORY_RE = re.compile(r"^DockerPull/([0-9]+)(?:/.*)?$")


def run_rclone(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["rclone", *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise CleanupError("Could not start rclone.") from exc
    if result.returncode != 0:
        # Do not echo subprocess output: it may contain sensitive remote details.
        raise CleanupError(f"rclone {args[0]} failed with exit code {result.returncode}.")
    return result.stdout


def get_remote_usage(remote_root: str) -> RemoteUsage:
    return parse_usage(run_rclone(["about", "--json", remote_root]))


def parse_usage(output: str) -> RemoteUsage:
    try:
        data = json.loads(output)
        used = data["used"]
        total = data["total"]
        free = data["free"]
        trashed = data.get("trashed", 0)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CleanupError("rclone about did not return valid used/total/free quota values.") from exc
    if (
        isinstance(used, bool)
        or isinstance(total, bool)
        or isinstance(free, bool)
        or isinstance(trashed, bool)
        or not isinstance(used, (int, float))
        or not isinstance(total, (int, float))
        or not isinstance(free, (int, float))
        or not isinstance(trashed, (int, float))
        or not math.isfinite(used)
        or not math.isfinite(total)
        or not math.isfinite(free)
        or not math.isfinite(trashed)
        or used < 0
        or total <= 0
        or free < 0
        or free > total
        or trashed < 0
    ):
        raise CleanupError("Remote quota values are missing or invalid; refusing to delete files.")
    return RemoteUsage(
        used=int(used),
        total=int(total),
        free=int(free),
        trashed=int(trashed),
    )


def list_issue_uploads(output: str) -> list[IssueUploadDirectory]:
    candidates: dict[str, IssueUploadDirectory] = {}
    for line in output.splitlines():
        relative_path = line.strip().rstrip("/")
        match = ISSUE_DIRECTORY_RE.fullmatch(relative_path)
        if not match:
            continue
        issue_number = int(match.group(1))
        issue_directory = f"DockerPull/{issue_number}"
        candidates[issue_directory] = IssueUploadDirectory(issue_number, issue_directory)
    return sorted(candidates.values(), key=lambda item: (item.issue_number, item.relative_path))


def select_oldest_directories(
    candidates: list[IssueUploadDirectory],
    bytes_to_free: int,
    get_size: Callable[[IssueUploadDirectory], int],
) -> list[tuple[IssueUploadDirectory, int]]:
    selected: list[tuple[IssueUploadDirectory, int]] = []
    reclaimed = 0
    for candidate in sorted(candidates, key=lambda item: (item.issue_number, item.relative_path)):
        size = get_size(candidate)
        if size < 0:
            raise CleanupError(f"Could not determine size for {candidate.relative_path}.")
        if size == 0:
            continue
        selected.append((candidate, size))
        reclaimed += size
        if reclaimed >= bytes_to_free:
            break
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default="E5", help="rclone remote name (default: E5)")
    parser.add_argument(
        "--threshold", type=float, default=0.60,
        help="start cleanup when occupied quota exceeds this fraction (default: 0.60)",
    )
    parser.add_argument(
        "--target", type=float, default=0.60,
        help="delete oldest issue directories until occupied quota reaches this fraction (default: 0.60)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete selected directories; without this flag, only report the plan",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.remote):
        raise CleanupError("Invalid rclone remote name.")
    if not (0 <= args.target <= args.threshold <= 1):
        raise CleanupError("Expected 0 <= target <= threshold <= 1.")

    remote_root = f"{args.remote}:"
    usage = get_remote_usage(remote_root)
    used_fraction = usage.occupied / usage.total
    print(
        f"Remote quota occupied: {usage.occupied} / {usage.total} bytes "
        f"({used_fraction:.1%}; used={usage.used}, trashed={usage.trashed}); "
        f"cleanup threshold: {args.threshold:.1%}."
    )
    if used_fraction <= args.threshold:
        print("Below threshold; nothing to clean.")
        return 0

    listing = run_rclone(["lsf", "--recursive", "--dirs-only", remote_root])
    candidates = list_issue_uploads(listing)
    bytes_to_free = max(0, usage.occupied - int(usage.total * args.target))

    def get_directory_size(candidate: IssueUploadDirectory) -> int:
        output = run_rclone(["size", "--json", f"{remote_root}{candidate.relative_path}"])
        try:
            size_data = json.loads(output)
            size = size_data["bytes"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CleanupError(
                f"Could not determine size for {candidate.relative_path}; refusing to delete files."
            ) from exc
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CleanupError(
                f"Invalid size for {candidate.relative_path}; refusing to delete files."
            )

        return size

    if not candidates:
        raise CleanupError(
            "Quota is above the cleanup threshold, but no issue upload directories are available."
        )

    if not args.apply:
        selected_with_sizes = select_oldest_directories(
            candidates, bytes_to_free, get_directory_size
        )
        if not selected_with_sizes:
            raise CleanupError(
                "Quota is above the cleanup threshold, but no non-empty issue uploads are available."
            )
        estimated_reclaimed = sum(size for _, size in selected_with_sizes)
        for candidate, size in selected_with_sizes:
            print(
                f"Would permanently delete issue #{candidate.issue_number}: "
                f"{candidate.relative_path} ({size} bytes)"
            )
        if estimated_reclaimed < bytes_to_free:
            print(
                f"Warning: eligible issue folders contain {estimated_reclaimed} bytes, "
                f"less than the estimated {bytes_to_free} bytes needed to reach the target."
            )
        print(
            f"Dry run: {len(selected_with_sizes)} issue folder(s), "
            f"estimated {estimated_reclaimed} bytes. Rerun with --apply to delete."
        )
        return 0

    target_occupied = int(usage.total * args.target)
    current_usage = usage
    estimated_occupied = usage.occupied
    deleted_count = 0
    for candidate in candidates:
        if estimated_occupied <= target_occupied:
            break
        size = get_directory_size(candidate)
        if size == 0:
            continue
        print(
            f"Permanently deleting issue #{candidate.issue_number}: "
            f"{candidate.relative_path} ({size} bytes)"
        )
        run_rclone(
            [
                "purge",
                "--onedrive-hard-delete",
                f"{remote_root}{candidate.relative_path}",
            ]
        )
        deleted_count += 1
        # Quota reporting can lag behind a successful hard delete. Account for
        # the deleted directory immediately so a stale `about` result does not
        # cause the loop to erase every remaining issue folder.
        estimated_occupied = max(0, estimated_occupied - size)
        current_usage = get_remote_usage(remote_root)
        estimated_occupied = min(estimated_occupied, current_usage.occupied)
        print(
            f"Quota after deletion: reported {current_usage.occupied} / {current_usage.total} bytes "
            f"({current_usage.occupied / current_usage.total:.1%}); "
            f"estimated {estimated_occupied} bytes occupied from deleted folder sizes."
        )

    if deleted_count == 0:
        raise CleanupError(
            "Quota is above the cleanup threshold, but no non-empty issue uploads were deleted."
        )
    if estimated_occupied > target_occupied:
        raise CleanupError(
            f"Estimated quota remains above the {args.target:.1%} target after deleting "
            "the oldest available issue folders; "
            "stopping before the new upload."
        )

    if current_usage.occupied <= target_occupied:
        print(
            f"Cleanup finished. Remote quota now reports {current_usage.occupied} / "
            f"{current_usage.total} bytes ({current_usage.occupied / current_usage.total:.1%}; "
            f"used={current_usage.used}, trashed={current_usage.trashed})."
        )
    else:
        print(
            f"Cleanup finished based on permanent-delete size estimates: approximately "
            f"{estimated_occupied} / {usage.total} bytes ({estimated_occupied / usage.total:.1%}) "
            f"occupied. rclone still reports {current_usage.occupied} bytes; quota reporting "
            "has not caught up. Proceeding without polling or deleting more issue folders."
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CleanupError as exc:
        print(f"Cleanup failed safely: {exc}", file=sys.stderr)
        sys.exit(1)