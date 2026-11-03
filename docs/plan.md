# Bootc + Ansible Pattern — Plan & Requirements

## Purpose

A reference pattern showing the correct division of responsibility between **bootc** (image-mode Linux, CentOS Stream 10) and **Ansible** for running a corporate standard — specifically "how a MariaDB server runs" — with a minimal example application. Optimized for clarity as a teachable pattern: simple and explicit, not production automation.

## Scope

- Two target hosts: one **database** (MariaDB), one **application** (minimal service that consumes the database).
- The example application is intentionally minimal; the value is in the image/Ansible design, not the app.
- Local libvirt VMs. VM creation (bootc image → qcow2) is known and out of scope.
- Ansible runs from a simple local control node. It must also work under **AAP** — a compatibility requirement, not a component of this project.

## Design principles (invariants the implementation must honor)

1. **Bake-time vs runtime boundary.** Everything common to the corporate standard lives in the image; only instance- and runtime-specific state is managed by Ansible.
2. **No runtime OS package management (scoped Python exception).** Ansible never installs or updates **operating-system** packages — those are baked into the image at build time. The one deliberate exception is **application-level Python dependencies**: Ansible may install them at runtime, but only into the application's virtualenv under `/opt/corp-app/venv`, only with fully pinned versions, and never into system site-packages. The corporate shared Python stack (DB drivers and the data-abstraction layer) is baked into the application runtime image, not installed at runtime; runtime pip is limited to genuinely app-specific extras. OS-level `dnf`/`yum`/`rpm` in an Ansible task remains a design violation.
3. **bootc filesystem discipline.** `/usr` is image-owned and immutable; `/etc` is machine-local config subject to the 3-way merge (local changes win); `/var` is machine-local data and first-contact state, applied at initial install only and never merged or re-applied by updates.
4. **Stable management identity.** The Ansible account has a fixed, pinned UID/GID that does not drift across image rebuilds.
5. **First-contact provisioning is one-shot.** The pre-shared key exists only until first Ansible contact (see PSK lifecycle).
6. **Idempotency everywhere.** Every Ansible run is safe to repeat; re-running onboarding against an already-onboarded host is a no-op.
7. **One rule across all configuration domains.** Platform standard → base image; workload standard → layer image, shipped as content; instance-specific → Ansible. Domains where the workload does not change the answer (time sync, SELinux posture, firewall policy) are base-only — no workload override.

## Team ownership & corporate standards

The layering and the Ansible phase split express an explicit ownership model. Each boundary is a team boundary:

| Layer / phase | Owner | Responsibility |
|---------------|-------|----------------|
| Base image | Platform/Security team | OS hardening, management account, SSH baseline, firewall + tuned baseline |
| Database image + `01_database` | DBA / DB-platform team | MariaDB runtime standard, DB operator tooling, and the **DB foundation** (database, account + privileges, firewall narrowing) — **not** application schema |
| Application **runtime** image + corporate Python standard | App-platform / runtime team | A reusable corporate Python runtime baseline and the single, centrally-governed Python stack every app inherits |
| Application payload + `02_app_deploy` / `03_app_release` | App-feature team | The app's code, pinned extras, and Alembic migrations; the two app-owned plays (deploy, then release/go-live) |

**Single corporate Python standard (few choice points).** The application runtime image is the
one place the corporate Python stack is defined: the DB driver set (`python3-PyMySQL` for
MariaDB/MySQL and `python3-psycopg2` for PostgreSQL — two drivers deliberately, to signal a
centrally-standardized runtime) and the data-abstraction layer (SQLAlchemy, Alembic, Pydantic),
on top of a broad runtime base (pip, virtualenv, ca-certificates, tzdata, and helper/packaging
tooling: git, tar, gzip, unzip, setuptools, wheel). App teams inherit this entire stack via a
virtualenv built with system site-packages and choose only genuinely app-specific extras — one
place to make dependency decisions, one source of truth.

**Standard application deployment target.** Applications deploy to `/opt/corp-app` (venv at
`/opt/corp-app/venv`). On bootc, `/opt` resolves under `/var/opt` — persistent, install-only,
preserved across upgrade/rollback — which is the correct zone for Ansible-owned instance payload,
exactly like `/var/lib/mysql`.

