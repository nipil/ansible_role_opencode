#!/usr/bin/env python3

import argparse
import logging
import os
import shlex
import shutil
import subprocess
import sys

from pathlib import Path
from typing import Sequence

SSH_ALIVE_INTERVAL = 10
SSH_ALIVE_COUNT_MAX = 3

DEFAULT_REMOTE_OPENCODE_EXEC = "opencode"
DEFAULT_USER = "opencode"
DEFAULT_BRANCH_REF = "HEAD"
DEFAULT_SSHFS_OPTIONS: tuple[str, ...] = (
    "reconnect",
    f"ServerAliveInterval={SSH_ALIVE_INTERVAL}",
    f"ServerAliveCountMax={SSH_ALIVE_COUNT_MAX}",
    "idmap=user",
)

CMD_FINDMNT = "findmnt"
CMD_GIT = "git"
CMD_SSH = "ssh"
CMD_SSHFS = "sshfs"
CMD_UMOUNT = "umount"

REQUIRED_TOOLS: tuple[str, ...] = (
    CMD_FINDMNT,
    CMD_GIT,
    CMD_SSH,
    CMD_SSHFS,
    CMD_UMOUNT,
)

REMOTE_REQUIRED_TOOLS: tuple[str, ...] = (
    "mkdir",
    "rm",
)


class AppError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare an sshfs-backed git worktree and launch remote opencode.",
    )
    parser.add_argument(
        "--pristine",
        action="store_true",
        help="Drop remote workdir and hard-clean worktree before launch",
    )
    parser.add_argument(
        "host",
        help="Sandbox hostname",
    )
    parser.add_argument(
        "--repo-path",
        default=".",
        help="Path to a git repository",
    )
    parser.add_argument(
        "branch",
        help="Working branch name",
    )
    parser.add_argument(
        "branch_ref",
        nargs="?",
        default=DEFAULT_BRANCH_REF,
        help=f"Ref used to create branch if missing [default: {DEFAULT_BRANCH_REF}]",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "fatal"],
        default="warning",
    )
    parser.add_argument(
        "--user",
        default=DEFAULT_USER,
        help=f"SSH username [default: {DEFAULT_USER}]",
    )
    parser.add_argument(
        "--sshfs",
        action="append",
        default=None,
        metavar="OPT",
        help="sshfs -o option (repeatable), appended to the defaults",
    )
    parser.add_argument(
        "--remote-opencode-exec",
        default=DEFAULT_REMOTE_OPENCODE_EXEC,
        help=(
            "Remote command used to start OpenCode "
            f"[default: {DEFAULT_REMOTE_OPENCODE_EXEC}]"
        ),
    )
    parser.add_argument(
        "--early-exit",
        action="store_true",
        help="Exit after setup",
    )
    parser.add_argument(
        "--keep-mounted",
        action="store_true",
        help="Leave the sshfs mount in place on exit",
    )
    return parser


