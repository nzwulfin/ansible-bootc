# Design Specification: bootc + Ansible Reference Pattern

> **Authority:** This document is the architectural contract for the project described in
> `docs/plan.md`. It states what every file must express, what constraints must hold across
> the whole repository, and what interfaces must agree between components. It does not
> contain complete file contents. Implementation agents receive one task definition and this
> document; that combination must be sufficient to implement the task correctly.

---

## 1. Global Constraints

Every implementation agent must verify these invariants before writing any file. A file that
satisfies its own contract but violates a global constraint is incorrect.

| ID  | Domain | Invariant |
|-----|--------|-----------|
| G1  | Firewall tooling | Every Containerfile step that manages firewall state must use `firewall-offline-cmd`. The runtime `firewall-cmd` requires a running daemon and will fail or behave incorrectly inside a build context. No exceptions in any image. |
| G2  | Management account identity | The `ansible` account is pinned to **UID 5000, GID 5000**. The numeric pin applies wherever a numeric identity is expressed: the `sysusers.d` declaration, any numeric `chown` in a Containerfile, and any numeric `owner:`/`group:` directive. The `authorized_key` module's `user:` field is **not** one of these — it takes the account *name* (`ansible`), never a UID, and must agree on that name. Any divergence between the numeric pins makes the PSK file unreadable at boot; a `user:` field that names an account other than `ansible` writes the key to the wrong home. |
| G3  | PSK install-only lifecycle | The bootstrap key lives at `/var/home/ansible/.ssh/authorized_keys` in the image's `/var` tree. No `tmpfiles.d` entry of type `f` or `f~` may reference this path. Only `d` (directory create) entries are permitted for `.ssh` in tmpfiles.d. An `f`/`f~` entry would recreate the PSK after rotation, defeating the first-contact-only guarantee. |
| G4  | Shared-secret scope | The variable `app_db_password` must be declared **only** in `ansible/inventory/group_vars/all/vault.yml`. It must not appear in any group-specific vault file, any role default, or any plaintext file. Both the database role (which creates the account) and the application role (which writes the config) must resolve the identical value from this single location. |
| G5  | MariaDB drop-in zone | The `mariadb.service` unit drop-in must be placed under `usr/lib/systemd/system/mariadb.service.d/` (image-owned). It must never be placed under `/etc/systemd/system/`. The `/usr/lib` location ensures the corporate standard updates unconditionally with the image; admins stack further tuning via `systemctl edit` into `/etc/`. |
| G6  | Tuned profile zone | The corporate database tuned profile must reside under `usr/lib/tuned/corp-database/` (image-owned). The profile directory name `corp-database` must exactly match the string written to `/etc/tuned/active_profile` in the database Containerfile. |
| G7  | App service not enabled | The `corp-app.service` unit is baked into the application image but the Containerfile must not run `systemctl enable corp-app`. The service requires `/etc/corp-app/config.ini` to exist before it can start; enabling it in the image causes a crash-loop on a fresh install before Ansible has written that file. |
| G8  | SSH policy delivery | SSH hardening policy is delivered exclusively through a drop-in file under `/etc/ssh/sshd_config.d/`. The base `sshd_config` file must never be modified or replaced directly. |
| G9  | No runtime package management | No Ansible task installs or updates packages. Every runtime dependency — including Python DB client libraries — must be baked into the appropriate image at build time. |
| G10 | SSH pipelining | `pipelining = True` must appear in the `[ssh_connection]` section of `ansible/ansible.cfg`. This is required for correct `sudo` behaviour with `NOPASSWD` accounts. |
| G11 | Ansible run directory | All `ansible-playbook` invocations are run from the `ansible/` directory. All paths in `ansible.cfg` must be relative to that directory. The README must state this requirement explicitly. |
| G12 | Config path agreement | The application config path `/etc/corp-app/config.ini` must be identical in three independent locations: hardcoded in the application executable, in the `dest:` of the Ansible template task, and readable by the systemd unit's process. A divergence between any two of these silently breaks the application. |
| G13 | Script placement | `scripts/generate-demo-keys.sh` lives at the **repository root** under `scripts/`, not inside any subtree. It crosses the images/ansible boundary (writes the bootstrap pubkey into the base image build context AND generates keys consumed by Ansible), and its placement reflects that project-wide scope. `ansible/keys/` holds only generated key material and its `.gitignore`; no scripts reside there. |

---

## 2. Repository Layout (Normative)

```
ansible-bootc/
├── README.md
├── docs/
│   ├── plan.md
│   └── design-spec.md
├── scripts/
│   └── generate-demo-keys.sh        ← repo-root scope; crosses images/ansible boundary
├── images/
│   ├── base/
│   │   ├── Containerfile
│   │   └── rootfs/                  ← mirrors target filesystem; documents what lands where
│   │       ├── etc/
│   │       │   ├── sudoers.d/
│   │       │   │   └── 99-ansible
│   │       │   └── ssh/
│   │       │       └── sshd_config.d/
│   │       │           └── 50-corporate-hardening.conf
│   │       ├── var/
│   │       │   └── home/
│   │       │       └── ansible/
│   │       │           └── .ssh/
│   │       │               └── authorized_keys    ← gitignored; generated by script
│   │       └── usr/
│   │           └── lib/
│   │               ├── sysusers.d/
│   │               │   └── ansible.conf
│   │               └── tmpfiles.d/
│   │                   └── ansible-home.conf
│   ├── database/
│   │   ├── Containerfile
│   │   └── rootfs/
│   │       ├── etc/
│   │       │   └── my.cnf.d/
│   │       │       └── 00-corporate.cnf
│   │       └── usr/
│   │           └── lib/
│   │               ├── systemd/
│   │               │   └── system/
│   │               │       └── mariadb.service.d/
│   │               │           └── 10-corporate.conf
│   │               └── tuned/
│   │                   └── corp-database/
│   │                       └── tuned.conf
│   └── application/
│       ├── Containerfile
│       └── rootfs/
│           └── usr/
│               ├── lib/
│               │   └── systemd/
│               │       └── system/
│               │           └── corp-app.service
│               └── local/
│                   └── bin/
│                       └── corp-app.py
└── ansible/
    ├── .gitignore
    ├── ansible.cfg
    ├── requirements.yml
    ├── site.yml
    ├── inventory/
    │   ├── hosts.yml
    │   └── group_vars/
    │       ├── all/
    │       │   ├── vars.yml
    │       │   └── vault.yml
    │       ├── database/
    │       │   ├── vars.yml
    │       │   └── vault.yml
    │       └── application/
    │           ├── vars.yml
    │           └── vault.yml
    ├── keys/                         ← generated key material only; no scripts
    │   └── .gitignore
    ├── playbooks/
    │   ├── 00_bootstrap.yml
    │   ├── 01_database.yml
    │   └── 02_application.yml
    └── roles/
        ├── bootstrap_key_rotation/
        ├── mysql_instance/
        └── app_instance/
```

---

## 3. Component Contracts

### 3.1 `scripts/generate-demo-keys.sh`

**Purpose:** Project-wide bootstrap script. Generates the bootstrap (PSK) and live SSH key pairs, then writes the PSK public key — with its mandatory restrictive options prefix — into the base image build context. Must be run before any image build.