**Deploy vs. release vs. proof-of-life.** "Real" deployment is kept separate from demonstration.
The deploy play lays down the payload/venv/config/unit; the release play migrates (app-owned
schema via Alembic) and starts the service with a single loopback readiness gate; all *functional*
verification lives in a separate, optional, user-run proof-of-life exercise.

**External exposure is a deliberate teaching moment.** Neither deploy nor release opens the
application's external firewall port. The service binds `0.0.0.0` and works on loopback
immediately, yet is unreachable from the network until a human runs the explicit firewall-exposure
step. This default-deny gap is intentional — it teaches default-deny posture, the difference
between loopback and network reachability, and runtime firewall ownership — and is documented as a
lesson, never as a defect.

**Operator tooling tiers.** Tools baked into an image must be frequently needed and host-coupled
(e.g., the DBA set: `mariadb` CLI, `mariadb-backup`, `lsof`, `strace`, `procps-ng`, `sysstat`,
`jq`). Episodic or high-privilege diagnostics (notably `tcpdump`) are **not** baked — they are
break-glass (a documented, transient, non-durable manual `dnf install` during an incident) or
one-shot container tooling.

## Image layering

Layering **is** the team boundary: the base image is platform/security-team owned; the database and application images are workload-team owned, each derived from the one below. No multi-stage Containerfile build is required — nothing is compiled.

Each image directory uses a `rootfs/` subtree mirroring the target filesystem layout; the build context is the documentation of what lands where on a deployed system.

**Containerfile build constraint:** the firewalld daemon is not running during an image build. All firewall configuration in Containerfile steps must use the offline mechanism — the standard runtime tool requires a running daemon and will fail at build time. This constraint applies to every image that manages firewall state.

### Base image (platform/security)

Requirements:
- Base: `quay.io/centos-bootc/centos-bootc:stream10`.
- Provides the admin toolset (sudo, openssh-server, python3, rsync, iproute, firewalld, plus light admin tools: tmux, vim-minimal, bind-utils).
- Provides the Ansible management account with a pinned UID/GID, a working home directory, and drift-free boot-time creation.
- Provides the **first-contact provisioning key (PSK)** in the account's standard SSH authorized-keys location, present and usable on a fresh install (correct ownership and permissions).
- Establishes a key-only SSH baseline: no password authentication, no root login, and SSH access restricted to the Ansible account. This is the "light security work" of the standard build, and it makes the key-only management model coherent — a fresh VM has no interactive login path.
- The baked PSK key line carries restrictive SSH options (no PTY, no X11 or agent forwarding, no user rc) that limit the blast radius of the shared provisioning key.
- Enables firewalld **and explicitly opens the ssh service** — the default zone drops everything, so the image must declare its own management surface.
- Enables `tuned` with a generic platform profile; a workload layer may replace the active profile with its own (see database image).
- The account has passwordless sudo (a demo convenience; production would scope it).
- Builds cleanly and passes `bootc container lint`. The expected lint warning about first-contact content under `/var` is **documented as intentional**, not a defect.

Design decisions (chosen mechanisms, with rationale):
- **The PSK is baked into the account's home under `/var` at build time.** `/var` ships with the image and is applied at initial install only, never merged or re-applied by `bootc upgrade` — exactly the "present at first contact, never resurrected" lifecycle required. The tmpfiles.d alternatives cannot express "seed once, never recreate" without reintroducing a resurrection path (an `f~` entry would re-create the key; a mask would survive rollback and defeat the strict first-contact-only reading).
- **The account is created via `sysusers.d`** (pinned UID/GID, shell) **and the home via `tmpfiles.d`** — both drift-free, both standard bootc boot-time mechanisms, with boot ordering such that name-based ownership in tmpfiles resolves (sysusers runs first).
- **The management account is pinned to UID 5000, GID 5000.** This value must be consistent across the sysusers.d definition and the Containerfile ownership operation on the baked PSK file; any divergence results in a PSK file the ansible account cannot read.
- **The PSK file must land in the image with its final ownership and permissions.** The user does not exist at build time, so ownership is numeric; and boot-time file-creation hooks do not re-enforce ownership or modes on files that already exist, so the image content itself must be correct.
- **No `AuthorizedKeysFile` drop-in, no custom SELinux labelling**: the standard `~/.ssh/authorized_keys` path and its default context are used as-is.
- **SSH hardening via sshd drop-in(s)**; the base `sshd_config` is never modified directly.

