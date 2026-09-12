# AGENTS.md

Ansible Galaxy role that installs OpenCode (from the official `anomalyco/opencode` repo),
creates a dedicated `opencode` login user + SSH access, an empty git repo at
`{{ opencode_home }}/git`, and a systemd `git-instaweb` service.

## Layout

- `tasks/main.yml` — entrypoint: validate -> user -> apt packages -> repo -> webui -> install -> configure (individual `import_tasks` files)
- `defaults/main.yml` — user-facing vars, incl. version pin
- `vars/main.yml` — computed paths / archive URL (overridable)
- `meta/main.yml` — role metadata: Debian bookworm/trixie, Ubuntu jammy/noble, Ansible >= 2.14
- `files/remote-opencode.py` — client-side companion (not run by the role): sshfs-mounts a git worktree and launches remote opencode over SSH. Requires local `git`, `ssh`, `findmnt`, `sshfs`, `umount` — declared as `CMD_*` constants in a `REQUIRED_TOOLS` tuple at the top of the file. Remote tools `mkdir`, `rm`, and the `opencode` binary (from `REMOTE_REQUIRED_TOOLS` + `--remote-opencode-exec`) are checked over SSH. `verify_required_tools()` (local) and `verify_remote_required_tools()` (remote, called at the start of `main`) each collect and report all missing tools at once.
- `molecule/default/` — Podman scenario (Debian 13 systemd container)

## Testing (Molecule)

- No CI; `molecule` is the only test harness. Toolchain (`molecule`, `ansible`, `podman`) is NOT installed in this workspace.
- `molecule dependency` — install deps from `molecule/requirements-*.yml` (needs `containers.podman` collection) before other commands.
- `molecule test` (or `converge`/`idempotence`/`verify` for focused runs). Scenario requires a local Podman; container runs privileged with a cgroup bind-mount.
- `molecule/default/verify.yml` and `side_effect.yml` have `tasks: []` — expect little coverage there.

## Gotchas

- Bumping OpenCode requires updating BOTH `opencode_version` AND `opencode_archive_sha256` in `defaults/main.yml`; the install task's `get_url` enforces the checksum. Archive name is hardcoded to the x64 baseline tarball.
- The role stops, disables, and **masks** the system-wide `lighttpd` service, since git instaweb runs its own lighttpd on port 1234.
- `tasks/user.yml` imports a `user_ssh` role that is not declared in `meta/main.yml` dependencies nor in the molecule role requirements (currently `[]`) — it must exist in `ANSIBLE_ROLES_PATH`.
- `opencode_shell` defaults to `/bin/bash`; tmux (`/usr/bin/tmux`) is the workaround for a foot terminal crash bug, and then a `.tmux.conf` forces `/bin/bash` as the default shell.