**Must declare/configure:**
- Resolve the base image rootfs path relative to the script's own location (not a hardcoded absolute path).
- Guard against existing keys: if `id_bootstrap` already exists in the output directory, abort with an error message that warns the operator that regeneration invalidates images built with the old key.
- Generate both key pairs as Ed25519 keys with no passphrase.
- Write a single line to `images/base/rootfs/var/home/ansible/.ssh/authorized_keys` consisting of the four SSH option tokens followed by the bootstrap public key material. The option tokens are: `no-pty`, `no-X11-forwarding`, `no-agent-forwarding`, `no-user-rc`.
- Print a summary identifying the files generated and the destination of the PSK, and a prominent warning that the base image must be built after this script.

**Must not do:**
- Be placed inside `ansible/` or `images/`. Its location under `scripts/` at the repo root is fixed (G13).
- Overwrite keys silently. The guard-against-existing-keys check is mandatory; accidental key regeneration invalidates deployed images.
- Use `echo -e` to write the PSK line; use a method that does not interpret escape sequences (e.g., `printf '%s\n'`).

**Interface agreements:**
- The path it writes to (`images/base/rootfs/var/home/ansible/.ssh/authorized_keys`) must be the exact path the base Containerfile copies via `COPY rootfs/ /`, placing the file at `/var/home/ansible/.ssh/authorized_keys` in the image.
- The generated `id_bootstrap.pub` and `id_live.pub` must land in `ansible/keys/` so that Ansible roles can read them via the paths declared in `group_vars/all/vars.yml`.

**Applicable constraints:** G2 (PSK is the bootstrap key, which is associated with the ansible account at UID 5000), G3, G13.

Example — restrictive options format (illustrative only):
```
no-pty,no-X11-forwarding,no-agent-forwarding,no-user-rc ssh-ed25519 AAAA... comment
```

---

### 3.2 `ansible/keys/.gitignore`

**Purpose:** Ensures all generated key material and the vault password file are never committed to the repository.

**Must declare:** Gitignore patterns for both private and public key files for both key pairs (`id_bootstrap`, `id_bootstrap.pub`, `id_live`, `id_live.pub`), and the vault password file (`.vault_pass`).

**Must not do:** List any script files or contain any path that would cause the `.gitignore` itself to be ignored.

**Applicable constraints:** G13 (this directory holds only key material).

---

### 3.3 Root-level `.gitignore`

**Must declare (in addition to any existing content):** A pattern that ignores `images/base/rootfs/var/home/ansible/.ssh/authorized_keys`. This file is a generated artifact (written by the key-generation script) and must never be committed.

**Interface agreement:** The gitignored path must exactly match the path that `scripts/generate-demo-keys.sh` writes to.

---

## 4. Base Image (`images/base/`)

### 4.1 `images/base/Containerfile`

**Purpose:** Defines the platform/security layer. Provides the management account, PSK, SSH baseline, firewall, and platform toolset. All workload images derive from this image.

**FROM:** `quay.io/centos-bootc/centos-bootc:stream10`

**Must install (package set):** `sudo`, `openssh-server`, `python3`, `rsync`, `iproute`, `firewalld`, `tuned`, `tmux`, `vim-minimal`, `bind-utils`. The rationale for each must appear as a comment.

**Must configure (in this order, which is normative):**
1. Install all packages and clean the package cache in a single layer.
2. Copy the entire `rootfs/` tree with `COPY rootfs/ /`. This single step installs all baked config and is the documentation of what lands where on a deployed system.
3. Set ownership on `/var/home/ansible` and its subtree to `5000:5000` (numeric; the user does not exist at build time). Set mode `0700` on `.ssh` and mode `0600` on `authorized_keys`. Boot-time hooks do not re-enforce modes on pre-existing files — the image content itself must be correct.
4. Set mode `0440` on `/etc/sudoers.d/99-ansible` (sudo requires root-owned, mode 440 files).
5. Enable `sshd`, `firewalld`, and `tuned` in a single `systemctl enable` step.
6. Open the SSH service in the default firewall zone using `firewall-offline-cmd` (G1).
7. Write the platform tuned profile name (`throughput-performance`) to `/etc/tuned/active_profile` and set profile mode to `manual`. Use direct file writes; `tuned-adm` requires a running daemon.

**Must not do:**
- Use `firewall-cmd` at any point (G1).
- Use a symbolic username in the `chown` step — the user does not exist at build time; ownership must be numeric (G2).
- Enable `corp-app.service` (G7 — though this is an application-image concern, the base image must not accidentally enable it either).
- Place any SSH policy in `/etc/ssh/sshd_config` directly (G8).
- Include a `tmpfiles.d` entry of type `f` or `f~` for the `authorized_keys` path (G3).
- Enable `mariadb`.

**bootc lint note:** `bootc container lint` will emit a warning about content under `/var`. This warning is expected and intentional — the PSK's placement under `/var` is the correct mechanism for "present at first contact, never resurrected" semantics. The README must document this warning as intentional, not a defect.

**Applicable constraints:** G1, G2, G3, G8, G9.

---

### 4.2 `images/base/rootfs/usr/lib/sysusers.d/ansible.conf`

**Purpose:** Declares the `ansible` management account with a pinned UID and GID. `systemd-sysusers` runs at every boot, before `systemd-tmpfiles`, making the account definition drift-free across image rebuilds and upgrades.

**Must declare:**
- A group entry for `ansible` at GID 5000.
- A user entry for `ansible` at UID 5000, GID 5000, with shell `/bin/bash` and home `/var/home/ansible`.
- A human-readable description/GECOS field (e.g., "Ansible management account").

