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
| G7  | App unit is instance-owned, not baked | The application image must **not** bake the `corp-app.service` unit or any application code. The unit is rendered by Ansible to `/etc/systemd/system/corp-app.service` at release time and must not be enabled or started until the payload is deployed, dependencies are installed, `/etc/corp-app/config.ini` exists, and migrations have run. The image is a reusable Python *runtime baseline* — it carries no workload-specific unit, executable, or firewall port. (This supersedes the former "baked but not enabled" model: the app layer no longer ships app content at all.) |
| G8  | SSH policy delivery | SSH hardening policy is delivered exclusively through a drop-in file under `/etc/ssh/sshd_config.d/`. The base `sshd_config` file must never be modified or replaced directly. |
| G9  | No runtime **OS** package management; scoped Python exception | No Ansible task may install or update **operating-system** packages (`dnf`/`yum`/`rpm`/`package`/`dnf-automatic`). OS packages are bake-time only. **Scoped exception:** Ansible *may* install **application-level Python dependencies** at runtime, but only (a) into the application virtualenv under `/opt/corp-app/venv`, (b) with fully pinned versions (`==`, or an equivalent hash/lock), (c) never with `--upgrade`, unbounded, or floating specifiers, and (d) never into system `site-packages`. The corporate shared Python stack (DB drivers + data abstraction layer) is baked into the image, not installed at runtime (G15); runtime pip installs are limited to genuinely app-specific libraries. |
| G14 | Standard application deployment target | Application payloads deploy to `/opt/corp-app` with the virtualenv at `/opt/corp-app/venv`. No image bakes application code. On bootc, `/opt` resolves under `/var/opt` (persistent, install-only, preserved across upgrade/rollback), which is the correct zone for Ansible-owned instance payload. The venv must be created with access to system site-packages so the baked corporate stack (G15) is visible without re-installation. |
| G15 | Single corporate Python standard | The shared Python runtime — the DB driver set (`python3-PyMySQL`, `python3-psycopg2`) and the data-abstraction layer (SQLAlchemy, Alembic, Pydantic) — is defined **once**, baked into the application image, and is the single source of truth. Ansible roles and app teams must not re-install, shadow, or pin alternate versions of these shared components; they install only app-specific extras (G9 exception). This is what makes the image a reusable, centrally governed runtime baseline. |
| G16 | Pinned versions mandatory | Every runtime Python install performed by Ansible must specify an exact pinned version. Floating ranges, `latest`, and `--upgrade` are prohibited. (There is no internal/offline index; pinning is the reproducibility guarantee.) |
| G17 | Deployment vs. proof-of-life separation | "Real" deployment plays/roles must not contain functional application exercises. A single lightweight **readiness gate** (one `/health` reachability check with retry) is permitted in release to confirm the service came up. All functional verification — `GET /` seed-visibility assertions, `POST /record` write probes, negative-input tests, DB-side cross-checks — lives only in the optional `04_proof_of_life.yml`, which a user runs to demonstrate the system, never as a deployment prerequisite. |
| G18 | Operator tooling tiers | Tools baked into an image must be frequently needed and host-coupled. Packet capture (`tcpdump`) and other episodic/high-privilege diagnostics are **not** baked into any image — they are break-glass (transient manual `dnf install`, documented and non-durable) or one-shot container tooling. No Ansible task may perform a break-glass install; by definition it is a manual incident action (reinforced by G9). |
| G19 | External exposure is a deliberate, separate step (teaching moment) | Neither `02_app_deploy` nor `03_app_release` opens the application's external firewall port. The app binds `0.0.0.0` and the readiness gate and `04_proof_of_life` both run over **loopback on the app host**, so they pass while the service remains **externally unreachable**. Opening the external port is its own clearly-labelled, user-invoked step. This default-deny gap is intentional and must be documented as a teaching moment (default-deny posture; loopback reachability ≠ network reachability; runtime firewall ownership), never as a defect or an afterthought. A service that works on loopback but is unreachable from the network is the expected post-release state until a human deliberately exposes it. |
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
│   └── application/                  ← reusable corporate Python RUNTIME baseline
│       └── Containerfile             ← no rootfs/: bakes no app code or unit (G7, G14)
├── app/                             ← app-team owned payload (deployed to /opt/corp-app)
│   └── corp-app/
│       ├── requirements.txt         ← app-specific EXTRAS only, pinned (G9, G16)
│       ├── src/                     ← application package (SQLAlchemy/Pydantic; G15)
│       └── migrations/              ← Alembic migration tree (app-owned schema)
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
    │   ├── 00_bootstrap.yml          ← platform: PSK → live key onboarding
    │   ├── 01_database.yml           ← DBA: DB foundation only (no schema/seed)
    │   ├── 02_app_deploy.yml         ← app team play 1: deploy payload/venv/config/unit
    │   ├── 03_app_release.yml        ← app team play 2: migrate + start + readiness gate
    │   └── 04_proof_of_life.yml      ← optional user exercise: functional verification
    └── roles/
        ├── bootstrap_key_rotation/
        ├── mysql_instance/           ← DB foundation (infra), schema-free
        ├── app_deploy/               ← payload, venv, pinned deps, config, unit
        ├── app_release/              ← Alembic migrate, enable/start, readiness gate
        └── app_proof_of_life/        ← functional GET/POST + DB cross-checks (exercise)
