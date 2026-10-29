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
2. **No runtime package management.** Ansible never installs or updates packages. Every runtime dependency — including Python DB client libraries — is baked into the image.
3. **bootc filesystem discipline.** `/usr` is image-owned and immutable; `/etc` is machine-local config subject to the 3-way merge (local changes win); `/var` is machine-local data and first-contact state, applied at initial install only and never merged or re-applied by updates.
4. **Stable management identity.** The Ansible account has a fixed, pinned UID/GID that does not drift across image rebuilds.
5. **First-contact provisioning is one-shot.** The pre-shared key exists only until first Ansible contact (see PSK lifecycle).
6. **Idempotency everywhere.** Every Ansible run is safe to repeat; re-running onboarding against an already-onboarded host is a no-op.
7. **One rule across all configuration domains.** Platform standard → base image; workload standard → layer image, shipped as content; instance-specific → Ansible. Domains where the workload does not change the answer (time sync, SELinux posture, firewall policy) are base-only — no workload override.

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

### Application image (workload)

Requirements:
- Derived from the base image.
- Installs the app runtime (python3) and the Python DB client library (the app probes MariaDB at runtime — a runtime dependency that must be baked).
- Bakes the app executable and its systemd unit into `/usr` (image-owned). The unit lives in the system unit directory under `/usr/lib` so it updates cleanly across image upgrades and admins override behaviour via `systemctl edit` drop-ins, never by editing the image file. The unit also carries a small set of systemd sandboxing directives (no new privileges, read-only protection of system and home paths, private tmp) — kept deliberately small to stay debuggable, and overridable via the same drop-in mechanism.
- Opens **no** firewall port: the app's health endpoint binds loopback only, so nothing external requires a port (default deny).
- **The app unit is baked but NOT enabled in the image.** The service cannot run before its runtime config exists; Ansible enables and starts it at first contact, after writing the config. Enabling in the image would crash-loop a fresh install and break first-contact ordering.
- The app's runtime configuration (database endpoint + credentials) is Ansible-managed. The agreed config path is `/etc/corp-app/config.ini`; the baked service unit and the Ansible role template must reference the same path.
- Passes `bootc container lint`.

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

### Database role
- Must reach a fresh, unconfigured MariaDB as root via the local unix socket with elevation — no pre-set root password required. All DB work on a fresh instance goes through that access path.
- Waits for the database service to be active before doing DB work.
- Creates the application database.
- Creates a dedicated application account that is **reachable from the application host** (not merely the local host), with privileges scoped to that database.
- **Narrows the database port at runtime:** replaces the broad open service from the image with a firewall rule restricting the database port to the application host's address. The application host's address therefore belongs in a variable scope visible to this role (consistent with the shared-secret requirement).
- Does **not** set a database root password; the unix socket remains the maintenance path. (Consequently no DB root password exists in the secrets store.)

### Application role
- Writes the app configuration (database endpoint + application account credentials) with restrictive permissions.
- Enables and starts the app service (first contact).
- Verifies the app's health endpoint **on the host's loopback** (the endpoint is not exposed externally), which must report the database connection.

### Shared-secret requirement
- The application account's password must resolve to the **same value** in the role that creates the account and the role that configures the client. It therefore lives in `group_vars/all.vault.yml` (visible to every role regardless of group), never in a single-group vault file.

## Secrets management

- Sensitive values are vault-encrypted (ansible-vault) files alongside their plaintext counterparts for standalone execution; the vault password file is gitignored.
- Under AAP, vault files are replaced by custom credentials / survey variables; **variable names remain identical** so roles are unchanged.
- Private key material and the vault password file are gitignored; only the key-generation script is committed.

## Control-node configuration (requirements)

- Runnable from a plain local checkout: inventory, role paths, vault password file, and collection requirements are all in the repo.
- Required collections: `community.mysql` (database modules) and `ansible.posix` (authorized-key and firewalld modules).
- The **live key is the default** private key for all plays; the onboarding phase overrides it explicitly for the bootstrap phase.
- SSH pipelining is enabled (required for correct sudo behaviour with NOPASSWD).
- Host key checking is relaxed for demo convenience and must be documented as such (production: accept-new or pre-populated known_hosts).
- Configuration paths are relative to the project directory — the README must state that playbooks are run from the `ansible/` directory.

## AAP compatibility (requirement — compatibility, not a component)