**Must not do:** Create the home directory (that is `tmpfiles.d`'s responsibility). Must not set a password.

**Interface agreements:** The UID and GID values (5000) must exactly match the numeric ownership set by the Containerfile `chown` step (G2).

**Applicable constraints:** G2.

---

### 4.3 `images/base/rootfs/usr/lib/tmpfiles.d/ansible-home.conf`

**Purpose:** Ensures the home directory tree for the `ansible` account exists on every boot. `systemd-tmpfiles` runs after `systemd-sysusers`, so the name `ansible` resolves correctly in ownership fields.

**Must declare:**
- A `d` (directory) entry for `/var/home/ansible` with mode `0700`, owned by `ansible:ansible`.
- A `d` (directory) entry for `/var/home/ansible/.ssh` with mode `0700`, owned by `ansible:ansible`.

**Must not do:** Include any entry of type `f`, `f~`, `F`, `w`, or any other type that would create or write file content. Specifically, no entry may reference `authorized_keys` (G3). A `d` entry on a directory that already exists is a no-op — the existing directory and its contents are never modified.

**Applicable constraints:** G3.

---

### 4.4 `images/base/rootfs/etc/ssh/sshd_config.d/50-corporate-hardening.conf`

**Purpose:** Establishes the corporate SSH security baseline as a drop-in, leaving the base `sshd_config` unmodified (G8).

**Must declare:**
- `PasswordAuthentication no` — key-only authentication.
- `ChallengeResponseAuthentication no` — disable challenge-response (blocks PAM password prompts).
- `PermitRootLogin no` — no root SSH login; break-glass is the live key or full re-provisioning.
- `PubkeyAuthentication yes` — stated explicitly for clarity.
- `AllowUsers ansible` — restrict SSH access to the management account only. This makes the management model coherent: a fresh VM has no interactive login path for any other account.

**Must not do:** Modify or reference `/etc/ssh/sshd_config` (G8). Must not grant SSH access to any account other than `ansible`.

**Applicable constraints:** G8.

---

### 4.5 `images/base/rootfs/etc/sudoers.d/99-ansible`

**Purpose:** Grants passwordless `sudo` to the `ansible` account. Required for Ansible `become: true` operations without an interactive password prompt; SSH pipelining + NOPASSWD is the standard bootc/Ansible pattern.

**Must declare:** A rule granting `ansible` `NOPASSWD:ALL` on all hosts and all users. Include a comment noting this is a demo convenience and that production would scope to specific commands.

**Must not do:** Grant sudo to any other account. Must not end with `~` or contain `.` (sudo ignores such files).

**File name rule:** `99-ansible` — no `~`, no `.`; `99` prefix ensures lexicographic processing after any distribution drop-ins.

**Permissions contract:** Must be mode `0440`, owner `root:root`. The Containerfile is responsible for enforcing this with an explicit `chmod` step after `COPY rootfs/ /`.

---

### 4.6 `images/base/rootfs/var/home/ansible/.ssh/authorized_keys`

**Status:** Gitignored. Not committed. The directory tree up to `.ssh/` is committed; the file itself is a generated artifact written by `scripts/generate-demo-keys.sh`.

**Content contract (post-script execution):** Exactly one line. The line must begin with the four SSH option tokens (`no-pty`, `no-X11-forwarding`, `no-agent-forwarding`, `no-user-rc`) as a comma-separated prefix, followed by the bootstrap public key material. No trailing whitespace or blank lines.

**Why these options:** They restrict what the shared provisioning key can do — it cannot open an interactive terminal (no-pty), forward X11 sessions, forward the SSH agent, or run user rc files. Ansible's connection mode (non-interactive command execution) does not require any of these capabilities.

**Permissions contract:** Mode `0600`, numeric owner `5000:5000`. Enforced by the Containerfile `chown`/`chmod` steps. SSH will refuse to use an `authorized_keys` file with incorrect permissions.

**Applicable constraints:** G2, G3.

---

## 5. Database Image (`images/database/`)

### 5.1 `images/database/Containerfile`

**Purpose:** Workload layer for the database host. Derives from the base image; adds MariaDB, the Python DB client, corporate database configuration, resource limits, and a database-oriented tuned profile.

**FROM:** The locally-built base image tag (e.g., `corp-base:latest`). The tag must be consistent with how the base image is built and referenced in build documentation.

**Must install (package set):** `mariadb-server`, `python3-PyMySQL`. Rationale for each must appear as a comment. (G9: baked because Ansible must not install packages at runtime; python3-PyMySQL is required by both the `community.mysql` Ansible modules and any local DB client work.)

**Must configure (in order):**
1. Install packages and clean cache.
2. Copy `rootfs/` with `COPY rootfs/ /`. This installs the MariaDB config, the unit drop-in, and the tuned profile in one step.
3. Enable `mariadb` so the service starts on first boot and self-initializes with the corporate config already in place.
4. Open the MySQL/MariaDB service in the firewall using `firewall-offline-cmd --add-service=mysql` (G1). This broad open is the bake-time posture; the `mysql_instance` Ansible role narrows it at runtime.
5. Write `corp-database` to `/etc/tuned/active_profile` and `manual` to `/etc/tuned/profile_mode`. The profile name must match the directory name under `/usr/lib/tuned/` exactly (G6).

**Must not do:**
- Use `firewall-cmd` (G1).
- Place the `mariadb.service` drop-in under `/etc/systemd/system/` (G5).
- Place the tuned profile under `/etc/tuned/` (G6).
- Install packages that are already present in the base image (avoid layer bloat).

**Applicable constraints:** G1, G5, G6, G9.

---

### 5.2 `images/database/rootfs/etc/my.cnf.d/00-corporate.cnf`

**Purpose:** Corporate MariaDB baseline configuration. MariaDB reads `/etc/my.cnf.d/` via `!includedir`; there is no image-owned (`/usr/lib`) equivalent. The `00-` prefix ensures this file is processed first, establishing corporate defaults before any instance overrides.

**Must declare (under `[mysqld]`):**
- Character set: `utf8mb4` with `utf8mb4_unicode_ci` collation. This is the corporate standard for all new databases.
- `bind-address = 0.0.0.0` — network-reachable binding. The application host connects over the network; localhost binding would break that. The firewall (G1) controls actual access.
- InnoDB buffer pool size and log file size (conservative demo values with a comment to size for production).
- Slow query logging enabled, with a log file path under `/var/log/mariadb/`, and a `long_query_time` threshold.

**Must declare (under `[client]`):**
- `default-character-set = utf8mb4` — clients use the same character set.

**Must not do:** Set a root password. Root access is exclusively via the unix socket (no password set, no password in any secrets store).

**Applicable constraints:** G9 (no runtime config; this is bake-time standard).

---

### 5.3 `images/database/rootfs/usr/lib/systemd/system/mariadb.service.d/10-corporate.conf`

**Purpose:** Corporate resource limits for the MariaDB service. Image-owned unit drop-in (G5).

**Must declare (under `[Service]`):**
- `LimitNOFILE` set to a value appropriate for a production database (the default systemd limit of 1024 is too low). The value must be documented with a comment explaining why.
- `LimitNPROC` set to a value appropriate for a database server.

**Why not `limits.d`:** PAM `limits.d` settings apply to login sessions. `mariadb.service` is started by systemd and bypasses PAM entirely. The unit drop-in is the correct and only reliable mechanism.

**Why `/usr/lib` not `/etc`:** The `/usr/lib` location is image-owned and updates unconditionally with the image — the corporate standard is always enforced. Admins who need instance-specific tuning use `systemctl edit mariadb` which writes to `/etc/systemd/system/mariadb.service.d/override.conf`, layering on top of this baseline.

**Must not do:** Be placed under `/etc/` (G5, absolute).

**Applicable constraints:** G5.

---

### 5.4 `images/database/rootfs/usr/lib/tuned/corp-database/tuned.conf`

**Purpose:** Corporate database workload tuned profile. Image-owned (G6); follows image updates.

**Must declare:**
- `[main]` section with a `summary` line.
- Inheritance from `throughput-performance` (the platform baseline set in the base image).
- `transparent_hugepages = never` under `[vm]` — THP causes latency spikes under database I/O patterns.
- `vm.swappiness` minimized (database workloads are I/O-bound, not memory-bound) under `[sysctl]`.
- At least one `[disk]` or `[sysctl]` directive appropriate for database workload I/O patterns, with a comment explaining the rationale.

**Must not do:** Be placed under `/etc/tuned/` (G6). The profile name in the directory path (`corp-database`) must exactly match the string written to `/etc/tuned/active_profile` (G6).

**Applicable constraints:** G6.

---

## 6. Application Image (`images/application/`)

### 6.1 `images/application/Containerfile`

**Purpose:** Workload layer for the application host. Derives from the base image; adds the Python DB client, the application executable, and the service unit.

**FROM:** The locally-built base image tag (same tag as used by the database image).

**Must install (package set):** `python3-PyMySQL`. Rationale: the application connects to MariaDB at runtime; this is a runtime dependency that must be baked (G9). `python3` is already in the base image.

**Must configure (in order):**
1. Install packages and clean cache.
2. Copy `rootfs/` with `COPY rootfs/ /`. Installs the application executable and systemd unit.
3. Set mode `0755` on `/usr/local/bin/corp-app.py` (executable by all; run by systemd as root).

**Must not do:**
- Run `systemctl enable corp-app` (G7, absolute). The service cannot start before Ansible writes its config file.
- Open any firewall port. The health endpoint binds loopback only; no external access requires a port rule.

**Applicable constraints:** G7, G9.

---

### 6.2 `images/application/rootfs/usr/lib/systemd/system/corp-app.service`

**Purpose:** Systemd service unit for the application. Image-owned (under `/usr/lib`); updates cleanly with the image. Admins override behaviour via `systemctl edit` drop-ins into `/etc/`; the image-owned unit is never edited directly.

**Must declare:**
- `[Unit]`: `Description`, `After=network-online.target`, `Wants=network-online.target`. No dependency on `mariadb` — the database is remote.
- `[Service]`: `Type=simple`, `ExecStart=/usr/local/bin/corp-app.py`, `Restart=on-failure`, journal output for stdout and stderr.
- `[Service]` sandboxing directives (intentionally small set, overridable via drop-in): `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`. Include a comment that these are overridable via `systemctl edit corp-app`.
- `[Install]`: `WantedBy=multi-user.target`.

**Must not do:** Set `User=` or `Group=` (the service runs as root to read a mode-0640 config file; acceptable for the demo). Must not declare any `ExecStartPre=` that would attempt to create the config directory (that is Ansible's job). Must not reference a config file path other than `/etc/corp-app/config.ini` (G12).

**Applicable constraints:** G7 (not enabled), G12.

---

### 6.3 `images/application/rootfs/usr/local/bin/corp-app.py`

**Purpose:** Minimal application service demonstrating the database wire-up pattern. The value is in the image/Ansible design, not the application logic.

**Must implement:**
- Read configuration from exactly `/etc/corp-app/config.ini` — this path is hardcoded and is the contract with the Ansible role (G12). No environment variable override, no CLI argument. The hardcoding makes the interface explicit and unavoidable.
- Exit with a non-zero code and a clear error message if the config file does not exist or `pymysql` is not importable.
- Attempt a MariaDB connection using the `[database]` section: `host`, `port` (default 3306), `name` (database name), `user`, `password`. Use a short connect timeout.
- Serve a JSON health endpoint on `127.0.0.1` only (loopback binding is mandatory; no external port opened, consistent with G7's firewall constraint) at the port specified in `[server] port` (default 8080).
- `GET /health`: respond HTTP 200 with `{"database": "connected", "detail": "ok"}` on successful DB connection; HTTP 503 with `{"database": "error", "detail": "<exception message>"}` on failure. All other paths: HTTP 404.
- `Content-Type: application/json` on all responses.

**Must not do:** Bind to any interface other than `127.0.0.1`. Must not read config from any path other than `/etc/corp-app/config.ini`. Must not attempt to create the config file or its directory.

**Interface agreements:**
- The config section names and key names the application reads (`[database]` → `host`, `port`, `name`, `user`, `password`; `[server]` → `port`) must exactly match what the Ansible template (`config.ini.j2`) writes. The application does **not** read a `bind_address` key; loopback binding is hardcoded (see behavioural contract above), so no such key may appear in its read-set. Any divergence silently breaks the application (G12).
- The health endpoint response body structure (`database`, `detail` keys) must match what the `app_instance` role's `assert` task checks.

**Applicable constraints:** G12.

---

## 7. Ansible Project (`ansible/`)

### 7.1 `ansible/.gitignore`

**Must declare:** Gitignore patterns that prevent private key files and the vault password file from being committed. At minimum: `keys/id_bootstrap`, `keys/id_bootstrap.pub`, `keys/id_live`, `keys/id_live.pub`, `keys/.vault_pass`.

---

### 7.2 `ansible/ansible.cfg`

**Purpose:** Control-node configuration. Makes the project runnable from a plain checkout.

**Must declare (under `[defaults]`):**
- `inventory = inventory/hosts.yml` (relative path; G11).
- `roles_path = roles` (relative path; G11).
- `vault_password_file = keys/.vault_pass` (relative path; G11).
- `private_key_file = keys/id_live` — the live key is the default for all plays. The bootstrap playbook overrides this per-play for the PSK phase.
- `remote_user = ansible` — all plays connect as the management account.
- `host_key_checking = False` with a comment that this is a demo convenience and that production should use `accept-new` or a pre-populated `known_hosts`.

**Must declare (under `[ssh_connection]`):**
- `pipelining = True` (G10, mandatory). Include a comment explaining why (correct sudo behaviour with NOPASSWD).

**Must not do:** Set `pipelining` under `[defaults]` — it must be under `[ssh_connection]`.

**Applicable constraints:** G10, G11.

---

### 7.3 `ansible/requirements.yml`

**Must declare:** Two collection dependencies:
- `community.mysql` (version constraint ≥ 3.0.0) — provides `mysql_db` and `mysql_user` modules.
- `ansible.posix` (version constraint ≥ 1.5.0) — provides `authorized_key` and `firewalld` modules.

Include a comment on each entry describing which modules it provides.

---

### 7.4 `ansible/site.yml`

**Purpose:** Master playbook entrypoint for standalone execution.

**Must declare:** Three `import_playbook` statements in order: `playbooks/00_bootstrap.yml`, `playbooks/01_database.yml`, `playbooks/02_application.yml`.

Include a header comment explaining standalone usage vs. AAP job-template split.

---

### 7.5 `ansible/inventory/hosts.yml`

**Must declare:**
- Two host groups: `database` (containing `db01`) and `application` (containing `app01`), both as children of `all`.
- An `ansible_host` IP address for each host (lab-specific placeholders that implementors replace).

**Interface agreements:**
- The IP of `app01` (`ansible_host`) must equal the value of `app_host_address` in `group_vars/database/vars.yml` (used by the `mysql_instance` role for both account host restriction and firewall rule).
- The IP of `db01` (`ansible_host`) must equal the value of `db_host` in `group_vars/application/vars.yml` (used by the `app_instance` role in the config template).

---

### 7.6 `ansible/inventory/group_vars/` — Variable Schema

The following table is the normative variable schema. Every variable must appear in exactly the declared file.

| Variable | File | Sensitive | Consumed by | Notes |
|----------|------|-----------|-------------|-------|
| `ansible_user` | `all/vars.yml` | No | all connection setup | Must match the baked account name (`ansible`) |
| `bootstrap_pubkey_path` | `all/vars.yml` | No | `bootstrap_key_rotation` | Relative to `ansible/` run directory |
| `live_pubkey_path` | `all/vars.yml` | No | `bootstrap_key_rotation` | Relative to `ansible/` run directory |
| `app_db_name` | `all/vars.yml` | No | `mysql_instance`, `app_instance` | Shared; must be visible to both groups |
| `app_db_user` | `all/vars.yml` | No | `mysql_instance`, `app_instance` | Shared; must be visible to both groups |
| `app_db_password` | `all/vault.yml` | **Yes** | `mysql_instance`, `app_instance` | G4: this file only |
| `app_host_address` | `database/vars.yml` | No | `mysql_instance` | IP of app01; must match `hosts.yml` `ansible_host` |
| `db_host` | `application/vars.yml` | No | `app_instance` | IP of db01; must match `hosts.yml` `ansible_host` |
| `app_port` | `application/vars.yml` | No | `app_instance` | Health endpoint port (loopback); must match in config.ini and URI check |

**Why `app_db_name` and `app_db_user` are in `all/vars.yml`:** The `mysql_instance` role (running against the `database` group) creates the account; the `app_instance` role (running against the `application` group) writes the account name to the config template. A group-specific variable is not visible cross-group. These non-sensitive shared values belong in `all/vars.yml`.

**`database/vault.yml` and `application/vault.yml`:** Placeholder files. No group-specific secrets exist in this design. They must exist (for structural completeness and to allow future additions) but may be empty vault files.

**File layout is normative (directory form):** Each group's variables live in a directory named exactly as the group (`all/`, `database/`, `application/`), with a `vars.yml` (plaintext) and a `vault.yml` (vault-encrypted) inside. The `host_group_vars` plugin auto-loads any file inside a directory named as the group; it does **not** load a flat file whose stem is not the group name. A flat `all.vault.yml` has stem `all.vault` and would never be loaded, silently stripping `app_db_password` from scope. The directory form is therefore mandatory, not stylistic.

**Applicable constraints:** G4.

---

### 7.7 `ansible/playbooks/00_bootstrap.yml`

**Purpose:** PSK probe → key rotation → live key verification. The single discrete onboarding unit. AAP-separable: this playbook runs with the bootstrap credential; workload playbooks run with the live credential.

**Must implement exactly three plays, in this order:**

**Play 1 — Probe via PSK:**
- `hosts: all`, `gather_facts: false`, `ignore_unreachable: true`.
- Override `ansible_ssh_private_key_file` in play `vars:` to point to `keys/id_bootstrap` (relative to playbook; resolve via `playbook_dir`).
- Attempt `ansible.builtin.ping` and register the result.
- On success: `set_fact: psk_reachable: true`. This fact persists across plays in the same run. Hosts that fail or are unreachable do not have `psk_reachable` set.

**Play 2 — Rotate keys on hosts where PSK worked:**
- `hosts: all`, `gather_facts: false`, `ignore_unreachable: true`.
- Same PSK key override as Play 1.
- Include the `bootstrap_key_rotation` role.
- The role include (or the task that calls it) must be gated with `when: psk_reachable | default(false)`. This is the unambiguous discriminator: an unreachable host is not "failed probe, skip rotation"; only explicit probe success drives rotation.

**Play 3 — Verify live key access:**
- `hosts: all`, `gather_facts: false`.
- No `ansible_ssh_private_key_file` override — uses `ansible.cfg` default (live key).
- `ansible.builtin.ping`. This play fails loudly if any host is unreachable via the live key, providing the post-rotation check.

**Must not do:** Combine probe and rotation into a single play. The three-play structure is normative because it enables AAP credential isolation at the job-template boundary.

---

### 7.8 `ansible/playbooks/01_database.yml`

**Must declare:** `hosts: database`, `gather_facts: true`, `become: true`, apply role `mysql_instance`.

Include a comment noting AAP usage: run as a separate job template with the live machine credential, after `00_bootstrap.yml` completes.

---

### 7.9 `ansible/playbooks/02_application.yml`

**Must declare:** `hosts: application`, `gather_facts: true`, `become: true`, apply role `app_instance`.

Include a comment noting AAP usage: run as a separate job template with the live machine credential, after `01_database.yml` completes.

---

## 8. Ansible Role Contracts

### 8.1 Role: `bootstrap_key_rotation`

**Purpose:** Installs the live management key and removes the bootstrap (PSK) key from the `ansible` account's `authorized_keys` file.

**Must implement (tasks):**
1. Install the live public key using `ansible.posix.authorized_key` with `state: present` and `exclusive: false`. Read the key content from the local file path declared in `live_pubkey_path` using a `lookup('file', ...)`. The module matches keys by fingerprint and is idempotent.
2. Remove the bootstrap public key using `ansible.posix.authorized_key` with `state: absent`. The module matches by key content/fingerprint and correctly identifies and removes the entry even if it has the `no-pty,...` options prefix.

**Must not do:**
- Use `become: true` or any privilege escalation. The `ansible` account owns its own `~/.ssh/authorized_keys`; no elevation is required or appropriate.
- Use `lineinfile`, `shell`, or `command` to modify `authorized_keys`. Only `ansible.posix.authorized_key` is permitted.
- Remove keys other than the bootstrap key (do not use `exclusive: true` on the removal task).

**Idempotency guarantee:** If the live key is already present and the PSK already absent, both tasks are no-ops. Re-running onboarding against an already-onboarded host must produce no changes.

**`defaults/main.yml` contract:** Must declare the role's overridable operational knobs with sensible defaults: the account name used in the `authorized_key` `user:` field (default `ansible`, agreeing with G2's name pin), and fallback local paths for the live and bootstrap public-key files consumed by the tasks (sensible defaults under `keys/`, overridden by the `live_pubkey_path` / `bootstrap_pubkey_path` values from `group_vars/all/vars.yml`). Must **not** declare any secret, the `psk_reachable` probe fact (set at play scope), or any value whose authoritative home is the inventory `group_vars` tree.

**Applicable constraints:** G2, G3.

---

### 8.2 Role: `mysql_instance`

**Purpose:** Provisions the MariaDB instance for runtime use: creates the application database and account, and narrows the firewall rule.

**Must implement (tasks, in this order):**
1. Wait for the MariaDB unix socket to exist (`ansible.builtin.wait_for` on `/var/lib/mysql/mysql.sock`, `state: present`, with a reasonable timeout). This proves the service has completed initialization before any DB work begins.
2. Create the application database using `community.mysql.mysql_db` with `login_unix_socket: /var/lib/mysql/mysql.sock`. No login password; root access is via the unix socket on a fresh install.
3. Create the application account using `community.mysql.mysql_user` with:
   - `host: "{{ app_host_address }}"` — the account is valid only for connections from the application host IP (not `localhost`, not `%`; the app connects over the network).
   - `password: "{{ app_db_password }}"` (G4).
   - `priv` scoped to the application database only.
   - `login_unix_socket: /var/lib/mysql/mysql.sock`.
4. Remove the broad `mysql` firewall service (image-baked, open to all) using `ansible.posix.firewalld` with `service: mysql`, `state: disabled`, `permanent: true`, `immediate: true`.
5. Add a rich rule allowing MySQL only from `app_host_address` using `ansible.posix.firewalld` with a rich rule in the form `rule family='ipv4' source address='{{ app_host_address }}/32' service name='mysql' accept`, `state: enabled`, `permanent: true`, `immediate: true`.

**Must not do:**
- Set a MariaDB root password. Root access is always via the unix socket.
- Use `become: false` — the play sets `become: true`; DB socket access and firewall management require root.
- Install any packages (G9).

**`defaults/main.yml` contract:** Must declare the role's overridable operational knobs with sensible defaults: the MariaDB unix socket path (default `/var/lib/mysql/mysql.sock`), the `wait_for` socket timeout (a conservative default allowing first-boot initialization), the firewall service name narrowed at runtime (default `mysql`), and the privilege grant string applied to the application account (scoped to the application database). Must **not** declare `app_db_password` (G4), nor the shared/instance values `app_db_name`, `app_db_user`, or `app_host_address` — these are resolved from `group_vars` (`all/` and `database/`) and must not be shadowed by a role default.

**Applicable constraints:** G1 (firewalld module uses the running daemon; this is a runtime task, not a build step — `ansible.posix.firewalld` with `immediate: true` is correct here), G4, G9.

---

### 8.3 Role: `app_instance`

**Purpose:** Writes the application runtime configuration, enables and starts the service, and verifies the health endpoint reports a successful database connection.

**Must implement (tasks, in this order):**
1. Ensure `/etc/corp-app/` exists with mode `0750`, owner `root:root`. The service runs as root and reads this directory.
2. Deploy the config from template `config.ini.j2` to `/etc/corp-app/config.ini` with mode `0640`, owner `root:root`. Notify a handler to restart the service if the config changes. (G12: this path must match `CONFIG_PATH` in `corp-app.py`.)
3. Enable and start `corp-app` using `ansible.builtin.systemd` with `state: started`, `enabled: true`, `daemon_reload: true` (G7: Ansible does the enabling; the image does not).
4. Wait for the health endpoint with `ansible.builtin.uri` targeting `http://127.0.0.1:{{ app_port }}/health`, `status_code: 200`, with a retry loop (sufficient retries and delay to allow the service to connect to the database).
5. Assert that the response body contains `database == "connected"`.

**Must implement (handler):** `Restart corp-app` — runs `ansible.builtin.systemd` with `state: restarted`, `daemon_reload: true`.

**Must implement (template `config.ini.j2`):** A valid `configparser`-readable INI file with two sections:
- `[database]` containing exactly: `host`, `port`, `name`, `user`, `password`. The key names must match what `corp-app.py` reads (G12).
- `[server]` containing exactly: `port`. No `bind_address` key — the application hardcodes loopback (`127.0.0.1`) binding and does not read `bind_address`; writing one would be a dead key that falsely implies the binding is configurable.

All values rendered from the variable schema in Section 7.6.

**Must not do:** Open any firewall port (the health endpoint is loopback-only). Must not install packages (G9).

**`defaults/main.yml` contract:** Must declare the role's overridable operational knobs with sensible defaults: the config directory mode (default `0750`) and config file mode (default `0640`), the systemd unit name managed by the role (default `corp-app`), and the health-endpoint poll parameters (retry count and delay). Must **not** declare `app_db_password` (G4), nor the shared/instance values `app_db_name`, `app_db_user`, `db_host`, or `app_port` — these are resolved from `group_vars` (`all/` and `application/`) and must not be shadowed by a role default. The config path is fixed by G12 and must not be made an overridable default.

**Applicable constraints:** G4, G7, G9, G12.

---

## 9. Interface Agreement Summary

The following cross-component agreements must hold. A violation in any one file breaks the system silently or at runtime.

| Agreement | Files that must agree | Value |
|-----------|-----------------------|-------|
| Management account UID/GID | `sysusers.d/ansible.conf`, base Containerfile `chown`, numeric `owner:`/`group:` directives | UID 5000, GID 5000 |
| Management account name | `sysusers.d/ansible.conf` user entry, `authorized_key` module `user:`, `ansible_user`/`remote_user` | `ansible` |
| PSK file path | `scripts/generate-demo-keys.sh` output path, base Containerfile `COPY rootfs/` target, `authorized_key` module target | `/var/home/ansible/.ssh/authorized_keys` |
| Tuned profile name | Database Containerfile `echo corp-database > /etc/tuned/active_profile`, `rootfs/usr/lib/tuned/corp-database/` directory name | `corp-database` |
| Application config path | `corp-app.py` hardcoded `CONFIG_PATH`, `app_instance` template `dest:` | `/etc/corp-app/config.ini` |
| Config INI keys | `corp-app.py` config reads, `config.ini.j2` template writes | `[database]`: `host`, `port`, `name`, `user`, `password`; `[server]`: `port` |
| App host IP | `hosts.yml` `app01.ansible_host`, `group_vars/database/vars.yml` `app_host_address` | Must be identical |
| DB host IP | `hosts.yml` `db01.ansible_host`, `group_vars/application/vars.yml` `db_host` | Must be identical |
| App DB password variable name | `all/vault.yml`, `mysql_instance` role `password:`, `config.ini.j2` template | `app_db_password` |
| Bootstrap key path (Ansible) | `group_vars/all/vars.yml` `bootstrap_pubkey_path`, `00_bootstrap.yml` play vars `ansible_ssh_private_key_file`, `bootstrap_key_rotation` role defaults | `keys/id_bootstrap` / `keys/id_bootstrap.pub` (relative to `ansible/`) |

---

## 10. PSK Lifecycle Protocol

This protocol is an invariant. Any implementation that produces a different state at any lifecycle point is incorrect.

| Phase | `authorized_keys` state | Mechanism |
|-------|------------------------|-----------|
| Image build | PSK entry (with restrictive options), owned 5000:5000, mode 0600 | `scripts/generate-demo-keys.sh` writes the file; Containerfile sets ownership/mode |
| First boot | PSK entry present and usable | bootc applies `/var` tree at initial install only |
| After `00_bootstrap.yml` | Live key only; PSK absent | `bootstrap_key_rotation` role installs live, removes PSK |
| `bootc upgrade` | Unchanged (live key only) | `/var` is never merged or re-applied by bootc upgrade |
| Reboot | Unchanged | `tmpfiles.d` has only `d` entries for `.ssh/`; existing file is not recreated |
| Re-run `00_bootstrap.yml` | Unchanged | Play 1 probe fails (PSK gone); Play 2 skipped; Play 3 verifies live key still works |
| Stale host (provisioned from old image, new PSK generated) | Cannot be onboarded with new PSK | Host must be decommissioned and reprovisioned; never rescued |

**No resurrection path:** After rotation, nothing re-creates the PSK entry — not upgrades, not reboots, not re-running playbooks. The only way to restore first-contact behaviour is a fresh provisioning from the image (which re-applies `/var`).

---

## 11. AAP Compatibility Contracts

The following design decisions are required to make the playbooks run unchanged under AAP while also running as a single `ansible-playbook` invocation locally.

**Job template split (mandatory):**

| Job | Playbook(s) | AAP credential type |
|-----|-------------|---------------------|
| Bootstrap | `00_bootstrap.yml` | Machine credential — bootstrap private key |
| Workload | `01_database.yml`, `02_application.yml` | Machine credential — live private key |

**Why the split is necessary:** AAP allows exactly one machine credential per job template. `00_bootstrap.yml` needs the bootstrap key for Plays 1–2 and the live key for Play 3. Locally, the playbook handles this via per-play `ansible_ssh_private_key_file` overrides. Under AAP, Play 3 (live key verification) is moved to the start of the workload job — or the bootstrap job is run with the bootstrap credential and the workload job with the live credential (which implicitly verifies connectivity).

**Vault → AAP credentials/survey:** Under AAP, vault files are replaced by AAP custom credentials or survey variables. Variable names must be identical to what roles reference (`app_db_password`, etc.) — roles work unchanged. The README must document this translation explicitly.

**The README must contain a dedicated "AAP Compatibility" section** covering the credential split, the one-credential-per-job constraint, and the vault-to-credential translation. This is a documentation mandate, not optional (per `docs/plan.md`).

---

## 12. Implementation Tasks

Each task is a discrete, independently reviewable unit. Dependencies are listed; a task must not begin until its dependencies are complete.

---

### Task 1 — Key Generation Infrastructure

**Branch slug:** `task/keygen-script`

**Files created or modified:**
- `scripts/generate-demo-keys.sh` (create)
- `ansible/keys/.gitignore` (create)
- Root-level `.gitignore` (create or modify — add pattern for `images/base/rootfs/var/home/ansible/.ssh/authorized_keys`)
- `images/base/rootfs/var/home/ansible/.ssh/` directory (create directory tree; no committed file content in `.ssh/` beyond the directory itself)

**Inputs required:**
- Repository root exists.

**Applicable constraints:** G2, G3, G13.

**Acceptance criteria:**
- Running `bash scripts/generate-demo-keys.sh` from the repo root completes without error and produces `ansible/keys/id_bootstrap`, `ansible/keys/id_bootstrap.pub`, `ansible/keys/id_live`, `ansible/keys/id_live.pub`.
- `images/base/rootfs/var/home/ansible/.ssh/authorized_keys` exists after the script runs and contains exactly one line beginning with `no-pty,no-X11-forwarding,no-agent-forwarding,no-user-rc ssh-ed25519`.
- Running the script a second time without deleting the keys exits with a non-zero code and prints an error message mentioning "Keys already exist."
- `git status` confirms `authorized_keys` and all four key files are gitignored (not staged, not tracked).
- The script is not inside `ansible/` or `images/` (G13).

**Dependencies:** None.

---

### Task 2 — Base Bootc Image

**Branch slug:** `task/base-image`

**Files created or modified:**
- `images/base/Containerfile` (create)
- `images/base/rootfs/usr/lib/sysusers.d/ansible.conf` (create)
- `images/base/rootfs/usr/lib/tmpfiles.d/ansible-home.conf` (create)
- `images/base/rootfs/etc/ssh/sshd_config.d/50-corporate-hardening.conf` (create)
- `images/base/rootfs/etc/sudoers.d/99-ansible` (create)

**Inputs required:**
- Task 1 complete (the `.ssh/` directory structure exists; the script can generate `authorized_keys` before the image build).
- `authorized_keys` generated (run `scripts/generate-demo-keys.sh` before building).

**Applicable constraints:** G1, G2, G3, G8, G9.

**Acceptance criteria:**
- `podman build -t corp-base:latest images/base/` completes without error.
- `bootc container lint corp-base:latest` exits 0. The lint output includes a warning about `/var` content; confirm the warning references `authorized_keys`; the build must not be modified to suppress it.
- Inside the built image: `authorized_keys` has numeric owner `5000:5000`, mode `0600`; `.ssh` directory has mode `0700`.
- Inside the image: `/etc/sudoers.d/99-ansible` has mode `0440`.
- Inside the image: `cat /etc/tuned/active_profile` outputs `throughput-performance`.
- Inside the image: `systemctl is-enabled sshd && systemctl is-enabled firewalld && systemctl is-enabled tuned` all output `enabled`.
- Inside the image: `firewall-offline-cmd --list-services` includes `ssh`.
- Inside the image: `cat /usr/lib/sysusers.d/ansible.conf` declares UID 5000 and GID 5000.
- Inside the image: `cat /usr/lib/tmpfiles.d/ansible-home.conf` contains no `f` or `f~` entries.

**Dependencies:** Task 1.

---

### Task 3 — Database Bootc Image

**Branch slug:** `task/db-image`

**Files created or modified:**
- `images/database/Containerfile` (create)
- `images/database/rootfs/etc/my.cnf.d/00-corporate.cnf` (create)
- `images/database/rootfs/usr/lib/systemd/system/mariadb.service.d/10-corporate.conf` (create)
- `images/database/rootfs/usr/lib/tuned/corp-database/tuned.conf` (create)

**Inputs required:**
- Task 2 complete; `corp-base:latest` available locally.

**Applicable constraints:** G1, G5, G6, G9.

**Acceptance criteria:**
- `podman build -t corp-db:latest images/database/` completes without error.
- `bootc container lint corp-db:latest` exits 0.
- Inside the image: `cat /etc/tuned/active_profile` outputs `corp-database`.
- Inside the image: `/usr/lib/tuned/corp-database/tuned.conf` exists and contains `throughput-performance` as an include and `transparent_hugepages = never`.
- Inside the image: `/usr/lib/systemd/system/mariadb.service.d/10-corporate.conf` exists and contains `LimitNOFILE` (G5: confirm no equivalent file exists under `/etc/systemd/`).
- Inside the image: `cat /etc/my.cnf.d/00-corporate.cnf` contains `bind-address = 0.0.0.0` and `character-set-server`.
- Inside the image: `systemctl is-enabled mariadb` outputs `enabled`.
- Inside the image: `firewall-offline-cmd --list-services` includes `mysql`.
- Inside the image: `python3 -c "import pymysql"` exits 0.

**Dependencies:** Task 2.

---

### Task 4 — Application Bootc Image

**Branch slug:** `task/app-image`

**Files created or modified:**
- `images/application/Containerfile` (create)
- `images/application/rootfs/usr/lib/systemd/system/corp-app.service` (create)
- `images/application/rootfs/usr/local/bin/corp-app.py` (create)

**Inputs required:**
- Task 2 complete; `corp-base:latest` available locally.

**Applicable constraints:** G7, G9, G12.

**Acceptance criteria:**
- `podman build -t corp-app-img:latest images/application/` completes without error.
- `bootc container lint corp-app-img:latest` exits 0.
- Inside the image: `systemctl is-enabled corp-app` outputs `disabled` (G7).
- Inside the image: `stat /usr/local/bin/corp-app.py` shows execute bit set.
- Inside the image: `/usr/lib/systemd/system/corp-app.service` contains `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`.
- Inside the image: `grep CONFIG_PATH /usr/local/bin/corp-app.py` shows `/etc/corp-app/config.ini` (G12).
- Inside the image: the service unit does not contain `systemctl enable` or any self-enabling mechanism.
- Inside the image: `python3 -c "import pymysql"` exits 0.
- Inside the image: `firewall-offline-cmd --list-services` does not include any port or service not present in the base image (no http, no 8080).

**Dependencies:** Task 2.

---

### Task 5 — Ansible Project Scaffold

**Branch slug:** `task/ansible-scaffold`

**Files created or modified:**
- `ansible/.gitignore` (create)
- `ansible/ansible.cfg` (create)
- `ansible/requirements.yml` (create)
- `ansible/site.yml` (create)
- `ansible/inventory/hosts.yml` (create)
- `ansible/inventory/group_vars/all/vars.yml` (create)
- `ansible/inventory/group_vars/all/vault.yml` (create — plaintext; encryption is a deployment step)
- `ansible/inventory/group_vars/database/vars.yml` (create)
- `ansible/inventory/group_vars/database/vault.yml` (create — empty placeholder)
- `ansible/inventory/group_vars/application/vars.yml` (create)
- `ansible/inventory/group_vars/application/vault.yml` (create — empty placeholder)

**Inputs required:**
- Repository root exists.

**Applicable constraints:** G4, G10, G11.

**Acceptance criteria (run from `ansible/` directory):**
- `ansible-inventory --list` outputs valid JSON with `db01` in the `database` group and `app01` in the `application` group.
- `ansible-inventory --host db01` includes `app_db_name`, `app_db_user`, `app_host_address`, and `app_db_password` (once vault is configured).
- `ansible-inventory --host app01` includes `db_host`, `app_port`, `app_db_name`, `app_db_user`, and `app_db_password`.
- `cat ansible.cfg` contains `pipelining = True` under a `[ssh_connection]` section header (G10).
- `cat ansible.cfg` contains `private_key_file = keys/id_live` under `[defaults]`.
- `ansible-galaxy collection install -r requirements.yml` completes without error.
- `app_db_password` does not appear in any file other than `all/vault.yml` (G4).

**Dependencies:** None (can begin in parallel with other tasks).

---

### Task 6 — Bootstrap Key Rotation Role and Playbook

**Branch slug:** `task/bootstrap-role`

**Files created or modified:**
- `ansible/roles/bootstrap_key_rotation/defaults/main.yml` (create)
- `ansible/roles/bootstrap_key_rotation/tasks/main.yml` (create)
- `ansible/roles/bootstrap_key_rotation/meta/main.yml` (create)
- `ansible/playbooks/00_bootstrap.yml` (create)

**Inputs required:**
- Task 5 complete (Ansible project structure, `ansible.cfg`, inventory).
- Task 1 complete (key material exists and is reachable at the declared paths).

**Applicable constraints:** G2, G3.

**Acceptance criteria:**
- Running `00_bootstrap.yml` against a fresh host (PSK active): Play 1 succeeds; Play 2 installs the live key and removes the PSK; Play 3 connects via the live key.
- After running: `cat /var/home/ansible/.ssh/authorized_keys` on the host shows only the live key (no `no-pty,...` prefix line).
- Running `00_bootstrap.yml` again against the same host (now onboarded): Play 1 fails to connect (PSK gone); Play 2 skips the rotation task; Play 3 connects via live key. `authorized_keys` is unchanged.
- The role tasks use only `ansible.posix.authorized_key` (not `lineinfile`, `shell`, or `command`).
- The role runs without `become: true`.
- Play 1 and Play 2 have `ignore_unreachable: true` at the play level.
- Play 3 has no `ansible_ssh_private_key_file` override.

**Dependencies:** Task 5, Task 1.

---

### Task 7 — Database Role and Playbook

**Branch slug:** `task/db-role`

**Files created or modified:**
- `ansible/roles/mysql_instance/defaults/main.yml` (create)
- `ansible/roles/mysql_instance/tasks/main.yml` (create)
- `ansible/roles/mysql_instance/meta/main.yml` (create)
- `ansible/playbooks/01_database.yml` (create)

**Inputs required:**
- Task 5 complete (Ansible project structure).
- For execution: Task 6 complete (live key installed on DB host); Task 3 complete (database image deployed).

**Applicable constraints:** G4, G9.

**Acceptance criteria (against a running database VM):**
- `mysql -u root -S /var/lib/mysql/mysql.sock -e "SHOW DATABASES;"` includes the application database.
- `mysql -u root -S /var/lib/mysql/mysql.sock -e "SELECT user,host FROM mysql.user WHERE user='corp_app';"` shows the account with `host` equal to the configured `app_host_address` (not `localhost`, not `%`).
- `firewall-cmd --list-rich-rules` shows a rule for `app_host_address` on port 3306.
- `firewall-cmd --list-services` does not include `mysql` (broad service removed).
- Re-running `01_database.yml` produces no changes (idempotent).
- No task attempts to set a MariaDB root password.

**Dependencies:** Task 5. (Task 6 and Task 3 are required for live execution, not for file authoring.)

---

### Task 8 — Application Role, Template, and Playbook

**Branch slug:** `task/app-role`

**Files created or modified:**
- `ansible/roles/app_instance/defaults/main.yml` (create)
- `ansible/roles/app_instance/tasks/main.yml` (create)
- `ansible/roles/app_instance/handlers/main.yml` (create)
- `ansible/roles/app_instance/templates/config.ini.j2` (create)
- `ansible/roles/app_instance/meta/main.yml` (create)
- `ansible/playbooks/02_application.yml` (create)

**Inputs required:**
- Task 5 complete (Ansible project structure).
- For execution: Task 6 complete (live key on app host); Task 4 complete (app image deployed); Task 7 complete (DB account created).

**Applicable constraints:** G4, G7, G9, G12.

**Acceptance criteria (against a running application VM):**
- `/etc/corp-app/config.ini` exists with mode `0640`, owner `root:root`.
- `systemctl is-active corp-app` outputs `active`.
- `systemctl is-enabled corp-app` outputs `enabled` (G7: Ansible enabled it, not the image).
- `curl -s http://127.0.0.1:8080/health` (run on the app host) returns HTTP 200 and a JSON body where `database == "connected"`.
- The template `config.ini.j2` contains exactly the section and key names the application reads: `[database]` with `host`, `port`, `name`, `user`, `password`; `[server]` with `port` only — no `bind_address` key (G12).
- Re-running `02_application.yml` produces no changes (idempotent), or triggers the restart handler if config was touched (handler is idempotent).

**Dependencies:** Task 5. (Task 6, 4, 7 required for live execution.)

---

### Task 9 — README Documentation

**Branch slug:** `task/readme`

**Files created or modified:**
- `README.md` (create)

**Inputs required:**
- All previous tasks complete. Documentation covers the verified, complete system.

**Applicable constraints:** All — the README must accurately describe every constraint and design decision.

**Required sections (all must be present):**
1. **Architecture Overview** — bake-time vs. runtime boundary; the three-tier rule (platform → base image; workload standard → layer image; instance-specific → Ansible); configuration domains where the base image is the only layer (time sync, SELinux posture, firewall policy).
2. **Image Layering** — `FROM base` as the team boundary; why multi-stage builds are not needed; the `rootfs/` mirror-tree convention as documentation of deployed state.
3. **PSK Lifecycle** — install-only `/var` semantics; no-resurrection guarantee; the restrictive SSH options on the PSK line and their purpose; stale-host policy (reprovision, not rescue); expected `bootc container lint` `/var` warning documented as intentional.
4. **Key Generation → Image Build Ordering** — explicit step: run `scripts/generate-demo-keys.sh` before any image build; what happens if the order is reversed (key mismatch between image and generated keys).
5. **Setup and Execution** — all commands in runnable order: generate keys → build images → provision VMs (out of scope, cross-reference) → install collections → create vault password file → encrypt vault files → run playbook. Working directory noted for each command.
6. **Practitioner Notes** — all items listed in `docs/plan.md` Section "Documentation requirements (README)" must appear substantively.
7. **AAP Compatibility** — one-credential-per-job constraint; bootstrap job template vs. live job template split; vault-to-credential/survey translation with explicit mention that variable names are unchanged and roles work without modification.

**Acceptance criteria:**
- All seven sections are present.
- All practitioner notes items appear substantively (not necessarily verbatim).
- All shell commands are in code blocks with correct working directories noted.
- The `bootc container lint` `/var` warning is explicitly mentioned and explained.
- The AAP section contains the three required topics.

**Dependencies:** Tasks 1–8.

---

## Appendix A: bootc Filesystem Zone Summary

| Path prefix | Zone | bootc behaviour | Implication for this project |
|-------------|------|-----------------|------------------------------|
| `/usr/` | Image-owned | Replaced on upgrade; not editable at runtime (immutable mount) | Sysusers, tmpfiles, unit files, executables, tuned profiles |
| `/etc/` | Machine-local config | 3-way merge on upgrade; local changes survive; image version wins if no local change | SSH drop-in, sudoers, MariaDB config, app runtime config |
| `/var/` | Machine-local data | Applied at initial install only; never merged or re-applied by `bootc upgrade` | PSK `authorized_keys` — present at first contact, never resurrected |

**Key consequence for the PSK:** Because `/var` is applied only at initial install, the PSK survives only until Ansible removes it. `bootc upgrade` will not restore it. Reboots will not restore it (tmpfiles.d has only `d` entries). This is the "first-contact-only" guarantee.

---

## Appendix B: File Permission Reference

| File | Mode | Numeric owner | Enforcement mechanism |
|------|------|---------------|----------------------|
| `/var/home/ansible/.ssh/` | `0700` | `5000:5000` | Containerfile `chmod` + `chown` after `COPY rootfs/` |
| `/var/home/ansible/.ssh/authorized_keys` | `0600` | `5000:5000` | Containerfile `chmod` + `chown` after `COPY rootfs/` |
| `/etc/sudoers.d/99-ansible` | `0440` | `root:root` | Containerfile `chmod` after `COPY rootfs/` |
| `/usr/local/bin/corp-app.py` | `0755` | `root:root` | Containerfile `chmod` after `COPY rootfs/` |
| `/etc/corp-app/` | `0750` | `root:root` | `ansible.builtin.file` in `app_instance` role |
| `/etc/corp-app/config.ini` | `0640` | `root:root` | `ansible.builtin.template` in `app_instance` role |
| `scripts/generate-demo-keys.sh` | `0755` | committer | Set at commit time |