### Database image (workload)

Requirements:
- Derived from the base image.
- Installs MariaDB server **and** the Python DB client library (so Ansible DB automation and any local client work with zero runtime installs).
- Bakes the corporate MariaDB baseline: UTF-8 (utf8mb4) default character set, InnoDB/buffer sizing, slow-query logging, and **network-reachable listening** (not localhost-only).
- Opens the database port in the firewall at bake time (usable in any test environment). Narrowing it to the application host is deliberately left to runtime — it is instance-specific (see database role).
- Ships a database-oriented **tuned profile** as image content under `/usr/lib/tuned/`. The database Containerfile activates it at build time, writing the active-profile selection into `/etc/tuned/`; the base image enables tuned on the platform default and does not set a workload profile.
- Bakes a **`mariadb.service` unit drop-in** under `/usr/lib/systemd/system/` that sets corporate resource limits — file handles are the classic one. As image-owned content in `/usr/lib/`, the corporate standard is always enforced and updates unconditionally with the image. Rationale: PAM `limits.d` does not apply to systemd services; the unit drop-in is the right mechanism. Admins stack further tuning via `systemctl edit` into `/etc/` on top.
- The database must be running and self-initialized on first boot.
- Passes `bootc container lint`.

### Application runtime image (workload runtime baseline)

The application image is a **reusable corporate Python runtime baseline**, not an app image. It
bakes the runtime and the corporate Python stack; it carries no application code, no systemd unit,
and no app firewall port. The application itself is a payload deployed onto it by Ansible.

Requirements:
- Derived from the base image.
- Bakes the corporate Python runtime bundle in three clearly-commented tiers:
  - **Core runtime:** python3, pip, virtualenv, ca-certificates, tzdata.
  - **Helpers & packaging tooling:** git, tar, gzip, unzip, setuptools, wheel.
  - **Corporate DB drivers + data-abstraction layer (called out as corporate standard, not
    universal):** `python3-PyMySQL`, `python3-psycopg2`; SQLAlchemy, Alembic, Pydantic (RPM where
    packaged on CentOS Stream 10, otherwise pinned build-time pip into system site-packages — a
    bake action, not a runtime one).
- Bakes **no** application code, executable, or systemd unit (G7/G14). The unit is instance-owned
  and rendered by Ansible to `/etc/systemd/system/corp-app.service` at deploy time, carrying the
  small overridable sandboxing set (no new privileges, read-only system/home, private tmp).
- Opens **no** firewall port. Exposure is an instance decision made at runtime, and is deliberately
  deferred to a separate user step as a teaching moment (default deny).
- The app's runtime configuration (database endpoint + credentials) is Ansible-managed at the agreed
  path `/etc/corp-app/config.ini`; the Ansible-rendered unit and template must reference the same
  path.
- Passes `bootc container lint`.

### Application payload (app-feature-team owned)

Requirements:
- Lives outside `images/` and `ansible/` (e.g., under a top-level `app/corp-app/`): the app source,
  its pinned `requirements.txt` (extras only — never the corporate stack), and its Alembic
  migration tree.
- Imports only the baked corporate stack plus pinned extras; it must not vendor or re-pin the
  corporate stack.
- **Owns its schema.** The initial Alembic migration creates the `service_records` table and seeds
  the single `ansible-bootstrap` bootstrap row idempotently. The running service issues no DDL.
- Reads config only from `/etc/corp-app/config.ini`; binds `0.0.0.0` at the configured port (bind
  address fixed in the executable, not config-driven); validates `POST /record` input with Pydantic;
  accesses the database via SQLAlchemy.

## PSK lifecycle (invariant)