```

> **Note on the `app/` tree:** The application source, its Alembic migrations, and its
> pinned `requirements.txt` live under a top-level `app/` directory, deliberately outside
> both `images/` and `ansible/`. This expresses the ownership model: the app is a payload
> deployed *onto* the runtime baseline, not content *of* the image and not logic *of* the
> control node. The `app_deploy` role reads from this tree (or a built artifact derived
> from it) and installs it to `/opt/corp-app`.

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

**Must install (package set):** `mariadb-server`, `mariadb` (CLI), `python3-PyMySQL`, and the DBA operator toolset `mariadb-backup`, `lsof`, `strace`, `procps-ng`, `sysstat`, `jq`. Rationale for each must appear as a comment. (G9: baked because Ansible must not install OS packages at runtime; `python3-PyMySQL` is required by both the MariaDB Ansible modules and any local DB client work. The DBA operator tools are baked because they are frequently needed and host-coupled — G18.)

**Must NOT install (tooling-tier policy, G18):** `tcpdump` and other episodic/high-privilege diagnostics must not be baked. These are explicitly break-glass (transient manual `dnf install` during an incident, documented and non-durable) or one-shot container tooling. A comment must call this out so the exclusion reads as a deliberate corporate-standard decision, not an omission.

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
- Bake `tcpdump` or other break-glass diagnostics (G18).

**Applicable constraints:** G1, G5, G6, G9, G18.

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

## 6. Application Runtime Image & Application Payload

The former "application image" is split into two distinct, separately-owned things:

- **The application runtime image** (`images/application/`) — a reusable corporate Python
  runtime baseline, owned by the app-platform/runtime team. It bakes **no** application
  code, unit, or firewall port (G7, G14). It is the single source of truth for the
  corporate Python stack (G15).
- **The application payload** (`app/corp-app/`) — the app-feature team's source, Alembic
  migrations, and pinned extra requirements. It is *deployed onto* the runtime image by
  Ansible (Sections 8.3–8.4), never baked.

### 6.1 `images/application/Containerfile`

**Purpose:** Reusable corporate Python application-host runtime baseline. Derives from the
base image; adds the corporate Python runtime bundle (Section 6.2). Carries no workload
code, no systemd unit, and opens no firewall port.

**FROM:** The locally-built base image tag (same tag as used by the database image).

**Must install (package set):** The corporate Python runtime bundle enumerated in Section
6.2. Each package/group must carry a rationale comment, and the three tiers (core runtime,
helper/packaging tooling, corporate DB+abstraction stack) must be visually distinguished so
the file reads as the corporate standard it is.

**Must configure:**
1. Install the RPM portion of the bundle and clean the package cache in a single layer.
2. For any corporate-stack component not available as an RPM on CentOS Stream 10, install it
   with `pip` **at build time** into the system interpreter, from a pinned requirement set
   (G16). This is a bake action, not a runtime action, and is therefore permitted. The
   pinned set must be committed and visible (a comment must point to it).

**Must not do:**
- Contain a `rootfs/` tree or any application code, executable, or systemd unit (G7, G14).
- Enable or reference `corp-app` in any way (G7). The unit does not exist in this image.
- Open any firewall port (G19). Port exposure is an instance decision made at runtime,
  deliberately separated as a teaching moment.
- Install OS packages via any mechanism other than the single bake-time layer (no runtime
  package management is introduced; G9).
- Re-declare or pin alternate versions of the corporate stack anywhere downstream (G15).

**Applicable constraints:** G7, G9, G14, G15, G16, G18, G19.

---

### 6.2 Corporate Python Runtime Bundle (baked set — single source of truth)

**Purpose:** Define, in one place, the corporate Python standard every application host
carries. This is the mechanism that makes "one corporate Python" real (G15) and minimises
per-app choices: an app team inherits this entire stack and chooses only genuinely
app-specific extras.

**Must include, grouped and commented as three tiers:**

1. **Core runtime:** `python3`, `python3-pip`, `ca-certificates`, `tzdata` (plus `virtualenv` and `wheel` installed via build-time pip, as they are not standalone AppStream RPMs in CentOS Stream 10).
2. **Helpers & packaging tooling:** `git`, `tar`, `gzip`, `unzip`, `python3-setuptools`.
3. **Corporate DB drivers + data-abstraction layer (explicitly called out as corporate
   standard, not universally required by every Python app):**
   - DB drivers: `python3-PyMySQL` (MariaDB/MySQL), `python3-psycopg2` (PostgreSQL). Two
     drivers are deliberate — they signal a *single corporate runtime* that standardises
     enterprise DB connectivity centrally rather than per-app.
   - Data-abstraction layer: SQLAlchemy, Alembic, Pydantic (RPM where packaged, else
     pinned build-time pip per Section 6.1 step 2).

**Must not do:** Include application-framework or app-specific libraries (those are the app
team's pinned extras, Section 6.4). Leave the DB/abstraction tier uncommented — the comment
distinguishing "corporate standard" from "universal runtime" is mandatory so the policy is
legible in the file.

**Interface agreements:** The abstraction layer baked here (SQLAlchemy/Pydantic) is what the
application payload (Section 6.3) imports, and Alembic baked here is what the release role
(Section 8.4) invokes. App venvs must be built with access to system site-packages (G14) so
these inherit without re-installation (G15).

**Applicable constraints:** G9, G14, G15, G16, G18.

---

### 6.3 `app/corp-app/` — Application Payload (source)

**Purpose:** The example application, owned by the app-feature team and deployed to
`/opt/corp-app` by Ansible. It is a genuine working instance demonstrating the corporate
Python stack end to end (SQLAlchemy data access, Pydantic request validation) against a
schema the app itself owns via Alembic. It remains deliberately small; the value is in the
ownership and deployment boundaries, not the app logic.

**Runtime/dependency constraints:**
- Imports only the corporate stack (SQLAlchemy, Pydantic, a DB driver) plus the Python
  standard library, and any app-specific extras declared in its pinned `requirements.txt`
  (Section 6.4). It must not vendor or pin its own copy of the corporate stack (G15).
- Reads configuration from exactly `/etc/corp-app/config.ini` — hardcoded, the contract with
  the `app_deploy` role template (G12). No env-var or CLI override.
- Exits non-zero with a clear message if the config file is missing or a required
  dependency is not importable.
- Listens on `0.0.0.0` at `[server] port` (default 8080). The bind address is a fixed
  property of the executable, not config-driven; actual network reachability is governed by
  the firewall, which is deliberately left closed until a separate user step (G19).
- `Content-Type: application/json` on all responses, including errors.

**Database interface:**
- Connects using the `[database]` section (`host`, `port` default 3306, `name`, `user`,
  `password`) via SQLAlchemy with a short connect timeout. Connection/session lifecycle must
  be released on every path including errors.
- The running service must **not** issue DDL. Schema creation and the seed row are owned by
  the app's Alembic migrations (Section 6.5), applied by the release role (Section 8.4)
  before the service is started — the bake/deploy/release division: the image provides the
  runtime, `app_deploy` installs the payload, `app_release` migrates and starts.

**Table contract (owned by the app's migrations — authoritative definition in Section 6.5):**
`service_records` with an auto-increment integer PK `id`, a non-null text `message`, and a
`created_at` timestamp defaulting to insertion time. The app depends only on these three
names.

**Routes (behavioural contract):**

1. `GET /` — service info + data read. Query the most recent rows from `service_records`
   (ordered by `id` DESC, bounded limit such as 50). Respond HTTP 200 with a JSON object
   carrying at minimum: a `service` string, a `database` status indicator, a `count` (total
   rows), and a `records` array whose elements expose at least `id`, `message`, `created_at`
   (timestamps serialized as strings). On DB failure: HTTP 503 with an error object.

2. `POST /record` — data write. Accept `Content-Type: application/json` with a `message`
   string; validation must use Pydantic. If the body is not valid JSON, or `message` is
   missing/empty/non-string, respond HTTP 400 with an error object and perform no write. On
   valid input: insert a row, read it back by its new PK, respond HTTP 201 with the inserted
   record (`id`, `message`, `created_at`). On DB failure: HTTP 503.

3. `GET /health` — connectivity + count. On success: HTTP 200 with `database == "connected"`,
   the record `count`, and a short `detail` such as `"ok"`. On failure: HTTP 503 with
   `database == "error"` and `detail` carrying the exception message. This is the endpoint
   the release readiness gate (Section 8.4) polls over loopback.

4. Any other method/path: HTTP 404 with a JSON error object; a known path with an
   unsupported method should be 405. Malformed `POST /record` input must be 400, never 404.

**Error contract:** Every non-2xx response carries a JSON body with at least a `detail` (or
`error`) string. The `/health` body must expose `database` (`"connected"`/`"error"`) and
`detail`. No non-JSON or empty bodies.

Example — `GET /` success body shape (illustrative only, not normative content to copy):
```
{"service": "corp-app", "database": "connected", "count": 1,
 "records": [{"id": 1, "message": "ansible-bootstrap", "created_at": "2026-01-01 00:00:00"}]}