def run_checked(
    command: Sequence[str],
    *,
    err_ctx: str | None = None,
    capture_output: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = run_unchecked(
        command,
        capture_output=capture_output,
        err_ctx=err_ctx,
        cwd=cwd,
    )
    if result.returncode == 0:
        return result
    stderr = (result.stderr or "").strip()
    details = f": {stderr}" if stderr else ""
    message = f"command failed ({shlex.join(command)}){details}"
    if err_ctx is not None:
        message = f"{err_ctx}: {message}"
    raise AppError(message)


def run_unchecked(
    command: Sequence[str],
    *,
    err_ctx: str | None = None,
    capture_output: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    # override lang for spawned processes to better match messages
    env = os.environ.copy()
    env["LC_ALL"] = "C.UTF-8"
    args = list(command)
    logging.debug(f"Executing: {args}")
    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            check=False,
            text=True,
            capture_output=capture_output,
        )
    except FileNotFoundError:
        message = f"command not found: {command[0]}"
        if err_ctx is not None:
            message = f"{err_ctx}: {message}"
        raise AppError(message) from None


def verify_required_tools() -> None:
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise AppError("required tools not found: " + ", ".join(missing))


def verify_remote_required_tools(
    host: str,
    user: str,
    remote_opencode_exec: str,
) -> None:
    tools = list(dict.fromkeys([*REMOTE_REQUIRED_TOOLS, remote_opencode_exec]))
    missing = []
    for tool in tools:
        result = run_unchecked(
            [
                CMD_SSH,
                "-l",
                user,
                host,
                "command",
                "-v",
                shlex.quote(tool),
            ],
            err_ctx=f"failed to check remote tool {tool}",
        )
        if result.returncode != 0:
            missing.append(tool)
    if missing:
        raise AppError(
            f"required remote tools not found on {user}@{host}: " + ", ".join(missing)
        )


def validate_repo(
    repo_path: Path,
) -> None:
    if not repo_path.exists():
        raise AppError(f"repository path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise AppError(f"repository path is not a directory: {repo_path}")
    run_checked(
        [
            CMD_GIT,
            "-C",
            str(repo_path),
            "rev-parse",
            "--git-dir",
        ],
        err_ctx=f"failed to validate repository at {repo_path}",
    )


def local_workdir(
    repo_path: Path,
    branch: str,
) -> Path:
    base_name = repo_path.name
    return repo_path.parent / f"{base_name}.{branch}"


def remove_path_if_exists(
    path: Path,
) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except IsADirectoryError:
        pass
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AppError(f"failed to remove path {path}: {exc}") from None


def umount_if_present(
    path: Path,
) -> None:
    result = run_unchecked(
        [CMD_UMOUNT, str(path)],
        err_ctx=f"failed to unmount {path}",
    )
    if result.returncode == 0:
        return
    stderr_lc = (result.stderr or "").strip().lower()
    if any(
        pattern in stderr_lc for pattern in ("not mounted", "no such file or directory")
    ):
        return
    raise AppError(f"umount failed for {path}: {(result.stderr or '').strip()}")


def remote_prepare(
    host: str,
    user: str,
    remote_workdir: str,
    pristine: bool,
) -> None:
    # remote path reaches a shell, so it must be quoted there
    remote_workdir_arg = shlex.quote(remote_workdir)
    if pristine:
        run_checked(
            [
                CMD_SSH,
                "-l",
                user,
                host,
                "rm",
                "-r",
                "-f",
                remote_workdir_arg,
            ],
            err_ctx=(
                f"failed to remove remote workdir {remote_workdir} on {user}@{host}"
            ),
        )
    run_checked(
        [
            CMD_SSH,
            "-l",
            user,
            host,
            "mkdir",
            "-p",
            remote_workdir_arg,
        ],
        err_ctx=(f"failed to create remote workdir {remote_workdir} on {user}@{host}"),
    )


def current_mount_source(path: Path) -> str | None:
    result = run_unchecked(
        [
            CMD_FINDMNT,
            "--noheadings",
            "--raw",
            "-o",
            "SOURCE",
            str(path),
        ],
    )
    if result.returncode != 0:
        return None
    source = (result.stdout or "").strip()
    return source or None


def mount_sshfs(
    host: str,
    user: str,
    remote_workdir: str,
    local_mount: Path,
    options: Sequence[str],
    debug: bool,
) -> None:
    target = f"{user}@{host}:{remote_workdir}"
    command: list[str] = [
        CMD_SSHFS,
        target,
        str(local_mount),
    ]
    for option in options:
        command.extend(["-o", option])
    if debug:
        command.append("--debug")
    result = run_unchecked(
        command,
        err_ctx=f"failed to mount sshfs from {target} at {local_mount}",
    )
    if result.returncode == 0:
        return
    # sshfs fails when the mountpoint is already mounted
    # (for example leftover from a previous run).
    # Reuse if it is the same, or error if source differs
    if current_mount_source(local_mount) == target:
        logging.info(
            "Mount %s -> %s already present; reusing the existing mount",
            target,
            local_mount,
        )
        return
    stderr = (result.stderr or "").strip()
    details = f": {stderr}" if stderr else ""
    raise AppError(f"failed to mount sshfs from {target} at {local_mount}{details}")


def branch_exists(repo_path: Path, branch: str) -> bool:
    result = run_unchecked(
        [
            CMD_GIT,
            "-C",
            str(repo_path),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ],
        err_ctx=f"failed to check branch {branch} in {repo_path}",
    )
    return result.returncode == 0


def ensure_branch(repo_path: Path, branch: str, branch_ref: str) -> None:
    if branch_exists(repo_path, branch):
        return
    run_checked(
        [CMD_GIT, "-C", str(repo_path), "branch", branch, branch_ref],
        err_ctx=f"failed to create branch {branch} from {branch_ref}",
    )


def listed_worktrees(repo_path: Path) -> set[Path]:
    result = run_checked(
        [
            CMD_GIT,
            "-C",
            str(repo_path),
            "worktree",
            "list",
            "--porcelain",
        ],
        err_ctx=f"failed to list worktrees of {repo_path}",
    )
    items: set[Path] = set()
    for line in result.stdout.splitlines():
        token0, sep, remainder = line.partition(" ")
        if token0 != "worktree" or not sep or not remainder:
            continue
        raw_path = remainder
        items.add(Path(raw_path).resolve())
    return items


def ensure_worktree(repo_path: Path, workdir: Path, branch: str) -> None:
    registered = listed_worktrees(repo_path)
    resolved_workdir = workdir.resolve()
    if resolved_workdir not in registered:
        run_checked(
            [
                CMD_GIT,
                "-C",
                str(repo_path),
                "worktree",
                "add",
                str(workdir),
                branch,
            ],
            err_ctx=f"failed to add worktree {workdir} for branch {branch}",
        )
        return
    run_checked(
        [
            CMD_GIT,
            "-C",
            str(repo_path),
            "worktree",
            "repair",
            str(workdir),
        ],
        err_ctx=f"failed to repair worktree {workdir}",
    )


def pristine_worktree(workdir: Path) -> None:
    run_checked(
        [
            CMD_GIT,
            "-C",
            str(workdir),
            "reset",
            "--hard",
        ],
        err_ctx=f"failed to reset worktree {workdir} to HEAD",
    )
    run_checked(
        [
            CMD_GIT,
            "-C",
            str(workdir),
            "clean",
            "-dxf",
        ],
        err_ctx=f"failed to clean untracked files in worktree {workdir}",
    )


def launch_remote_opencode(
    host: str, user: str, remote_exec: str, remote_workdir: str
) -> int:
    # remote path reaches a shell, so it must be quoted there
    remote_workdir_arg = shlex.quote(remote_workdir)
    result = run_unchecked(
        [
            CMD_SSH,
            # remote TUI requires a tty
            "-t",
            "-l",
            user,
            host,
            remote_exec,
            remote_workdir_arg,
        ],
        # remote TUI requires terminal pass-through
        capture_output=False,
        err_ctx=f"failed to launch remote opencode on {user}@{host}",
    )
    return result.returncode


def resolve_sshfs_options(values: Sequence[str] | None) -> list[str]:
    options = list(DEFAULT_SSHFS_OPTIONS)
    if values is not None:
        options.extend(values)
    return options


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper())
    logging.basicConfig(
        level=log_level,
        format="[%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    verify_required_tools()
    verify_remote_required_tools(args.host, args.user, args.remote_opencode_exec)

    repo_path = Path(args.repo_path).expanduser().resolve()
    validate_repo(repo_path)
    logging.info("Validated local repo at %s", repo_path)

    logging.info("Ensuring branch %s (ref=%s)", args.branch, args.branch_ref)
    ensure_branch(repo_path, args.branch, args.branch_ref)

    workdir = local_workdir(repo_path, args.branch)
    remote_workdir = workdir.name
    sshfs_options = resolve_sshfs_options(args.sshfs)
    logging.info("Resolved workdir=%s remote_workdir=%s", workdir, remote_workdir)

    logging.info(
        "Preparing remote workdir %s on %s@%s (pristine=%s)",
        remote_workdir,
        args.user,
        args.host,
        args.pristine,
    )
    remote_prepare(args.host, args.user, remote_workdir, args.pristine)

    logging.info("Creating local mount directory %s", workdir)
    try:
        workdir.mkdir(parents=True)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AppError(f"failed to create mount directory {workdir}: {exc}") from None

    logging.info(
        "Mounting sshfs %s@%s:%s -> %s",
        args.user,
        args.host,
        remote_workdir,
        workdir,
    )

    mount_sshfs(
        args.host,
        args.user,
        remote_workdir,
        workdir,
        sshfs_options,
        log_level is logging.DEBUG,
    )

    try:
        logging.info("Ensuring worktree at %s for branch %s", workdir, args.branch)
        ensure_worktree(repo_path, workdir, args.branch)

        if args.pristine:
            logging.info("Resetting worktree to pristine state")
            pristine_worktree(workdir)

        if args.early_exit:
            logging.info("Exiting early as requested")
            return os.EX_OK

        logging.info("Launching remote opencode on %s@%s", args.user, args.host)
        return launch_remote_opencode(
            args.host,
            args.user,
            args.remote_opencode_exec,
            remote_workdir,
        )
    finally:
        if args.keep_mounted:
            logging.warning("Leaving %s mounted, as requested", workdir)
        else:
            logging.info("Unmounting %s", workdir)
            try:
                umount_if_present(workdir)
            except AppError as exc:
                logging.warning("Cleanup %s when you are done: %s", workdir, exc)


def safe_main() -> None:
    exit_code = os.EX_SOFTWARE
    try:
        exit_code = main()
    except AppError as exc:
        logging.error("%s", exc)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    sys.exit(exit_code)


if __name__ == "__main__":
    safe_main()
