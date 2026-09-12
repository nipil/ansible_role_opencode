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

DEFAULT_REMOTE_OPENCODE_EXEC: str = "opencode"
DEFAULT_USER: str = "opencode"
DEFAULT_BRANCH_REF: str = "HEAD"
DEFAULT_SSHFS_OPTIONS: tuple[str, ...] = (
    "reconnect",
    "ServerAliveInterval=10",
    "ServerAliveCountMax=3",
    "idmap=user",
)

CMD_GIT: str = "git"
CMD_SSH: str = "ssh"


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
        "repo_path",
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
        default="info",
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
        help="sshfs -o option (repeatable)",
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
        ["umount", str(path)],
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


def remote_workdir_path(
    workdir: Path,
) -> str:
    return shlex.quote(workdir.name)


def remote_prepare(
    host: str,
    user: str,
    remote_workdir: str,
    pristine: bool,
) -> None:
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
        "sshfs",
        target,
        str(local_mount),
    ]
    for option in options:
        command.extend(["-o", option])
    if debug:
        command.append("--debug")
    run_checked(
        command,
        err_ctx=f"failed to mount sshfs from {target} at {local_mount}",
    )


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
    result = run_unchecked(
        [
            CMD_SSH,
            # remote TUI requires a tty
            "-t",
            "-l",
            user,
            host,
            remote_exec,
            remote_workdir,
        ],
        # remote TUI requires terminal pass-through
        capture_output=False,
        err_ctx=f"failed to launch remote opencode on {user}@{host}",
    )
    return result.returncode


def resolve_sshfs_options(values: Sequence[str] | None) -> list[str]:
    if values is not None:
        return list(values)
    return list(DEFAULT_SSHFS_OPTIONS)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper())
    logging.basicConfig(
        level=log_level,
        format="[%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    repo_path = Path(args.repo_path).expanduser().resolve()
    validate_repo(repo_path)
    logging.info("Validated local repo at %s", repo_path)

    workdir = local_workdir(repo_path, args.branch)
    remote_workdir = remote_workdir_path(workdir)
    sshfs_options = resolve_sshfs_options(args.sshfs)
    logging.info("Resolved workdir=%s remote_workdir=%s", workdir, remote_workdir)

    logging.info("Unmounting existing mount at %s (if present)", workdir)
    umount_if_present(workdir)

    logging.info("Removing existing path %s (if present)", workdir)
    remove_path_if_exists(workdir)

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
        "Mounting sshfs %s@%s:%s -> %s", args.user, args.host, remote_workdir, workdir
    )

    mount_sshfs(
        args.host,
        args.user,
        remote_workdir,
        workdir,
        sshfs_options,
        log_level is logging.DEBUG,
    )

    logging.info("Ensuring branch %s (ref=%s)", args.branch, args.branch_ref)
    ensure_branch(repo_path, args.branch, args.branch_ref)

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
