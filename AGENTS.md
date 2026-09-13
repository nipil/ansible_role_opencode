# AGENTS.md

Ansible Galaxy role that installs OpenCode (from the official `anomalyco/opencode` repo),
creates a dedicated `opencode` login user + SSH access, an empty git repo at
`{{ opencode_home }}/git`, and a systemd `git-instaweb` service.

## Layout

- `tasks/main.yml` — entrypoint: validate -> user -> apt packages -> stop/disable/mask system lighttpd -> repo -> webui -> (conditional) install -> configure -> helper install. Each step is an `import_tasks`.
  - Install runs `opencode --version` first and skips unless the binary is missing or the pinned `opencode_version` isn't in the output.
  - Final task copies `files/remote-opencode.py` to `{{ opencode_helper_install_folder }}` only when `opencode_helper_install` (default `false`); with `opencode_helper_install_on_controller` (default `true`) it delegates to `localhost` with `run_once`.
- `defaults/main.yml` — user-facing vars, incl. version pin
- `vars/main.yml` — computed paths / archive URL (overridable). Archive name is hardcoded to the x64 baseline tarball.
- `meta/main.yml` — role metadata: Debian bookworm/trixie, Ubuntu jammy/noble, Ansible >= 2.14 (no role dependencies)
- `requirements.yml` — consumer-facing runtime dependencies (`user_ssh` role + `ansible.posix` collection)
- `files/remote-opencode.py` — installed by the role (see above) on the controller: sshfs-mounts a git worktree and launches remote opencode over SSH. Requires local `git`, `ssh`, `findmnt`, `sshfs`, `umount` — declared as `CMD_*` constants in a `REQUIRED_TOOLS` tuple at the top of the file. Remote tools `mkdir`, `rm`, and the `opencode` binary (from `REMOTE_REQUIRED_TOOLS` + `--remote-opencode-exec`) are checked over SSH. `verify_required_tools()` (local) and `verify_remote_required_tools()` (remote, called at the start of `main`) each collect and report all missing tools at once.
- `molecule/default/` — Podman scenario (Debian 13 systemd container)

## Testing (Molecule)

- No CI; `molecule` is the only test harness. Toolchain (`molecule`, `ansible`, `podman`) is NOT installed in this workspace.
- `molecule dependency` — installs roles/collections from `molecule/requirements-*.yml` (needs `containers.podman` collection). It pulls `user_ssh` into `molecule/default/roles`, which `molecule.yml` sets as `ANSIBLE_ROLES_PATH`.
- `molecule test` (or `converge`/`idempotence`/`verify` for focused runs). Scenario requires a local Podman; the container (`mpaivabarbosa/molecule-systemd-debian:13`) runs privileged with a cgroup bind-mount and no `init`.
- `side_effect.yml` and the container-bound play of `verify.yml` have `tasks: []`; verify's second (localhost) play asserts the helper landed at `/tmp/remote-opencode.py` (the inventory `group_vars/opencode/local.yml` sets `opencode_helper_install: true` and `opencode_helper_install_folder: /tmp`).

## Gotchas

- Bumping OpenCode requires updating BOTH `opencode_version` AND `opencode_archive_sha256` in `defaults/main.yml`; the install task's `get_url` enforces the checksum.
- The role stops, disables, and **masks** the system-wide `lighttpd` service, since git instaweb runs its own lighttpd on port 1234.
- `tasks/user.yml` ends with `include_role: name: user_ssh`. The role is fetched from `github.com/nipil/ansible-role-user-ssh` v1.2.0 (declared in `requirements.yml` and `molecule/requirements.yml`). It is **not** auto-installed (no `meta/main.yml` dependency — Galaxy deps run before the role's own tasks, but `user_ssh` must run *after* the user exists), so consumers must `ansible-galaxy install -r requirements.yml`; it must exist under exactly the name `user_ssh` in `ANSIBLE_ROLES_PATH` (`molecule dependency` sets this up for the scenario).
- `configure.yml` always rewrites the opencode user's `auth.json` and `model.json` from `opencode_auth` / `opencode_model` (both asserted as mappings in `validate.yml`) — the defaults are empty `{}`, so an unset var clobbers existing credentials.
- `opencode_shell` defaults to `/bin/bash`; tmux (`/usr/bin/tmux`) is the workaround for a foot terminal crash bug, and then a `.tmux.conf` forces `/bin/bash` as the default shell.