- The PSK is present and usable on a freshly installed, never-onboarded system, in the standard authorized-keys location.
- On first Ansible contact: the live management key is installed and the PSK is removed.
- After rotation, **nothing re-creates the PSK** — not updates, not rollbacks, not reboots, not boot-time file-creation hooks. Only a full reprovision (fresh deployment unpacking the image's `/var`) restores first-contact behaviour.
- Onboarding is idempotent: re-running it is a no-op; an already-onboarded host is never re-rotated.
- The PSK's presence/absence is the **sole discriminator** between new and onboarded hosts.

## Ansible onboarding (requirements)

- A **single discrete onboarding unit**: probe with the PSK → on probe success, install the live key and remove the PSK → then verify that all hosts are reachable via the live key before any workload automation proceeds.
- No reprobe after rotation; successful completion of the verification is the check.
- Rotation must be gated on the probe's **explicit success** — an unreachable host is not "probe failed, skip rotation"; the probe outcome is the discriminator and must drive the decision unambiguously.
- Rotation is a single discrete unit against the standard `~/.ssh/authorized_keys` path: the account owns its own file, so no elevation, no drop-ins, no auxiliary state.
- After onboarding, all workload automation runs under the **live key only**, unconditionally. The live key is the control node's default management key; the PSK is used only during onboarding.
- The onboarding phase must be separable by credential for AAP (see AAP compatibility).

## Workload automation (requirements)

The automation is a five-phase sequence: `00_bootstrap` (platform onboarding), `01_database` (DBA
DB foundation), `02_app_deploy` and `03_app_release` (the app team's two plays), and the optional,
user-run `04_proof_of_life`. Only the first four are deployment; `site.yml` imports exactly those
four and never proof-of-life.

### Database foundation role (DBA-owned, `01_database`)
- Must reach a fresh, unconfigured MariaDB as root via the local unix socket with elevation — no pre-set root password required. All DB work on a fresh instance goes through that access path.
- Waits for the database service to be active before doing DB work.
- Creates the application database.
- Creates a dedicated application account **reachable from the application host** (not merely the local host), with privileges scoped to that database — including the DDL (CREATE/ALTER/DROP) the app team's Alembic migrations require, so the app team can **own its schema**. The grant never extends beyond the application database and is never global.
- **Narrows the database port at runtime:** replaces the broad open service from the image with a firewall rule restricting the database port to the application host's address.
- Creates **no** application schema and **no** seed data — that is app-team territory, applied by Alembic in the release phase.
- Does **not** set a database root password; the unix socket remains the maintenance path.
- A stricter corporate posture (separate DDL "migration" account vs. DML "runtime" account, each with its own secret) is a named production next-step, intentionally out of scope for the reference pattern.

### Application deploy role (app-team play 1, `02_app_deploy`)
- Installs the payload to `/opt/corp-app` and creates the venv at `/opt/corp-app/venv` with system site-packages (inherits the baked corporate stack).
- Installs only the app's pinned extras into the venv; never re-installs or re-pins the corporate stack.
- Writes the app configuration (database endpoint + credentials) to `/etc/corp-app/config.ini` with restrictive permissions, and renders the instance-owned systemd unit to `/etc/systemd/system/corp-app.service`.
- Does **not** start the service, run migrations, or open any firewall port.

### Application release role (app-team play 2, `03_app_release`)
- Runs the app's Alembic migrations from the venv (creating the schema + idempotent seed).
- Enables and starts the app service.
- Verifies **only** a single loopback readiness gate — the app's health endpoint on the host's loopback must report the database connection. No functional read/write exercises here.
- Does **not** open the external firewall port (the deliberate teaching-moment gap).

### Proof-of-life exercise (optional, user-run, `04_proof_of_life`)
- A clearly-labelled firewall-exposure step opens the app port (the teaching moment: before it, external access fails by default-deny; after it, the service is reachable).
- Functional verification runs from the control node **over the network**: health is connected, the `ansible-bootstrap` seed is visible via the read endpoint, a write succeeds, and malformed input is rejected. These are the checks removed from deployment.

### Shared-secret requirement
- The application account's password must resolve to the **same value** in the role that creates the account and the roles that use it (config template + Alembic migration credential). It therefore lives in `group_vars/all/vault.yml` (directory form; visible to every role regardless of group), never in a single-group vault file.

## Secrets management

- Sensitive values are vault-encrypted (ansible-vault) files alongside their plaintext counterparts for standalone execution; the vault password file is gitignored.
- Under AAP, vault files are replaced by custom credentials / survey variables; **variable names remain identical** so roles are unchanged.
- Private key material and the vault password file are gitignored; only the key-generation script is committed.

## Control-node configuration (requirements)

- Runnable from a plain local checkout: inventory, role paths, vault password file, and collection requirements are all in the repo.
- Required collections: `ansible.mariadb` (database modules) and `ansible.posix` (authorized-key and firewalld modules). The app deploy/release/proof-of-life roles use only `ansible.builtin` (`pip`, `command`, `template`, `systemd`, `uri`) and add no collection.
- The **live key is the default** private key for all plays; the onboarding phase overrides it explicitly for the bootstrap phase.
- SSH pipelining is enabled (required for correct sudo behaviour with NOPASSWD).
- Host key checking is relaxed for demo convenience and must be documented as such (production: accept-new or pre-populated known_hosts).
- Configuration paths are relative to the project directory — the README must state that playbooks are run from the `ansible/` directory.

## AAP compatibility (requirement — compatibility, not a component)

- The playbooks must run **unchanged** under AAP.
- **Constraint: an AAP job carries exactly one machine credential.** Therefore:
  - The onboarding (PSK rotation) phase must be runnable as its own job template with the **bootstrap** credential.
  - The workload phases (`01_database` + `02_app_deploy` + `03_app_release`) must be runnable as a job template with the **live** credential.
  - The optional `04_proof_of_life` is a separate job (or manual run), never chained into the workload job.
- **Documentation call-out (required in README/artifacts):** when templating this project for AAP, the documentation must explicitly explain the credential/job split above and the vault → credential/survey translation.

## Repository layout

```text
ansible-bootc/
├── README.md                            # Architecture guide, design rationale, usage walkthrough
├── docs/
│   ├── plan.md                          # Requirements and design decisions (this document)
│   └── design-spec.md                   # Technical design specification (architect output)
├── scripts/
│   └── generate-demo-keys.sh            # Project bootstrap: generates SSH key pairs and writes
│                                        # the bootstrap pubkey into the base image build context.
│                                        # Crosses the images/ansible boundary — lives at root,
│                                        # not inside either subtree.
├── images/                              # Layered bootc container definitions
│   ├── base/
│   │   ├── Containerfile                # CentOS Stream 10 + admin tools + ansible account + PSK
│   │   └── rootfs/                      # Mirrors target filesystem layout
│   │       ├── etc/
│   │       │   ├── sudoers.d/
│   │       │   │   └── 99-ansible       # Passwordless sudo for the ansible account
│   │       │   └── ssh/
│   │       │       └── sshd_config.d/
│   │       │           └── 50-corporate-hardening.conf  # Key-only SSH, no root login, account-restricted
│   │       ├── var/
│   │       │   └── home/
│   │       │       └── ansible/
│   │       │           └── .ssh/
│   │       │               └── authorized_keys  # PSK — baked at build, first contact only
│   │       └── usr/
│   │           └── lib/
│   │               ├── sysusers.d/
│   │               │   └── ansible.conf # Pinned UID/GID account definition
│   │               └── tmpfiles.d/
│   │                   └── ansible-home.conf  # Home dir + .ssh dir (d entries)
│   ├── database/
│   │   ├── Containerfile                # FROM base -> MariaDB + DB client lib + corporate baseline + tuning
│   │   └── rootfs/
│   │       ├── etc/
│   │       │   └── my.cnf.d/
│   │       │       └── 00-corporate.cnf # Corporate baseline (UTF-8, sizing, slow log, network bind)
│   │       └── usr/
│   │           └── lib/
│   │               ├── systemd/
│   │               │   └── system/
│   │               │       └── mariadb.service.d/
│   │               │           └── 10-corporate.conf  # Corporate resource limits (image-owned unit drop-in)
│   │               └── tuned/
│   │                   └── corp-database/
│   │                       └── tuned.conf  # DB workload tuned profile (image-owned)
│   └── application/                     # Reusable corporate Python RUNTIME baseline
│       └── Containerfile                # FROM base -> python runtime + corporate stack; NO app code/unit
├── app/                                 # App-feature-team payload (deployed to /opt/corp-app)
│   └── corp-app/
│       ├── requirements.txt             # App-specific pinned EXTRAS only (never the corporate stack)
│       ├── src/                         # Application package (SQLAlchemy + Pydantic)
│       └── migrations/                  # Alembic tree (app-owned schema + seed)
└── ansible/                             # Pure, AAP-compatible Ansible project
    ├── .gitignore                       # Excludes private keys and vault password file
    ├── ansible.cfg                      # Inventory, role paths, live key default, vault file, pipelining
    ├── requirements.yml                 # ansible.mariadb, ansible.posix
    ├── inventory/
    │   ├── hosts.yml                    # Defines db01 and app01
    │   └── group_vars/                  # Directory form (mandatory): group name == directory name
    │       ├── all/
    │       │   ├── vars.yml             # Key paths, remote_user, shared DB name/user
    │       │   └── vault.yml            # Vault-encrypted shared credential (app account password)
    │       ├── database/
    │       │   ├── vars.yml             # app_host_address
    │       │   └── vault.yml            # Empty placeholder
    │       └── application/
    │           ├── vars.yml             # db_host, app_port, app_root/app_venv/app_src/app_entrypoint
    │           └── vault.yml            # Empty placeholder
    ├── keys/                            # Generated key material — all gitignored
    │   └── .gitignore                   # Excludes id_* private keys and vault password file
    ├── playbooks/
    │   ├── 00_bootstrap.yml             # Platform: bootstrap key probe & rotation
    │   ├── 01_database.yml              # DBA: DB foundation (no schema/seed)
    │   ├── 02_app_deploy.yml            # App team play 1: deploy payload/venv/config/unit
    │   ├── 03_app_release.yml           # App team play 2: migrate + start + readiness gate
    │   └── 04_proof_of_life.yml         # Optional user exercise: firewall-open + functional checks
    ├── roles/
    │   ├── bootstrap_key_rotation/
    │   ├── mariadb_instance/            # DB foundation (schema-free)
    │   ├── app_deploy/                  # incl. templates/ for config.ini + the systemd unit
    │   ├── app_release/                 # Alembic migrate, enable/start, loopback readiness gate
    │   └── app_proof_of_life/           # Network functional verification (exercise)
    └── site.yml                         # Deployment entrypoint: 00, 01, 02, 03 (NOT proof-of-life)
```

## Acceptance criteria (drive the implementation)

1. All three images build cleanly; `bootc container lint` passes, with the expected warning about first-contact `/var` content documented as intentional.
2. The application image is a runtime baseline: it bakes the corporate Python stack (DB drivers + SQLAlchemy/Alembic/Pydantic) and contains **no** application code, systemd unit, or app firewall port.
3. A freshly installed, never-onboarded system is reachable by Ansible via the PSK; the PSK is present in the standard authorized-keys location with usable ownership and permissions.
4. Onboarding installs the live key and removes the PSK; re-running onboarding is a no-op.
5. On a fresh instance, the DB foundation role creates the application database and account (scoped to its own database, including the DDL the app's migrations need) and narrows the database port to the application host; it creates **no** application schema or seed.
6. `02_app_deploy` installs the payload to `/opt/corp-app` with a venv (system site-packages) and pinned extras, writes the config and the instance-owned unit, and does not start the service. `03_app_release` runs the app's Alembic migration (creating the schema + idempotent `ansible-bootstrap` seed), starts the service, and passes a loopback readiness gate; the application authenticates to the database **over the network**.
7. Immediately after release, the app is reachable on the host's loopback but **not** from the network (default-deny). The optional `04_proof_of_life` opens the port (the teaching moment) and then verifies — over the network — health, seed visibility, a write, and rejection of malformed input.
8. The deployment flow runs as a single `ansible-playbook site.yml` standalone (four phases, proof-of-life excluded), and as separable AAP jobs (bootstrap credential, then live credential) with no playbook changes.
9. A `bootc upgrade` leaves state intact: live key present, PSK absent, DB data preserved, the `/opt/corp-app` payload and `/etc` runtime config preserved, services enabled.
10. On the database host the active tuned profile is the corporate database profile, and the MariaDB service runs with the corporate unit drop-in limits in effect.
11. No Ansible task installs an OS package; the only runtime package installs are app-specific Python extras, pinned, into the app venv.

## Documentation requirements (README)

- The bake-time vs runtime boundary, and why it is drawn where it is.
- The `rootfs/` mirror-tree convention (build context = documentation of what lands where).
- **Key generation → image build ordering**: run `scripts/generate-demo-keys.sh` before any image build; the script writes the bootstrap pubkey into the base image build context, and running it after the build produces a key mismatch.
- Collection installation, vault setup, and end-to-end execution (run from the `ansible/` directory).
- Team ownership model: the layered `FROM base` structure and the Ansible phase split *are* the team boundaries (platform → base; DBA → DB image + foundation; app-platform → runtime image + corporate Python standard; app-feature → payload + two plays); multi-stage build stages are not needed.
- Corporate Python standard: the single baked stack (two DB drivers + SQLAlchemy/Alembic/Pydantic) as one source of truth; the `/opt/corp-app` + venv (`--system-site-packages`) deployment standard; the runtime package policy (OS packages bake-time only; app Python deps via pip into the venv, pinned); why centralizing minimizes per-app choices.
- Deploy vs. release vs. proof-of-life: the five-phase sequence; schema/seed as app-owned Alembic (not DBA); proof-of-life as an optional, user-run network exercise separate from deployment.
- Firewall exposure as a teaching moment: the service works on loopback immediately after release yet is unreachable from the network until a deliberate, user-run firewall-open step — default-deny posture, loopback ≠ network, runtime firewall ownership.
- Operator tooling tiers: baked host-coupled DBA tools vs. break-glass (`tcpdump`, transient manual install) vs. one-shot container tooling.
- Practitioner notes:
  - Shared PSK is intentional design: one provisioning key per image build covers first contact for the whole fleet; its blast radius is unprovisioned hosts only; per-build rotation means rebuilding the image.
  - PSK lifecycle and the no-resurrection guarantee (install-only `/var` semantics; only full reprovision restores first contact).
  - Stale-host policy: an unprovisioned host that outlives a PSK rotation is reaped and recreated from the current image — never rescued.
  - No interactive login path is intentional design: break-glass is the live key or full re-provisioning; a host whose live key is lost after rotation must be re-provisioned.
  - DB firewall: the image ships the database port open to anywhere (usable in any test environment); the DB foundation role narrows it to the application host at runtime — the bake/runtime split made concrete.
  - App unit: instance-owned (rendered by Ansible to `/etc/systemd/system`, not baked) and why; the `systemctl edit` override mechanism; the service runs as root to read a restrictive-mode config (acceptable for the demo).
  - DB privilege scope: the app account owns DDL on its own database (so the app team owns its schema). Because this is an *example, not a template*, the resulting over-grant — the running service holds DDL it never uses (it only `SELECT`/`INSERT`s) — must be flagged loudly and in-line, read as the least-privilege lesson; the stricter DDL/DML account split (ideally with an ephemeral, release-scoped migration credential) is the documented production next-step, intentionally not implemented here.
  - DB host discovery: the demo uses a static variable; real-world options (fact caching, discovery step) noted.
  - Inventory: static IPs assumed.
  - `bootc upgrade` safety: what survives where, and why (sysusers drift-free, tmpfiles re-create dirs but not content, `/var` never merged, `/usr` updates cleanly, `/opt`→`/var/opt` payload preserved).
  - SELinux posture: app runs unconfined; baked content uses standard contexts; no custom labelling.
  - Sudo scope: NOPASSWD:ALL is a demo convenience.
  - Configuration domains: the three-tier rule (platform → base, workload → layer image, instance → Ansible); time sync, SELinux posture, and firewall policy are platform-only.
  - SSH management surface: key-only, restricted to the Ansible account, restrictive options on the PSK line, and the image explicitly opens its own ssh service (the default zone drops it).
  - Service tuning: the tuned profile and the `mariadb.service` drop-in are image content that follows updates; why `limits.d` would not work for a systemd service; the app unit's small hardening directive set is overridable via drop-in.
  - Security non-additions as decisions: no runtime update daemon (the patching unit is image rebuild + `bootc upgrade`), no custom SELinux policy, no sysctl pile-up; image signing and DB TLS are named as next steps, out of scope.
- **AAP templating call-out** (required): the one-credential-per-job constraint, the bootstrap-job vs workload-job split (workload = `01`+`02`+`03`), proof-of-life as a separate optional job, and the vault → credential/survey translation (see AAP compatibility).

## Build & execution ordering

1. Run `scripts/generate-demo-keys.sh` to generate the demo keys (bootstrap + live) — must precede any image build, since the PSK must be in the image build context. The script crosses the images/ansible boundary and lives at the repo root for that reason.
2. Build the base image, then the database and application (runtime baseline) images.
3. Provision the VMs from the images (out of scope).
4. Install collections; run onboarding (PSK → live key); then the deployment phases: DB foundation (`01`), app deploy (`02`), app release (`03`). Standalone, this is one `ansible-playbook site.yml` run; under AAP, two jobs (bootstrap credential, then live credential).
5. Optionally run `04_proof_of_life.yml` by hand: it performs the deliberate firewall-exposure step and then verifies the system over the network.
