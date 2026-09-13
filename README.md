# ansible_role_opencode

Ansible Galaxy role that installs OpenCode (from the official
[`anomalyco/opencode`](https://github.com/anomalyco/opencode) repo), creates a
dedicated `opencode` login user + SSH access, an empty git repo at
`{{ opencode_home }}/git`, and a systemd `git-instaweb` service.

## Requirements

Runtime dependencies, to install before applying the role:

```sh
ansible-galaxy install -r requirements.yml
```

- Role `user_ssh` (from `github.com/nipil/ansible-role-user-ssh` v1.2.0), used
  to install the SSH public keys and allow the `opencode` user to log in.
- Collection `ansible.posix` (for `authorized_key`) for `user_ssh`

## Variables

See `defaults/main.yml`.