```

**Must not do:** Read config from any path other than `/etc/corp-app/config.ini`. Create the
config file or its directory. Issue DDL or create/seed schema at request time (that is the
Alembic migrations' job, applied by `app_release`). Vendor or re-pin the corporate stack
(G15). Leak database connections/sessions on error paths.

**Interface agreements:**
- Config sections/keys the app reads (`[database]`: `host`, `port`, `name`, `user`,
  `password`; `[server]`: `port`) must match the `config.ini.j2` the `app_deploy` role
  renders (G12). No `bind_address` key.
- The table name `service_records` and columns `id`, `message`, `created_at` must match the
  Alembic migration (Section 6.5). The seed `message` value `ansible-bootstrap` created by
  the migration/release must be findable in `GET /` output (asserted by proof-of-life,
  Section 8.5).

**Applicable constraints:** G9, G12, G14, G15.

---

### 6.4 `app/corp-app/requirements.txt` — app-specific pinned extras

**Purpose:** Declares only the libraries the app needs **beyond** the baked corporate stack,
each fully pinned. This is the one place an app team expresses dependency choices (G15: few
choice points).

**Must declare:** Exact-pinned (`==`) versions for any app-only dependency. The corporate
stack (SQLAlchemy, Alembic, Pydantic, DB drivers) must **not** be listed here — it is
inherited from the image (G15). If the app needs nothing beyond the corporate stack, the
file exists but may be empty with an explanatory comment.

**Must not do:** Use floating ranges, `>=`, `latest`, or unpinned entries (G16). List the
corporate stack (G15).

**Applicable constraints:** G9, G15, G16.

---

### 6.5 `app/corp-app/migrations/` — Alembic migration tree (app-owned schema)

**Purpose:** The application owns its own schema. The initial migration creates
`service_records` and inserts the single bootstrap seed row (`ansible-bootstrap`). This
replaces the former DBA-role schema/seed tasks: schema is app-team territory, applied with
the corporate Alembic standard.

**Must declare:**
- An Alembic environment that reads the DB connection from the same runtime config the app
  uses (`/etc/corp-app/config.ini`) or an equivalent agreed source, so migrations and the
  app target the identical database.
- An initial migration creating `service_records` (`id` INT AUTO_INCREMENT PK; `message`
  text NOT NULL; `created_at` timestamp defaulting to insertion time), inheriting the
  database's `utf8mb4`/`utf8mb4_unicode_ci` baseline (Section 5.2) — no per-table override.
- A seed of exactly one row with `message = ansible-bootstrap`, applied idempotently (a
  re-run must not duplicate it). This is the bootstrap state the proof-of-life exercise
  (Section 8.5) asserts is visible, proving the app-owned migration reached the data layer.

**Must not do:** Depend on a table name or columns other than the contract above (must agree
with Section 6.3). Require DDL privileges beyond the app database (the DB account is scoped
to its own database, Section 8.2).

Example — initial schema shape (illustrative only, not normative content to copy):
```
service_records(id INT AUTO_INCREMENT PRIMARY KEY,
                message VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
```

**Applicable constraints:** G9, G15, G16.

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
- `ansible.mariadb` (version constraint ≥ 6.0.0) — provides `mariadb_db` and `mariadb_user`, used by the `mysql_instance` role for the DB foundation (database + account + privileges). Schema is owned by the app's Alembic migrations, so `mariadb_query` is no longer required by the DB role. (Replaces `community.mysql`, which drops MariaDB support in 6.0.0.)
- `ansible.posix` (version constraint ≥ 1.5.0) — provides `authorized_key` and `firewalld`, used by `bootstrap_key_rotation` and the firewall tasks.

Include a comment on each entry describing which modules it provides. The app deployment
roles (`app_deploy`, `app_release`) use `ansible.builtin` modules (`pip`, `command`/`shell`
for Alembic, `template`, `systemd`, `uri`, `copy`/`synchronize`) and introduce no new
collection dependency.

---

### 7.4 `ansible/site.yml`

**Purpose:** Master playbook entrypoint for standalone execution of the *deployment*
sequence.

**Must declare:** `import_playbook` statements, in order, for the four deployment phases:
`playbooks/00_bootstrap.yml`, `playbooks/01_database.yml`, `playbooks/02_app_deploy.yml`,
`playbooks/03_app_release.yml`.

**Must not do:** Import `playbooks/04_proof_of_life.yml`. Proof-of-life is an optional,
user-invoked verification exercise, never a deployment step (G17). It is run separately and
documented as such. A header comment must state this explicitly, alongside the standalone
vs. AAP job-template split.

Include a header comment explaining the ownership of each phase (platform → DBA → app team ×2)
and that `04_proof_of_life.yml` is run by hand after deployment.

---

### 7.5 `ansible/inventory/hosts.yml`

**Must declare:**
- Two host groups: `database` (containing `db01`) and `application` (containing `app01`), both as children of `all`.
- An `ansible_host` IP address for each host (lab-specific placeholders that implementors replace).

**Interface agreements:**
- The IP of `app01` (`ansible_host`) must equal the value of `app_host_address` in `group_vars/database/vars.yml` (used by the `mysql_instance` role for both account host restriction and firewall rule).
- The IP of `db01` (`ansible_host`) must equal the value of `db_host` in `group_vars/application/vars.yml` (used by the `app_deploy` role in the config template).
- The IP of `app01` (`ansible_host`) is also the target the `04_proof_of_life` exercise connects to over the network (from the control node) when demonstrating external reachability after the firewall-exposure step (G19).

---

### 7.6 `ansible/inventory/group_vars/` — Variable Schema

The following table is the normative variable schema. Every variable must appear in exactly the declared file.

| Variable | File | Sensitive | Consumed by | Notes |
|----------|------|-----------|-------------|-------|
| `ansible_user` | `all/vars.yml` | No | all connection setup | Must match the baked account name (`ansible`) |
| `bootstrap_pubkey_path` | `all/vars.yml` | No | `bootstrap_key_rotation` | Relative to `ansible/` run directory |
| `live_pubkey_path` | `all/vars.yml` | No | `bootstrap_key_rotation` | Relative to `ansible/` run directory |
| `app_db_name` | `all/vars.yml` | No | `mysql_instance`, `app_deploy`, `app_release` | Shared; must be visible to both groups |
| `app_db_user` | `all/vars.yml` | No | `mysql_instance`, `app_deploy`, `app_release` | Shared; must be visible to both groups |
| `app_db_password` | `all/vault.yml` | **Yes** | `mysql_instance`, `app_deploy`, `app_release` | G4: this file only |
| `app_host_address` | `database/vars.yml` | No | `mysql_instance` | IP of app01; must match `hosts.yml` `ansible_host` |
| `db_host` | `application/vars.yml` | No | `app_deploy` | IP of db01; must match `hosts.yml` `ansible_host` |
| `app_port` | `application/vars.yml` | No | `app_deploy`, `app_release`, `app_proof_of_life` (firewall-exposure step + external check) | Application listen port; must match `[server] port` in config.ini, the firewall rule, the readiness gate, and the proof-of-life check |

**Why `app_db_name` and `app_db_user` are in `all/vars.yml`:** The `mysql_instance` role (running against the `database` group) creates the account; the `app_deploy` role (running against the `application` group) writes the account name to the config template, and `app_release` uses it when running migrations. A group-specific variable is not visible cross-group. These non-sensitive shared values belong in `all/vars.yml`.

**Application deployment variables (`application/vars.yml`, non-sensitive):** The deployment
standard (G14) is expressed as overridable variables so the pattern is reusable, with
sensible defaults:
- `app_root` (default `/opt/corp-app`) — payload install root.
- `app_venv` (default `{{ app_root }}/venv`) — virtualenv path; created with system
  site-packages so the baked corporate stack is visible (G15).
- `app_src` — path to the payload source (the `app/corp-app/` tree or a built artifact).
- `app_entrypoint` — the venv entrypoint the systemd unit's `ExecStart` invokes.
These belong to the `application` group only. None is sensitive; none may carry a secret.
The config path `/etc/corp-app/config.ini` is **not** a variable (fixed by G12).

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

### 7.8 `ansible/playbooks/01_database.yml` — DB foundation (DBA-owned)

**Must declare:** `hosts: database`, `gather_facts: true`, `become: true`, apply role `mysql_instance`.

**Scope:** Database *foundation* only — DB, accounts, privileges, firewall narrowing. It
creates **no** application schema and **no** seed data; those are app-team territory, applied
by the app's Alembic migrations in `03_app_release` (G17, Section 6.5).

Include a comment noting ownership (DBA team) and AAP usage: run as a separate job template
with the live machine credential, after `00_bootstrap.yml` completes.

---

### 7.9 `ansible/playbooks/02_app_deploy.yml` — app deploy (app-team play 1)

**Must declare:** `hosts: application`, `gather_facts: true`, `become: true`, apply role `app_deploy`.

**Scope (deploy, not go-live):** Install the payload to `/opt/corp-app`, create the venv with
system site-packages, install app-specific pinned extras, render `/etc/corp-app/config.ini`,
and render + install the `/etc/systemd/system/corp-app.service` unit with `daemon_reload`.
The service is **not** enabled/started here, migrations are **not** run here, and the
firewall port is **not** opened here (G17, G19).

Include a comment: app-feature team ownership, play 1 of 2; AAP = part of the workload job
(live credential), after `01_database.yml`.

---

### 7.10 `ansible/playbooks/03_app_release.yml` — app release / go-live (app-team play 2)

**Must declare:** `hosts: application`, `gather_facts: true`, `become: true`, apply role `app_release`.

**Scope (go-live):** Run the app's Alembic migrations (creating `service_records` + the seed
row) using the deployed venv, then enable and start `corp-app`, then run a **single loopback
readiness gate** (`127.0.0.1:{{ app_port }}/health` returns 200 with `database == "connected"`,
with retries). It performs **no** functional GET/POST exercises (those are proof-of-life,
G17) and does **not** open the external firewall port (G19).

Include a comment: app-feature team ownership, play 2 of 2; the deliberate omission of the
firewall-exposure step is a teaching moment documented in the README.

---

### 7.11 `ansible/playbooks/04_proof_of_life.yml` — optional verification exercise (user-run)

**Must declare:** Not imported by `site.yml` (G17). Run by a user by hand after deployment.

**Scope (two deliberate parts, both optional and user-invoked):**
1. **Firewall-exposure step (the teaching moment, G19):** an explicitly-labelled task/play
   that opens `app_port` on the application host via `ansible.posix.firewalld`
   (`permanent: true`, `immediate: true`). A comment must frame this as the deliberate act of
   exposing the service — before it, external access fails by default-deny; after it, the
   service is reachable from the network.
2. **Functional verification via role `app_proof_of_life`:** exercise the application over the
   **network from the control node** (`delegate_to: localhost`, URL built from the app host
   IP and `app_port`): assert `GET /health` connected, assert the `ansible-bootstrap` seed is
   visible in `GET /`, perform a `POST /record` write-verification, and assert a malformed
   `POST /record` is rejected 400. These prove the full round trip and the external path a
   real user would use.

Include a header comment: this playbook demonstrates two lessons — (a) a service can be fully
working on loopback yet unreachable until deliberately exposed (default-deny; loopback ≠
network), and (b) functional verification is a separate concern from deployment (G17). The
comment must state that re-running it adds harmless write-probe rows (documented
non-idempotent probe).

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

### 8.2 Role: `mysql_instance` (DB foundation — DBA-owned)

**Purpose:** Provisions the MariaDB *foundation* for runtime use: the application database, the
application account with privileges on its own database, and the runtime firewall narrowing. It
does **not** create application schema or seed data — those are owned by the app team's Alembic
migrations (Sections 6.5, 8.4). This is the ownership boundary: the DBA team provisions the
container (database + account + access); the app team fills it (schema + data).

**Must implement (tasks, in this order):**
1. Wait for the MariaDB unix socket to exist (`ansible.builtin.wait_for` on `/var/lib/mysql/mysql.sock`, `state: present`, with a reasonable timeout). This proves the service has completed initialization before any DB work begins.
2. Create the application database using `ansible.mariadb.mariadb_db` with `login_unix_socket: /var/lib/mysql/mysql.sock`. No login password; root access is via the unix socket on a fresh install.
3. Create the application account using `ansible.mariadb.mariadb_user` with:
   - `host: "{{ app_host_address }}"` — the account is valid only for connections from the application host IP (not `localhost`, not `%`; the app connects over the network).
   - `password: "{{ app_db_password }}"` (G4).
   - `priv` scoped to the application database only (`<app_db>.*`), granting the privileges the app team needs to **own its schema**: data-manipulation **and** the schema-management (DDL: CREATE/ALTER/DROP/INDEX) its Alembic migrations require. The grant remains confined to the application database — never `*.*`, never global. **The grant is deliberately broader than the running service uses** — `corp-app` itself performs only `SELECT` and `INSERT` (see Section 6.3) — and this gap must be made visible, not silent (see the design note below).
   - `login_unix_socket: /var/lib/mysql/mysql.sock`.
4. Remove the broad `mysql` firewall service (image-baked, open to all) using `ansible.posix.firewalld` with `service: mysql`, `state: disabled`, `permanent: true`, `immediate: true`.
5. Add a rich rule allowing MySQL only from `app_host_address` using `ansible.posix.firewalld` with a rich rule in the form `rule family='ipv4' source address='{{ app_host_address }}/32' service name='mysql' accept`, `state: enabled`, `permanent: true`, `immediate: true`.

**Must not do:**
- Create any table, index, or other schema object, or seed any data (G17: schema/data is app-owned, applied by `app_release` via Alembic — Sections 6.5, 8.4). The former `service_records` creation and `ansible-bootstrap` seed tasks must **not** appear in this role.
- Set a MariaDB root password. Root access is always via the unix socket.
- Use `become: false` — the play sets `become: true`; DB socket access and firewall management require root.
- Install any packages (G9).
- Grant the application account any privilege outside its own database, or any global (`*.*`) privilege.

**Privilege-scope design note (documented decision — MUST be flagged loudly and in-line).** This
is an **example, not a template**: readers copy what they see, so the simplification must teach, not
mislead. The reference uses a **single** app account that holds DDL on *its own database* so the app
team can own its schema end to end — this keeps the example focused on the bootc ↔ Ansible boundary
rather than on DB credential plumbing. But a single account means the long-running,
network-exposed service process holds schema-destroying privileges it never uses, which is a real
anti-pattern. An example that modelled that **silently** would normalise it. Therefore:

- The `mysql_instance` task **comment itself** (not only distant docs) must state that the DDL grant
  exists purely so the app team can own its schema in this simplified example, and that a real
  deployment separates a release-time DDL/"migration" credential from a least-privilege runtime
  credential.
- The grant comment must name the honest gap: the running service needs only `SELECT, INSERT`
  (Section 6.3), so the combined account is deliberately over-granted relative to runtime need. That
  named gap *is* the least-privilege lesson — taught by signposting, without paying the complexity
  cost of a second account.
- The production posture (separate DDL "migration" account and DML "runtime" account, each with its
  own secret; migration credential ideally ephemeral/release-scoped and never persisted on the host)
  is documented as a named next-step in the README. It is intentionally **not** implemented here —
  the complexity is off-thesis for this example and would compete with the core lesson — but it is
  never left unstated.

**`defaults/main.yml` contract:** Must declare the role's overridable operational knobs with
sensible defaults: the MariaDB unix socket path (default `/var/lib/mysql/mysql.sock`), the
`wait_for` socket timeout (a conservative default allowing first-boot initialization), the
firewall service name narrowed at runtime (default `mysql`), and the privilege grant string
applied to the application account (scoped to the application database, including the DDL the app
team's migrations require). Must **not** declare `app_db_password` (G4), nor the shared/instance
values `app_db_name`, `app_db_user`, or `app_host_address` — these are resolved from `group_vars`
(`all/` and `database/`) and must not be shadowed by a role default. Must **not** declare a table
name or seed value — this role no longer owns schema or data.

**Applicable constraints:** G1 (firewalld module uses the running daemon; this is a runtime task, not a build step — `ansible.posix.firewalld` with `immediate: true` is correct here), G4, G9, G17.

---

### 8.3 Role: `app_deploy` (app-team play 1 — deploy, not go-live)

**Purpose:** Install the application payload onto the runtime baseline and lay down its
instance configuration and systemd unit — **without** starting the service, running migrations,
or opening the firewall. This is "the bits are in place" state (G14, G17, G19).

**Must implement (tasks, in this order):**
1. Ensure `app_root` (default `/opt/corp-app`) exists, owned `root:root` with a sane mode.
2. Deliver the payload from `app_src` (the `app/corp-app/` tree or a built artifact) into
   `app_root` — source code and the Alembic migration tree. Use a mechanism that converges
   idempotently (e.g., `ansible.builtin.copy`/`synchronize`/`unarchive`); re-running with
   unchanged source produces no change.
3. Create the virtualenv at `app_venv` (default `{{ app_root }}/venv`) **with access to system
   site-packages** so the baked corporate stack (SQLAlchemy/Alembic/Pydantic + drivers) is
   visible without re-installation (G14, G15).
4. Install the app's **pinned extras only** from `app/corp-app/requirements.txt` into `app_venv`
   using `ansible.builtin.pip` (G9 exception; G16 pinned). Must not install, upgrade, or re-pin
   the corporate stack (G15); must not use floating/`--upgrade` specifiers (G16); must not touch
   system site-packages.
5. Ensure `/etc/corp-app/` exists with mode `0750`, owner `root:root`.
6. Render the config from template `config.ini.j2` to `/etc/corp-app/config.ini`, mode `0640`,
   owner `root:root`, notifying the restart handler on change (G12).
7. Render the systemd unit to `/etc/systemd/system/corp-app.service` with `daemon_reload`
   (notify), `ExecStart` invoking the venv entrypoint (`app_entrypoint`). The unit is
   instance-owned here, not baked (G7).

**Must implement (handler):** `Restart corp-app` — `ansible.builtin.systemd`, `state: restarted`, `daemon_reload: true`. (Only restarts an already-started unit; `app_deploy` itself does not start the service.)

**Must implement (template `config.ini.j2`):** A `configparser`-readable INI with exactly:
- `[database]`: `host`, `port`, `name`, `user`, `password` (key names match what the app reads, G12).
- `[server]`: `port` only. No `bind_address` key (the bind address is fixed in the executable).

**Must implement (template `corp-app.service.j2`):** `[Unit]` `Description`, `After=network-online.target`, `Wants=network-online.target` (no `mariadb` dependency — DB is remote); `[Service]` `Type=simple`, `ExecStart` = `{{ app_venv }}/bin/...` entrypoint, `Restart=on-failure`, journal output, and the small overridable sandboxing set (`NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`); `[Install]` `WantedBy=multi-user.target`. `ProtectSystem=strict` is compatible with the payload under `/opt` (→ `/var/opt`, writable) and read-only `/etc` config.

**Must not do:** Enable or start the service (that is `app_release`, Section 8.4). Run migrations. Open any firewall port (G19). Install OS packages (G9). Install/upgrade/re-pin the corporate stack (G15). Create or seed database schema.

**`defaults/main.yml` contract:** Overridable knobs with sensible defaults — `app_root`,
`app_venv`, `app_src`, `app_entrypoint`, config dir mode (`0750`), config file mode (`0640`),
unit name (`corp-app`). Must **not** declare `app_db_password` (G4) or the shared/instance values
`app_db_name`, `app_db_user`, `db_host`, `app_port` (resolved from `group_vars`). The config path
is fixed by G12 and must not be an overridable default.

**Applicable constraints:** G4, G7, G9, G12, G14, G15, G16, G19.

---

### 8.4 Role: `app_release` (app-team play 2 — migrate, start, readiness gate)

**Purpose:** Bring the deployed payload to life: apply the app-owned schema via Alembic, start
the service, and confirm it came up — with a **single loopback readiness gate** only. No
functional exercises, no firewall exposure (G17, G19).

**Must implement (tasks, in this order):**
1. **Run migrations.** Invoke Alembic from `app_venv` (e.g., `ansible.builtin.command` running
   `{{ app_venv }}/bin/alembic upgrade head` with the migration tree's working directory). This
   creates `service_records` and the idempotent `ansible-bootstrap` seed (Section 6.5). Alembic's
   version tracking makes re-runs no-ops; the task's `changed_when` should reflect whether a
   migration was actually applied. The app DB account (DDL-on-own-database, Section 8.2) is the
   credential.
2. **Enable and start** `corp-app` using `ansible.builtin.systemd` with `state: started`,
   `enabled: true`, `daemon_reload: true` (G7: Ansible enables/starts; no image ever did).
3. **Readiness gate (loopback only).** Poll `http://127.0.0.1:{{ app_port }}/health` with
   `ansible.builtin.uri`, `status_code: 200`, retry loop, and assert `database == "connected"`.
   This runs *on the app host* over loopback, so it succeeds even though the external port is
   still closed (G19). It is the **only** verification permitted in release (G17) — a liveness
   confirmation, not a functional test.

**Must not do:** Perform `GET /` seed assertions, `POST /record` write probes, or negative-input
tests (those are proof-of-life, Section 8.5, G17). Open the external firewall port (G19). Install
OS packages (G9). Create/seed schema directly in SQL (schema is Alembic's job, step 1).

**`defaults/main.yml` contract:** Overridable knobs — `app_venv`, Alembic command/working
directory, unit name (`corp-app`), readiness retry count and delay. Must **not** declare
`app_db_password` (G4) or shared/instance `group_vars` values. Must **not** declare the seed
message as a free knob: `ansible-bootstrap` is a fixed interface value shared with the migration
(Section 6.5) and the proof-of-life assertion (Section 8.5).

**Applicable constraints:** G4, G7, G9, G16, G17, G19.

---

### 8.5 Role: `app_proof_of_life` (optional, user-run verification)

**Purpose:** Demonstrate the system is genuinely working **over the network** after a user has
deliberately exposed it. This role carries all functional verification removed from deployment
(G17). It is invoked only by `04_proof_of_life.yml`, never by `site.yml`.

**Must implement (tasks):** Run from the control node against the application host over the
network (`delegate_to: localhost`; URL built from the app host's IP/`ansible_host` and `app_port`):
1. **Health over the network.** `GET /health`, assert 200 and `database == "connected"`. If this
   fails while the loopback readiness gate (Section 8.4) passed, the lesson is explicit: the
   service works but the firewall was not opened (G19) — the failure message should say so.
2. **Seed-visibility assertion.** `GET /`, assert the `ansible-bootstrap` seed `message` appears
   in the structured `records` array (not a brittle raw-string match). Proves the app-owned
   Alembic migration reached the data layer and is visible over the network.
3. **Write verification.** `POST /record` with a recognizable message, assert 201 and the echoed
   record. Documented non-idempotent probe: each run adds one harmless row; must not be deleted or
   suppressed.
4. **Negative-path check.** `POST /record` with a malformed/empty body, assert 400 and no write.

**Must not do:** Run over loopback (it must exercise the real network path, G19). Open the
firewall itself (that is the deliberate, separately-labelled step in `04_proof_of_life.yml`, so
the before/after teaching contrast is visible). Delete probe rows or mutate the seed. Install OS
packages (G9).

**`defaults/main.yml` contract:** Overridable knobs — the verification message token, retry/delay
for the network checks. Must **not** declare secrets or shadow `group_vars` values. The seed
message asserted is the fixed interface value `ansible-bootstrap` (Sections 6.5, 8.4).

**Applicable constraints:** G9, G16, G17, G19.

---

## 9. Interface Agreement Summary

The following cross-component agreements must hold. A violation in any one file breaks the system silently or at runtime.

| Agreement | Files that must agree | Value |
|-----------|-----------------------|-------|
| Management account UID/GID | `sysusers.d/ansible.conf`, base Containerfile `chown`, numeric `owner:`/`group:` directives | UID 5000, GID 5000 |
| Management account name | `sysusers.d/ansible.conf` user entry, `authorized_key` module `user:`, `ansible_user`/`remote_user` | `ansible` |
| PSK file path | `scripts/generate-demo-keys.sh` output path, base Containerfile `COPY rootfs/` target, `authorized_key` module target | `/var/home/ansible/.ssh/authorized_keys` |
| Tuned profile name | Database Containerfile `echo corp-database > /etc/tuned/active_profile`, `rootfs/usr/lib/tuned/corp-database/` directory name | `corp-database` |
| Application config path | app payload hardcoded config path, `app_deploy` `config.ini.j2` `dest:`, `corp-app.service.j2` process | `/etc/corp-app/config.ini` |
| Config INI keys | app payload config reads, `config.ini.j2` template writes | `[database]`: `host`, `port`, `name`, `user`, `password`; `[server]`: `port` |
| Schema table + columns | app payload SQL (read/write), Alembic migration (`app/corp-app/migrations/`), `app_proof_of_life` seed assertion | table `service_records`; columns `id`, `message`, `created_at` |
| Seed record message | Alembic migration seed (Section 6.5), `app_proof_of_life` `GET /` assertion (Section 8.5) | `ansible-bootstrap` |
| App payload deployment root | `app_deploy` `app_root`, `app_venv`, `corp-app.service.j2` `ExecStart` | `/opt/corp-app`, venv at `/opt/corp-app/venv` |
| App unit path | `app_deploy` `corp-app.service.j2` `dest:`, `app_release`/handler `systemd name:` | `/etc/systemd/system/corp-app.service` (instance-owned, not baked) |
| Corporate Python stack | baked in `images/application` (Section 6.2), inherited by `app_venv` (`--system-site-packages`), imported by app payload | single source of truth; never re-pinned downstream (G15) |
| App host IP | `hosts.yml` `app01.ansible_host`, `group_vars/database/vars.yml` `app_host_address`, `app_proof_of_life` network target | Must be identical |
| DB host IP | `hosts.yml` `db01.ansible_host`, `group_vars/application/vars.yml` `db_host` | Must be identical |
| App DB password variable name | `all/vault.yml`, `mysql_instance` role `password:`, `config.ini.j2` template, Alembic migration credential | `app_db_password` |
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
| Workload | `01_database.yml`, `02_app_deploy.yml`, `03_app_release.yml` | Machine credential — live private key |
| Proof-of-life (optional) | `04_proof_of_life.yml` | Machine credential — live key (connects to app host; functional checks delegate to the control node) |

**Why the split is necessary:** AAP allows exactly one machine credential per job template. `00_bootstrap.yml` needs the bootstrap key for Plays 1–2 and the live key for Play 3. Locally, the playbook handles this via per-play `ansible_ssh_private_key_file` overrides. Under AAP, Play 3 (live key verification) is moved to the start of the workload job — or the bootstrap job is run with the bootstrap credential and the workload job with the live credential (which implicitly verifies connectivity).

**Proof-of-life is never part of the deployment job (G17).** It is a separate, optional job template (or a manual run) that a user triggers to demonstrate the system, including the deliberate firewall-exposure teaching step (G19). It must not be chained into the workload job.

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

### Task 4 — Application Runtime Image (corporate Python baseline)

**Branch slug:** `task/app-image`

**Files created or modified:**
- `images/application/Containerfile` (create — runtime baseline only)
- The pinned build-time requirement set for any corporate-stack component not available as an RPM (create; committed and referenced by the Containerfile).

**Inputs required:**
- Task 2 complete; `corp-base:latest` available locally.

**Applicable constraints:** G7, G9, G14, G15, G16, G18, G19.

**Acceptance criteria:**
- `podman build -t corp-app-img:latest images/application/` completes without error.
- `bootc container lint corp-app-img:latest` exits 0.
- The image directory contains **no** `rootfs/` tree and **no** application code or systemd unit (G7, G14).
- Inside the image: `python3 -c "import pymysql, psycopg2, sqlalchemy, alembic, pydantic"` exits 0 (corporate stack baked, G15).
- Inside the image: `command -v pip3 && command -v virtualenv && command -v git && command -v jq || true` — core runtime and helper tooling are present per Section 6.2 (adjust to the enumerated set).
- Inside the image: `firewall-offline-cmd --list-services` is unchanged from the base image (no app port/service; G19).
- Inside the image: `systemctl list-unit-files | grep -c corp-app` is `0` (no baked unit; G7).
- Any corporate-stack component installed via build-time pip uses an exact pinned version (G16); the pinned set is committed and visible.

**Dependencies:** Task 2.

---

### Task 4b — Application Payload (`app/corp-app/`)

**Branch slug:** `task/app-payload`

**Files created or modified:**
- `app/corp-app/src/...` (create — the application package: HTTP service using SQLAlchemy + Pydantic)
- `app/corp-app/requirements.txt` (create — app-specific pinned extras only; may be empty)
- `app/corp-app/migrations/...` (create — Alembic env + initial migration creating `service_records` and the idempotent `ansible-bootstrap` seed)

**Inputs required:**
- Task 4 complete (the corporate stack the payload imports is defined); can be authored in parallel against the Section 6.2 stack list.

**Applicable constraints:** G9, G12, G14, G15, G16.

**Acceptance criteria:**
- The app reads config only from `/etc/corp-app/config.ini` (G12); it does not create the file/dir.
- The app imports the corporate stack (SQLAlchemy, Pydantic, a DB driver) and does not vendor or re-pin it (G15); `requirements.txt` lists only pinned extras, or is empty with a comment (G16).
- `POST /record` validation is implemented with Pydantic; malformed input yields HTTP 400 with a JSON body and no write.
- The Alembic initial migration creates `service_records` (`id`, `message`, `created_at`) and seeds exactly one `ansible-bootstrap` row idempotently (a second `alembic upgrade head` adds no duplicate).
- The running service issues no DDL.
- The app binds `0.0.0.0` at `[server] port`; no `bind_address` is read from config.

**Dependencies:** Task 4.

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
- `ansible-galaxy collection install -r requirements.yml` completes without error; `requirements.yml` declares `ansible.mariadb (>=6.0.0)` and `ansible.posix (>=1.5.0)`.
- `site.yml` imports exactly the four deployment phases in order (`00_bootstrap`, `01_database`, `02_app_deploy`, `03_app_release`) and does **not** import `04_proof_of_life.yml` (G17).
- `ansible-inventory --host app01` includes the deployment variables `app_root`, `app_venv`, `app_src`, `app_entrypoint` (Section 7.6).
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

**Applicable constraints:** G4, G9, G17.

**Acceptance criteria (against a running database VM):**
- `mysql -u root -S /var/lib/mysql/mysql.sock -e "SHOW DATABASES;"` includes the application database.
- `mysql -u root -S /var/lib/mysql/mysql.sock -e "SELECT user,host FROM mysql.user WHERE user='corp_app';"` shows the account with `host` equal to the configured `app_host_address` (not `localhost`, not `%`).
- The role creates **no** schema and **no** seed data: immediately after `01_database.yml` (before `03_app_release` runs), `SHOW TABLES` in the application database is empty. The `service_records` table and the `ansible-bootstrap` seed appear only after the app's Alembic migration runs in release (G17).
- The `corp_app` account's grants are scoped to the application database (`<app_db>.*`, never `*.*`) and **do** include the DDL the app's migrations require (CREATE/ALTER/DROP), so the app team can own its schema (Section 8.2).
- `firewall-cmd --list-rich-rules` shows a rule for `app_host_address` on port 3306.
- `firewall-cmd --list-services` does not include `mysql` (broad service removed).
- Re-running `01_database.yml` produces no changes (idempotent).
- No task attempts to set a MariaDB root password; the role uses `ansible.mariadb.*` modules via the unix socket.

**Dependencies:** Task 5. (Task 6 and Task 3 are required for live execution, not for file authoring.)

---

### Task 8 — Application Deploy Role and Playbook (app-team play 1)

**Branch slug:** `task/app-deploy-role`

**Files created or modified:**
- `ansible/roles/app_deploy/defaults/main.yml` (create)
- `ansible/roles/app_deploy/tasks/main.yml` (create)
- `ansible/roles/app_deploy/handlers/main.yml` (create)
- `ansible/roles/app_deploy/templates/config.ini.j2` (create)
- `ansible/roles/app_deploy/templates/corp-app.service.j2` (create)
- `ansible/roles/app_deploy/meta/main.yml` (create)
- `ansible/playbooks/02_app_deploy.yml` (create)

**Inputs required:**
- Task 5 complete (Ansible project structure); Task 4b complete (payload to deploy).
- For execution: Task 6 (live key on app host); Task 4 (runtime image deployed).

**Applicable constraints:** G4, G7, G9, G12, G14, G15, G16, G19.

**Acceptance criteria (against a running application VM):**
- `/opt/corp-app` contains the payload; `/opt/corp-app/venv` exists and was created with system site-packages (the baked corporate stack imports inside the venv without re-installation; G14/G15).
- Any app extras from `requirements.txt` are installed into the venv with pinned versions; the corporate stack is **not** re-installed or re-pinned (G15/G16).
- `/etc/corp-app/config.ini` exists mode `0640` `root:root`; `/etc/systemd/system/corp-app.service` exists with `ExecStart` pointing into `{{ app_venv }}` (G12/G14).
- `systemctl is-active corp-app` outputs `inactive`/`failed`-free **not-started** state — the deploy role does **not** start the service (G17).
- No firewall port for the app is opened (G19).
- The `config.ini.j2` has `[database]` (`host`,`port`,`name`,`user`,`password`) and `[server]` (`port` only, no `bind_address`, G12).
- Re-running `02_app_deploy.yml` with unchanged source converges idempotently (no changes).

**Dependencies:** Task 5, Task 4b. (Task 6, 4 for live execution.)

---

### Task 8b — Application Release Role and Playbook (app-team play 2)

**Branch slug:** `task/app-release-role`

**Files created or modified:**
- `ansible/roles/app_release/defaults/main.yml` (create)
- `ansible/roles/app_release/tasks/main.yml` (create)
- `ansible/roles/app_release/meta/main.yml` (create)
- `ansible/playbooks/03_app_release.yml` (create)

**Inputs required:**
- Task 8 complete (payload deployed, venv + unit + config in place).
- For execution: Task 7 complete (DB + account with DDL on its database).

**Applicable constraints:** G4, G7, G9, G16, G17, G19.

**Acceptance criteria (against a running application VM):**
- After release, `service_records` exists in the application database and holds exactly one `ansible-bootstrap` seed row (created by the Alembic migration, not the DBA role).
- A second `03_app_release.yml` run applies no migration (Alembic at head) and adds no duplicate seed.
- `systemctl is-active corp-app` outputs `active`; `systemctl is-enabled corp-app` outputs `enabled` (G7).
- The release readiness gate polls `http://127.0.0.1:{{ app_port }}/health` **over loopback on the app host** and asserts `database == "connected"`. It performs no `GET /`/`POST /record` functional checks (G17).
- The external port is **not** opened by this role: from the control node, `curl http://<app_ip>:<app_port>/health` fails/times out after release (default-deny; G19) — this is the expected, documented state.

**Dependencies:** Task 8. (Task 7 for live execution.)

---

### Task 8c — Proof-of-Life Role and Playbook (optional exercise)

**Branch slug:** `task/app-proof-of-life`

**Files created or modified:**
- `ansible/roles/app_proof_of_life/defaults/main.yml` (create)
- `ansible/roles/app_proof_of_life/tasks/main.yml` (create)
- `ansible/roles/app_proof_of_life/meta/main.yml` (create)
- `ansible/playbooks/04_proof_of_life.yml` (create — includes the deliberate firewall-exposure step + the role)

**Inputs required:**
- Task 8b complete (service running and healthy on loopback).

**Applicable constraints:** G9, G16, G17, G19.

**Acceptance criteria (against a running application VM):**
- Before running `04_proof_of_life.yml`: `curl http://<app_ip>:<app_port>/health` from the control node fails (port closed) while the on-host loopback health check passes — the teaching contrast (G19).
- `04_proof_of_life.yml` contains a clearly-labelled firewall-exposure task that opens `app_port` (`ansible.posix.firewalld`, permanent + immediate).
- After running it: `curl http://<app_ip>:<app_port>/health` from the control node returns 200 with `database == "connected"`.
- The role (delegated to the control node, over the network) asserts: `/health` connected; the `ansible-bootstrap` seed is present in `GET /` `records`; `POST /record` returns 201 with the echoed record; a malformed `POST /record` returns 400.
- The playbook is **not** imported by `site.yml` (G17).
- Re-running adds one harmless write-probe row per run (documented non-idempotent probe); it must not error and must not delete prior probe rows.

**Dependencies:** Task 8b.

---

### Task 9 — README Documentation

**Branch slug:** `task/readme`

**Files created or modified:**
- `README.md` (create)

**Inputs required:**
- All previous tasks complete. Documentation covers the verified, complete system.

**Applicable constraints:** All — the README must accurately describe every constraint and design decision.

**Required sections (all must be present):**
1. **Architecture Overview** — bake-time vs. runtime boundary; the three-tier rule (platform → base image; workload standard → layer image; instance-specific → Ansible); the team-ownership model (platform/security → base; DBA → database image + DB foundation; app-platform/runtime → application runtime image + corporate Python standard; app-feature → payload + the two app plays); configuration domains where the base image is the only layer (time sync, SELinux posture, firewall policy).
2. **Image Layering & the application runtime baseline** — `FROM base` as the team boundary; why multi-stage builds are not needed; the `rootfs/` mirror-tree convention; and the application image as a reusable corporate Python *runtime baseline* that bakes no app code (G7/G14).
3. **Corporate Python standard** — the single baked stack (DB drivers `PyMySQL`+`psycopg2`; abstractions SQLAlchemy/Alembic/Pydantic); why it is centralized (few choice points, one source of truth, G15); the `/opt/corp-app` + venv (`--system-site-packages`) deployment standard; the runtime package policy (OS packages bake-time only; app Python deps via pip into the venv, pinned — G9/G16); and operator-tooling tiers incl. `tcpdump` as break-glass, not baked (G18).
4. **Deploy vs. release vs. proof-of-life** — the five-phase sequence and ownership; schema/seed as app-owned Alembic (not DBA); the deliberate firewall-exposure teaching moment (service works on loopback yet is unreachable until explicitly exposed; default-deny; loopback ≠ network; runtime firewall ownership — G17/G19); proof-of-life as an optional user exercise over the network.
5. **PSK Lifecycle** — install-only `/var` semantics; no-resurrection guarantee; the restrictive SSH options on the PSK line and their purpose; stale-host policy (reprovision, not rescue); expected `bootc container lint` `/var` warning documented as intentional.
6. **Key Generation → Image Build Ordering** — explicit step: run `scripts/generate-demo-keys.sh` before any image build; what happens if the order is reversed.
7. **Setup and Execution** — all commands in runnable order: generate keys → build images → provision VMs (out of scope) → install collections → create vault password file → encrypt vault files → run the deployment sequence → (optionally) run `04_proof_of_life.yml` including the firewall-exposure step. Working directory noted for each command.
8. **Practitioner Notes** — all items listed in `docs/plan.md` "Documentation requirements (README)" must appear substantively, including the privilege-scope next-step (DDL/DML account split) and the tooling-tier policy.
9. **AAP Compatibility** — one-credential-per-job constraint; bootstrap vs. workload job split (workload = `01`+`02`+`03`); proof-of-life as a separate optional job; vault-to-credential/survey translation with variable names unchanged.

**Acceptance criteria:**
- All nine sections are present.
- All practitioner notes items appear substantively (not necessarily verbatim).
- All shell commands are in code blocks with correct working directories noted.
- The `bootc container lint` `/var` warning is explicitly mentioned and explained.
- The firewall-exposure teaching moment (G19) and the deploy/proof-of-life separation (G17) are explicitly explained.
- The AAP section reflects the four-phase workload split and the separate proof-of-life job.

**Dependencies:** Tasks 1–8c.

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
| `/opt/corp-app/` (payload root) | `0755` | `root:root` | `ansible.builtin.file`/deploy in `app_deploy` role |
| `/opt/corp-app/venv/` | `0755` | `root:root` | created by `app_deploy` (`--system-site-packages`) |
| `/etc/corp-app/` | `0750` | `root:root` | `ansible.builtin.file` in `app_deploy` role |
| `/etc/corp-app/config.ini` | `0640` | `root:root` | `ansible.builtin.template` in `app_deploy` role |
| `/etc/systemd/system/corp-app.service` | `0644` | `root:root` | `ansible.builtin.template` in `app_deploy` role (instance-owned, not baked) |
| `scripts/generate-demo-keys.sh` | `0755` | committer | Set at commit time |