- The playbooks must run **unchanged** under AAP.
- **Constraint: an AAP job carries exactly one machine credential.** Therefore:
  - The onboarding (PSK rotation) phase must be runnable as its own job template with the **bootstrap** credential.
  - The verification + workload phases must be runnable as a job template with the **live** credential.
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
│   └── application/
│       ├── Containerfile                # FROM base -> sample app baseline (unit baked, not enabled)
│       └── rootfs/
│           └── usr/
│               ├── lib/
│               │   └── systemd/
│               │       └── system/
│               │           └── corp-app.service  # Image-managed systemd unit
│               └── local/
│                   └── bin/
│                       └── corp-app.py # Minimal service demonstrating DB wire-up
└── ansible/                             # Pure, AAP-compatible Ansible project
    ├── .gitignore                       # Excludes private keys and vault password file
    ├── ansible.cfg                      # Inventory, role paths, live key default, vault file, pipelining
    ├── requirements.yml                 # community.mysql, ansible.posix
    ├── inventory/
    │   ├── hosts.yml                    # Defines db01 and app01
    │   └── group_vars/
    │       ├── all.yml                  # Key paths, remote_user, common non-sensitive settings
    │       ├── all.vault.yml            # Vault-encrypted shared credentials (app account password)
    │       ├── database.yml             # Instance DB name, non-sensitive settings
    │       ├── database.vault.yml       # Vault-encrypted DB-specific credentials (if any)
    │       ├── application.yml          # App port, DB host pointer
    │       └── application.vault.yml    # Vault-encrypted app-specific credentials (if any)
    ├── keys/                            # Generated key material — all gitignored
    │   └── .gitignore                   # Excludes id_* private keys and vault password file
    ├── playbooks/
    │   ├── 00_bootstrap.yml             # Bootstrap key probe & rotation (single discrete unit)
    │   ├── 01_database.yml              # DB instance runtime provisioning
    │   └── 02_application.yml           # App instance runtime configuration
    ├── roles/
    │   ├── bootstrap_key_rotation/
    │   ├── mysql_instance/
    │   └── app_instance/                # incl. templates/ for the runtime app config
    └── site.yml                         # Master entrypoint: 00, 01, 02 in order
```

## Acceptance criteria (drive the implementation)

1. All three images build cleanly; `bootc container lint` passes, with the expected warning about first-contact `/var` content documented as intentional.
2. A freshly installed, never-onboarded system is reachable by Ansible via the PSK; the PSK is present in the standard authorized-keys location with usable ownership and permissions.
3. Onboarding installs the live key and removes the PSK; re-running onboarding is a no-op.
4. On a fresh instance, the database role creates the application database and account and narrows the database port to the application host; the application authenticates to the database **over the network**, and its health endpoint (loopback-only) reports the database connection.
5. The full flow runs as a single `ansible-playbook` invocation standalone, and as separable AAP jobs (bootstrap credential, then live credential) with no playbook changes.
6. A `bootc upgrade` leaves onboarding state intact: live key present, PSK absent, database and application runtime config preserved, services enabled.
7. On the database host the active tuned profile is the corporate database profile, and the MariaDB service runs with the corporate unit drop-in limits in effect.

## Documentation requirements (README)

- The bake-time vs runtime boundary, and why it is drawn where it is.
- The `rootfs/` mirror-tree convention (build context = documentation of what lands where).
- **Key generation → image build ordering**: run `scripts/generate-demo-keys.sh` before any image build; the script writes the bootstrap pubkey into the base image build context, and running it after the build produces a key mismatch.
- Collection installation, vault setup, and end-to-end execution (run from the `ansible/` directory).
- Practitioner notes:
  - Team split: the layered `FROM base` structure *is* the team boundary; multi-stage build stages are not needed.
  - Shared PSK is intentional design: one provisioning key per image build covers first contact for the whole fleet; its blast radius is unprovisioned hosts only; per-build rotation means rebuilding the image.
  - PSK lifecycle and the no-resurrection guarantee (install-only `/var` semantics; only full reprovision restores first contact).
  - Stale-host policy: an unprovisioned host that outlives a PSK rotation is reaped and recreated from the current image — never rescued.
  - No interactive login path is intentional design: break-glass is the live key or full re-provisioning; a host whose live key is lost after rotation must be re-provisioned.
  - DB firewall: the image ships the database port open to anywhere (usable in any test environment); the database role narrows it to the application host at runtime — the bake/runtime split made concrete.
  - App unit: baked-not-enabled decision and why; the `systemctl edit` override mechanism; the service runs as root to read a restrictive-mode config (acceptable for the demo).
  - DB host discovery: the demo uses a static variable; real-world options (fact caching, discovery step) noted.
  - Inventory: static IPs assumed.
  - `bootc upgrade` safety: what survives where, and why (sysusers drift-free, tmpfiles re-create dirs but not content, `/var` never merged, `/usr` updates cleanly).
  - SELinux posture: app runs unconfined; baked content uses standard contexts; no custom labelling.
  - Sudo scope: NOPASSWD:ALL is a demo convenience.
  - Configuration domains: the three-tier rule (platform → base, workload → layer image, instance → Ansible); time sync, SELinux posture, and firewall policy are platform-only.
  - SSH management surface: key-only, restricted to the Ansible account, restrictive options on the PSK line, and the image explicitly opens its own ssh service (the default zone drops it).
  - Service tuning: the tuned profile and the `mariadb.service` drop-in are image content that follows updates; why `limits.d` would not work for a systemd service; the app unit's small hardening directive set is overridable via drop-in.
  - Security non-additions as decisions: no runtime update daemon (the patching unit is image rebuild + `bootc upgrade`), no custom SELinux policy, no sysctl pile-up; image signing and DB TLS are named as next steps, out of scope.
- **AAP templating call-out** (required): the one-credential-per-job constraint, the bootstrap-job vs live-job split, and the vault → credential/survey translation (see AAP compatibility).

## Build & execution ordering

1. Run `scripts/generate-demo-keys.sh` to generate the demo keys (bootstrap + live) — must precede any image build, since the PSK must be in the image build context. The script crosses the images/ansible boundary and lives at the repo root for that reason.
2. Build the base image, then the database and application images.
3. Provision the VMs from the images (out of scope).
4. Install collections; run onboarding (PSK → live key); then the workload plays. Standalone, this is one `ansible-playbook` run; under AAP, two jobs (bootstrap credential, then live credential).
