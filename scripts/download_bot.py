#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


class WorkflowError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class DockerPlan:
    command: str
    image_name: str
    options: list[str]


@dataclass
class DownloadPlan:
    command: str
    tool: str
    url: str
    flags: list[str]


ALLOWED_DOCKER_FLAGS = {
    "-a",
    "--all-tags",
    "--disable-content-trust",
    "-q",
    "--quiet",
}

BLOCKED_DOWNLOAD_OPTIONS = {
    "curl": {
        "-o",
        "--output",
        "-O",
        "--remote-name",
        "-J",
        "--remote-header-name",
        "-T",
        "--upload-file",
        "-d",
        "--data",
        "--data-ascii",
        "--data-binary",
        "--data-raw",
        "--data-urlencode",
        "-F",
        "--form",
        "-I",
        "--head",
        "-X",
        "--request",
        "-K",
        "--config",
        "-u",
        "--user",
        "-b",
        "--cookie",
        "-c",
        "--cookie-jar",
        "--next",
        "-Z",
        "--parallel",
        "--parallel-immediate",
        "--parallel-max",
        "-w",
        "--write-out",
    },
    "wget": {
        "-O",
        "--output-document",
        "-i",
        "--input-file",
        "--post-data",
        "--post-file",
        "--method",
        "--body-data",
        "--body-file",
        "--save-headers",
        "--spider",
        "-m",
        "--mirror",
        "-r",
        "--recursive",
        "-p",
        "--page-requisites",
        "-k",
        "--convert-links",
        "-E",
        "--adjust-extension",
    },
}

DOWNLOAD_OPTIONS_REQUIRING_VALUE = {
    "curl": {
        "-A",
        "--user-agent",
        "-e",
        "--referer",
        "-H",
        "--header",
        "-m",
        "--max-time",
        "--retry",
        "--retry-delay",
        "--connect-timeout",
        "--speed-limit",
        "--speed-time",
        "--limit-rate",
        "--proto",
        "--proto-redir",
        "--tls-max",
        "-Y",
        "-y",
        "-C",
        "--continue-at",
        "--url",
    },
    "wget": {
        "-t",
        "--tries",
        "-T",
        "--timeout",
        "--waitretry",
        "--dns-timeout",
        "--connect-timeout",
        "--read-timeout",
        "--limit-rate",
        "--user-agent",
        "--referer",
        "--header",
        "--secure-protocol",
        "-e",
        "--execute",
    },
}

ALLOWED_CURL_BOOLEAN_FLAGS = {
    "-s",
    "-S",
    "-L",
    "-k",
    "-f",
    "-g",
    "-v",
    "--compressed",
}

ALLOWED_WGET_BOOLEAN_FLAGS = {
    "-q",
    "-c",
    "-nv",
    "-N",
    "--content-disposition",
    "--trust-server-names",
    "--retry-connrefused",
    "--no-check-certificate",
    "--inet4-only",
    "--inet6-only",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Docker or single-file download jobs from an issue title.")
    parser.add_argument("--issue-title", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--rclone-remote", default="E5")
    parser.add_argument("--disk-reserve-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def post_issue_comment(repo: str, issue_number: int, body: str, *, allow_failure: bool = False) -> None:
    cmd = ["gh", "issue", "comment", str(issue_number), "-R", repo, "-b", body]
    try:
        subprocess.run(cmd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        if allow_failure:
            stderr = (exc.stderr or "").strip()
            print(f"warning: failed to post issue comment: {stderr}", file=sys.stderr)
            return
        raise


def run_command(cmd: Sequence[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd),
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        output = stderr or stdout or "No command output was captured."
        raise WorkflowError(
            "❌ Task failed.\n\n"
            f"**Command:** `{shlex.join(list(cmd))}`\n"
            f"**Exit code:** `{exc.returncode}`\n\n"
            "```\n"
            f"{truncate_text(output)}\n"
            "```"
        ) from exc


def truncate_text(text: str, limit: int = 1500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def available_disk_budget(reserve_bytes: int) -> int:
    free_bytes = shutil.disk_usage(Path.cwd()).free
    budget = free_bytes - reserve_bytes
    if budget <= 0:
        raise WorkflowError("❌ The runner does not have enough free disk space to start this task.")
    return budget


def parse_issue_title(issue_title: str) -> DockerPlan | DownloadPlan:
    patterns = {
        "docker": r"^docker\ pull\ (.+)$",
        "wget": r"^wget\ (.+)$",
        "curl": r"^curl\ (.+)$",
    }
    for command, pattern in patterns.items():
        match = re.match(pattern, issue_title)
        if match:
            raw = match.group(1)
            if command == "docker":
                return parse_docker_plan(raw)
            return parse_download_plan(command, raw)
    raise WorkflowError(
        "❌ Invalid issue title format.\n\n"
        "Please use one of the following formats:\n"
        "- `docker pull <image>`\n"
        "- `wget <URL>`\n"
        "- `curl <URL>`"
    )


def parse_docker_plan(raw: str) -> DockerPlan:
    try:
        args = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise WorkflowError(f"❌ Failed to parse the docker command: {exc}") from exc

    if not args:
        raise WorkflowError("❌ No image name was provided.")

    image_name = ""
    options: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in ALLOWED_DOCKER_FLAGS:
            options.append(token)
            index += 1
            continue
        if token.startswith("--platform="):
            options.append(token)
            index += 1
            continue
        if token == "--platform":
            if index + 1 >= len(args):
                raise WorkflowError("❌ `docker pull --platform` is missing a platform value.")
            options.extend([token, args[index + 1]])
            index += 2
            continue
        if token.startswith("-"):
            raise WorkflowError(f"❌ Unsupported docker pull option: `{token}`.")
        if image_name:
            raise WorkflowError(f"❌ Unexpected extra docker argument: `{token}`.")
        image_name = token
        index += 1

    if not image_name:
        raise WorkflowError("❌ No image name was provided.")

    return DockerPlan(command="docker", image_name=image_name, options=options)


def parse_download_plan(tool: str, raw: str) -> DownloadPlan:
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise WorkflowError(f"❌ Failed to parse the download command: {exc}") from exc

    if not tokens:
        raise WorkflowError("❌ No download URL was provided.")

    flags: list[str] = []
    pending_option = ""
    target_url = ""
    index = 0
    while index < len(tokens):
        token = tokens[index]

        if pending_option:
            if pending_option == "--url":
                if target_url:
                    raise WorkflowError("❌ Only one download URL is supported per issue.")
                target_url = token
            else:
                flags.extend([pending_option, token])
            pending_option = ""
            index += 1
            continue

        if is_http_url(token):
            if target_url:
                raise WorkflowError("❌ Only one download URL is supported per issue.")
            target_url = token
            index += 1
            continue

        if token == "--url":
            if tool != "curl":
                raise WorkflowError("❌ The option `--url` is only supported for curl commands.")
            pending_option = token
            index += 1
            continue

        if token.startswith("--url="):
            if tool != "curl":
                raise WorkflowError("❌ The option `--url` is only supported for curl commands.")
            if target_url:
                raise WorkflowError("❌ Only one download URL is supported per issue.")
            target_url = token.split("=", 1)[1]
            index += 1
            continue

        if token.startswith("--") and "=" in token:
            option_name = token.split("=", 1)[0]
            if is_blocked_option(tool, option_name):
                raise WorkflowError(
                    f"❌ The option `{option_name}` is not supported in issue commands.\n\n"
                    "Use a download command that fetches exactly one file."
                )
            if option_name in DOWNLOAD_OPTIONS_REQUIRING_VALUE[tool] or option_name in allowed_boolean_options(tool):
                flags.append(token)
                index += 1
                continue
            raise WorkflowError(f"❌ Unsupported {tool} option: `{option_name}`.")

        if not token.startswith("-"):
            raise WorkflowError(
                "❌ Found an unexpected positional argument.\n\n"
                f"**Argument:** `{token}`"
            )

        if is_blocked_option(tool, token):
            raise WorkflowError(
                f"❌ The option `{token}` is not supported in issue commands.\n\n"
                "Use a download command that fetches exactly one file."
            )

        if token in DOWNLOAD_OPTIONS_REQUIRING_VALUE[tool]:
            pending_option = token
            index += 1
            continue

        if tool == "curl":
            flags.extend(parse_curl_short_or_boolean(token))
        else:
            flags.extend(parse_wget_short_or_boolean(token))
        index += 1

    if pending_option:
        raise WorkflowError(f"❌ The option `{pending_option}` requires a value.")

    if not target_url:
        raise WorkflowError(
            "❌ No valid http/https download URL was found.\n\n"
            f"**Input:** `{raw}`"
        )

    if not is_http_url(target_url):
        raise WorkflowError(
            "❌ Only http/https download URLs are supported.\n\n"
            f"**Input:** `{target_url}`"
        )

    return DownloadPlan(command=tool, tool=tool, url=target_url, flags=flags)


def parse_curl_short_or_boolean(token: str) -> list[str]:
    if token.startswith("--"):
        if token in ALLOWED_CURL_BOOLEAN_FLAGS:
            return [token]
        raise WorkflowError(
            f"❌ Unsupported curl option: `{token}`.\n\n"
            "Common download flags such as `-sSL`, `-H`, `-A`, `--retry`, and `--max-time` are supported."
        )

    remainder = token[1:]
    parsed: list[str] = []
    while remainder:
        option = f"-{remainder[0]}"
        remainder = remainder[1:]
        if is_blocked_option("curl", option):
            raise WorkflowError(f"❌ The option `{option}` is not supported in issue commands.")
        if option in DOWNLOAD_OPTIONS_REQUIRING_VALUE["curl"]:
            if remainder:
                parsed.extend([option, remainder])
                return parsed
            parsed.append(option)
            return parsed
        if option not in ALLOWED_CURL_BOOLEAN_FLAGS:
            raise WorkflowError(
                f"❌ Unsupported curl option or option combination: `{token}`.\n\n"
                "Common download flags such as `-sSL`, `-H`, `-A`, `--retry`, and `--max-time` are supported."
            )
        parsed.append(option)
    return parsed


def parse_wget_short_or_boolean(token: str) -> list[str]:
    if token in ALLOWED_WGET_BOOLEAN_FLAGS:
        return [token]
    if token.startswith("-t") and token != "-t":
        return ["-t", token[2:]]
    if token.startswith("-T") and token != "-T":
        return ["-T", token[2:]]
    if token.startswith("--"):
        raise WorkflowError(
            f"❌ Unsupported wget option: `{token}`.\n\n"
            "Common download flags such as `-q`, `-c`, `-nv`, `--header`, `--referer`, and `--user-agent` are supported."
        )
    raise WorkflowError(
        f"❌ Unsupported wget option or option combination: `{token}`.\n\n"
        "Common download flags such as `-q`, `-c`, `-nv`, `--header`, `--referer`, and `--user-agent` are supported."
    )


def is_blocked_option(tool: str, option: str) -> bool:
    return option in BLOCKED_DOWNLOAD_OPTIONS[tool]


def allowed_boolean_options(tool: str) -> set[str]:
    return ALLOWED_CURL_BOOLEAN_FLAGS if tool == "curl" else ALLOWED_WGET_BOOLEAN_FLAGS


def is_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value))


def get_remote_headers(url: str) -> str:
    result = run_command(
        ["curl", "-fsSLI", "--retry", "3", "--retry-delay", "5", "--connect-timeout", "20", url],
        capture_output=True,
    )
    return result.stdout


def get_last_header_value(headers: str, name: str) -> str:
    last_value = ""
    pattern = re.compile(rf"^{re.escape(name)}:\s*(.+)$", re.IGNORECASE)
    for line in headers.splitlines():
        match = pattern.match(line.strip("\r"))
        if match:
            last_value = match.group(1).strip()
    return last_value


def infer_filename(url: str, headers: str) -> str:
    content_disposition = get_last_header_value(headers, "content-disposition")
    filename_candidate = ""
    if content_disposition:
        match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.IGNORECASE)
        if match:
            filename_candidate = unquote(match.group(1))
        else:
            match = re.search(r'filename="?([^";]+)', content_disposition, flags=re.IGNORECASE)
            if match:
                filename_candidate = match.group(1)

    if not filename_candidate:
        path = urlparse(url).path
        filename_candidate = Path(path).name

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename_candidate or "")
    if not safe_name or safe_name in {".", ".."} or re.fullmatch(r"_+", safe_name):
        safe_name = f"download_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return safe_name


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{num_bytes}B"


def ensure_nonempty_file(file_path: Path) -> int:
    size = file_path.stat().st_size
    if size == 0:
        raise WorkflowError("❌ The downloaded file is empty, so the upload was cancelled.")
    return size


def upload_and_link(local_path: Path, remote: str, issue_number: int, prefix: str) -> str:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    upload_dir = f"{prefix}/{issue_number}/{timestamp}"
    print(f"Starting upload to {remote}:{upload_dir}/{local_path.name}")
    run_command(["rclone", "copy", "--stats=1m", "--stats-one-line", str(local_path), f"{remote}:{upload_dir}/"])
    print("Upload completed, generating share link...")
    result = run_command(["rclone", "link", f"{remote}:{upload_dir}/{local_path.name}"], capture_output=True)
    return result.stdout.strip()


def run_docker_plan(plan: DockerPlan, *, issue_number: int, remote: str) -> str:
    print(f"Pulling image: {plan.image_name}")
    run_command(["docker", "pull", *plan.options, plan.image_name])

    output_name = re.sub(r"[:/ ]", "_", plan.image_name) + ".tar.gz"
    output_path = Path(output_name)
    print(f"Saving image to {output_path.name}...")
    with output_path.open("wb") as raw_file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_file) as gzip_file:
            proc = subprocess.Popen(["docker", "save", plan.image_name], stdout=subprocess.PIPE)
            assert proc.stdout is not None
            shutil.copyfileobj(proc.stdout, gzip_file)
            proc.stdout.close()
            return_code = proc.wait()
    if return_code != 0:
        raise WorkflowError("❌ Failed to export the Docker image archive.")

    share_link = upload_and_link(output_path, remote, issue_number, "DockerPull")
    return (
        "✅ Docker image download completed.\n\n"
        f"**Image:** `{plan.image_name}`\n\n"
        "**Download link:**\n"
        f"{share_link}\n\n"
        f"> After downloading, import the image with `docker load < {output_path.name}`."
    )


def run_download_plan(plan: DownloadPlan, *, issue_number: int, remote: str, disk_budget: int) -> str:
    print(f"Downloading: {plan.url}")
    headers = get_remote_headers(plan.url)
    file_name = infer_filename(plan.url, headers)
    remote_size = get_last_header_value(headers, "content-length")
    if remote_size.isdigit():
        remote_size_int = int(remote_size)
        print(f"Remote file size: {remote_size_int} bytes")
        if remote_size_int > disk_budget:
            raise WorkflowError(
                "⚠️ The remote file is larger than the available runner capacity, so the download was stopped before it started.\n\n"
                f"**Remote size:** `{remote_size_int}` bytes\n"
                f"**Available budget:** `{disk_budget}` bytes"
            )
    else:
        print("Remote file size unavailable, continuing download.")

    print(f"Output filename: {file_name}")
    output_path = Path(file_name)
    if plan.tool == "wget":
        cmd = [
            "wget",
            "--tries=3",
            "--retry-connrefused",
            "--waitretry=5",
            "--timeout=30",
            "-nv",
            *plan.flags,
            "-O",
            str(output_path),
            "--",
            plan.url,
        ]
    else:
        cmd = [
            "curl",
            "--fail",
            "--location",
            "--retry",
            "3",
            "--retry-delay",
            "5",
            "--connect-timeout",
            "30",
            "--silent",
            "--show-error",
            *plan.flags,
            "-o",
            str(output_path),
            "--",
            plan.url,
        ]

    print("Starting download...")
    run_command(cmd)
    file_size = ensure_nonempty_file(output_path)
    print(f"Downloaded file size: {human_size(file_size)} ({file_size} bytes)")
    if file_size > disk_budget:
        raise WorkflowError(
            "⚠️ The downloaded file exceeds the current runner capacity, so the upload was cancelled.\n\n"
            f"**File size:** `{human_size(file_size)}`"
        )

    share_link = upload_and_link(output_path, remote, issue_number, "Downloads")
    return (
        "✅ File download completed.\n\n"
        "**File information:**\n"
        f"- File name: `{output_path.name}`\n"
        f"- File size: `{human_size(file_size)}`\n\n"
        "**Download link:**\n"
        f"{share_link}\n\n"
        "> Check your cloud storage settings for link expiration. If the link expires, submit a new issue."
    )


def dry_run_payload(plan: DockerPlan | DownloadPlan, disk_budget: int) -> dict[str, object]:
    payload: dict[str, object] = {
        "disk_budget_bytes": disk_budget,
        "plan": asdict(plan),
    }
    return payload


def main() -> int:
    args = parse_args()
    disk_budget = available_disk_budget(args.disk_reserve_bytes)
    print(f"Download size budget: {disk_budget} bytes")

    try:
        plan = parse_issue_title(args.issue_title)
        if args.dry_run:
            print(json.dumps(dry_run_payload(plan, disk_budget), indent=2, ensure_ascii=False))
            return 0

        if isinstance(plan, DockerPlan):
            comment = run_docker_plan(plan, issue_number=args.issue_number, remote=args.rclone_remote)
        else:
            comment = run_download_plan(plan, issue_number=args.issue_number, remote=args.rclone_remote, disk_budget=disk_budget)
        post_issue_comment(args.repo, args.issue_number, comment)
        return 0
    except WorkflowError as exc:
        print(exc.message, file=sys.stderr)
        if not args.dry_run:
            post_issue_comment(args.repo, args.issue_number, exc.message, allow_failure=True)
        return 1
    except subprocess.CalledProcessError as exc:
        message = (
            "❌ Task failed.\n\n"
            f"**Command:** `{shlex.join(exc.cmd if isinstance(exc.cmd, Sequence) else [str(exc.cmd)])}`\n"
            f"**Exit code:** `{exc.returncode}`"
        )
        print(message, file=sys.stderr)
        if not args.dry_run:
            post_issue_comment(args.repo, args.issue_number, message, allow_failure=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())