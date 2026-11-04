# bootc + Ansible Reference Pattern — Requirements

This is the canonical, normative requirements document for the project. It states what the
system must achieve and how a reviewer can tell whether it does. Reader-facing documents
explain the implementation; where they disagree with this file, this file wins.

## Purpose and scope

A reference pattern showing the correct division of responsibility between **bootc**
(image-mode Linux, CentOS Stream 10) and **Ansible** for running a corporate standard —
specifically "how a MariaDB server runs" — with a minimal example application. It is
optimised for clarity as a teachable pattern: simple and explicit, not production
automation.

In scope:

- Two target hosts: one **database** host (MariaDB) and one **application** host (a minimal
  service that consumes the database).
- Layered bootc images that carry the platform and workload standards, and an Ansible
  project that applies instance-specific state.
- Execution from a plain local control node, with the same playbooks runnable unchanged
  under Ansible Automation Platform (AAP).
- End-to-end integration with the supplied application fixture.

Out of scope (non-goals):

- Production automation: high availability, backup operations, image signing, database TLS,
  and a hardened secret-rotation scheme are named next steps, not deliverables.
- Creating VMs from the built images. Image-to-qcow2 conversion and provisioning are known
  procedures.
- The internals of the supplied application fixture, described next.

### The application is a supplied fixture

`app/corp-app/` is a working fixture treated as though it came from a separate repository.
It exists so the image and Ansible boundaries can be exercised end to end against a real
payload with real migrations and a real HTTP surface. It is **not** the teaching outcome or
the review target:

- Its internal structure, framework choice, route implementation, and code style are not
  requirements of this project.
- Its table name, columns, seed message, and port are **fixture integration values**, not
  project-wide goals. They are listed in "Integration agreement with the fixture" only
  because two independent components must agree on them.
- Change the fixture only when it is broken as an integration partner — for example, when it
  violates a stated interface. Treat such a change as a fixture integration change, not as
  architecture work.
- Documentation, architecture, and agent-instruction work must not modify `app/`.

The value of this repository is the ownership boundary: what belongs in the image, what
belongs to platform/DBA runtime automation, and what belongs to the application team.

## Team ownership

Layering expresses the ownership model. Each boundary below is a team boundary:

| Layer / phase | Owner | Responsibility |
|---|---|---|
| Base image | Infrastructure / Operations | OS hardening, management account, SSH baseline, firewall and tuned baseline |
| Database image and DB foundation play | DBA | MariaDB runtime standard, DB operator tooling, database/account/privilege foundation, runtime firewall narrowing — not application schema |
| Application runtime image and corporate Python standard | Middleware | A reusable corporate Python runtime baseline and the single, centrally governed Python stack every application inherits |
| Application payload and the two application plays | Application Development | The fixture's code, pinned extras, and migrations; deploy, then release/go-live |

There is no multi-stage Containerfile build: nothing is compiled. A workload image derives
from the base image with `FROM`, and that line is the team boundary in code. The database
and application images are workload-team owned.

The application side has a second boundary inside the runtime layer: **Middleware** owns the
reusable runtime baseline (the corporate Python stack); **Application Development** owns the
payload deployed onto it. The image answers "what runtime does every app host get?"; the
payload answers "what is this app?".

## The image-mode boundary

The bake-time/runtime split follows bootc's filesystem semantics:

| Zone | Semantics | What belongs there |
|---|---|---|
| `/usr` | Image-owned; immutable at runtime; replaced atomically on upgrade | Account and tmpfiles declarations, unit files and drop-ins, executables, tuned profiles |
| `/etc` | Machine-local config; 3-way merge on upgrade; local changes win | SSH drop-ins, sudoers, service baseline config, application runtime config |
| `/var` | Machine-local data and first-contact state; applied at initial install only; never merged or re-applied on upgrade | Management home and first-contact key, database data, deployed payload under `/var/opt` |

Everything common to the corporate standard lives in the image; only instance- and
runtime-specific state is managed by Ansible. One rule applies across all configuration
domains:

- **Platform standard** → base image.
- **Workload standard** → workload layer image, shipped as content.
- **Instance-specific** → Ansible at runtime.

Domains where the workload does not change the answer — time sync, SELinux posture,
firewall policy — are base-only, with no workload override.

## Cross-cutting requirements

These IDs are stable citation targets for requirements that genuinely span components.
Implementation-local conventions are stated where they apply, without an ID; only
requirements that genuinely cross components carry one.

**G1 — Build-time firewall changes use the offline mechanism.** Any image build step that
manages firewall state uses `firewall-offline-cmd`. The runtime tool (`firewall-cmd`)
requires a running daemon and is valid only at runtime, never inside a build context.

**G2 — Management identity is pinned and consistent.** The management account has a fixed
name and a fixed numeric identity that does not drift across image rebuilds. The name and
numeric values must be identical wherever they are expressed (account declaration, file
ownership, connection user); divergence makes first-contact key material unreadable or
writes keys to the wrong home. This demo pins the name `ansible` and UID/GID `5000`; `5000`
is this demo's chosen value, not a universal requirement.

**G3 — The first-contact key is install-only.** The pre-shared bootstrap key (PSK) lives in
the account's standard `authorized_keys` location under the image's `/var` tree, present and
usable on a fresh install. After rotation nothing recreates it — not upgrade, rollback,
reboot, or boot-time file-creation hook. Only a full reprovision restores first-contact
behaviour. No boot-time file-creation entry may target that path.

**G4 — The application database secret has one source.** The application account password
resolves from exactly one shared location visible to both the database-provisioning phase and
the application-deployment phase (here, `ansible/inventory/group_vars/all/vault.yml`). It
must not be duplicated into a group-specific vault, a role default, or a plaintext file.
Under AAP the same variable name is supplied by a custom credential or survey variable.

**G5 — Corporate database service tuning is image-owned.** The `mariadb.service`
resource-limit drop-in is image content under `/usr/lib/systemd/system/`, never instance
`/etc`. It therefore updates unconditionally with the image; instance-specific tuning layers
on top through `systemctl edit`.

**G6 — The database tuned profile is image-owned and consistently named.** The corporate
database profile lives under `/usr/lib/tuned/`, and its directory name exactly matches the
active-profile selection written at build time (`corp-database` in this implementation).

**G7 — Application code and the application unit are instance-owned, never baked.** No image
bakes application code, an executable, or the `corp-app.service` unit. Deployment renders the
unit to `/etc/systemd/system/`; start and enable happen only at release, after the payload,
configuration, and migrations are in place. The application image is a reusable runtime
baseline, not an application image.

**G8 — SSH policy is delivered by drop-in.** SSH hardening lives in a file under
`/etc/ssh/sshd_config.d/`. The base `sshd_config` is never modified or replaced directly.

**G9 — No runtime OS package management; scoped Python exception.** No Ansible task installs
or updates operating-system packages (`dnf`/`yum`/`rpm`/`package`/`dnf-automatic`); OS
packages are bake-time only. The deliberate exception is application-level Python
dependencies: Ansible may install them at runtime only into the application virtualenv, only
with fully pinned versions, and never with `--upgrade`, unbounded specifiers, or into system
site-packages. The shared corporate Python stack is baked, not installed at runtime.

**G12 — The application runtime config location is a single agreed interface.** One path is
used identically by the application executable, the Ansible-rendered template, and the
service process. The current fixture value is `/etc/corp-app/config.ini`. Any divergence
between two of the three silently breaks the application.

**G14 — Application payloads deploy at runtime to persistent machine-local storage.** No
image bakes application code. The runtime image must provide a persistent, writable
deployment root on `/var` (this implementation exposes `/opt/corp-app` →
`/var/opt/corp-app`), and the virtualenv is created with access to system site-packages so
the baked corporate stack is inherited without re-installation. The literal path is a
fixture deployment value, not a universal rule.

**G15 — There is one corporate Python standard.** The DB driver set (`python3-PyMySQL`,
`python3-psycopg2`) and the data-abstraction layer (SQLAlchemy, Alembic, Pydantic) are
defined once, baked into the application runtime image, and are the single source of truth.
Ansible roles and the payload must not re-install, shadow, or pin alternate versions; they
add only genuinely app-specific extras. Exact versions are implementation detail.

**G16 — Runtime Python installs are pinned.** Every runtime Python install performed by
Ansible specifies an exact pinned version. Floating ranges, `latest`, and `--upgrade` are
prohibited; pinning is the reproducibility guarantee.

**G17 — Deployment and functional verification are separate.** Deployment plays and roles
contain no functional application exercises. Release may perform one lightweight loopback
readiness gate to confirm the service came up. All functional verification — read
assertions, write probes, negative-input checks — lives only in the optional, user-run
verification exercise, is never a deployment prerequisite, and is never imported by the
deployment entrypoint.

**G18 — Operator tooling follows a baked-versus-break-glass tier.** Tools baked into an
image must be frequently needed and host-coupled. Episodic or high-privilege diagnostics
(for example packet capture) are not baked; they are break-glass (a documented, transient,
non-durable manual install) or one-shot container tooling. No Ansible task performs a
break-glass install.

**G19 — External exposure is a deliberate, separate step.** Neither deployment nor release
opens the application's external firewall port. After release the service is healthy and
usable on the host's loopback but unreachable from the network; only a clearly labelled,
user-invoked exposure action opens the port. This default-deny gap is an intentional
teaching requirement — default-deny posture, loopback reachability is not network
reachability, and the firewall is runtime-owned — and must be documented as a lesson, never
as a defect.

## Lifecycle requirements

### First contact and onboarding

- Onboarding is a single discrete unit: probe with the PSK; on **explicit probe success**,
  install the live management key and remove the PSK; then verify that all hosts are
  reachable with the live key before workload automation proceeds.
- Rotation is gated on explicit probe success. An unreachable host is not "probe failed, skip
  rotation"; the probe outcome is the unambiguous discriminator.
- There is no reprobe after rotation. Successful completion of the live-key verification is
  the check.
- Onboarding is idempotent: re-running it against an already-onboarded host is a no-op and
  never re-rotates.
- The PSK's presence or absence is the sole discriminator between new and onboarded hosts.
- After onboarding, all workload automation uses the live key only, unconditionally. The live
  key is the control node's default management key; the PSK is used only during onboarding.
- The onboarding phase is separable by credential for AAP.

### Database foundation

- The DB foundation reaches a fresh, unconfigured MariaDB as root through the local unix
  socket with elevation; no pre-set root password is required.
- It waits for the database service to be active before doing database work.
- It creates the application database and a dedicated application account that is reachable
  from the application host (not `localhost`, not `%`). Privileges are scoped to that
  database and include the DDL the application's migrations require, so the application team
  can own its schema; the grant never becomes global.
- It narrows the database port at runtime, replacing the broad image-baked open with a rule
  restricted to the application host.
- It creates no application schema and no seed data; that belongs to the application's
  migrations.
- It does not set a database root password; the unix socket remains the maintenance path.
- A stricter posture — separate release-time DDL and runtime DML credentials — is a named
  production next step, intentionally out of scope.

### Deploy and release

- Deploy installs the payload and a virtualenv that inherits the baked corporate stack, adds
  only the application's pinned extras, writes the runtime configuration and the
  instance-owned unit, and does **not** start the service, run migrations, or open a firewall
  port.
- Release applies the application-owned schema and seed through its migration tool, enables
  and starts the service, and confirms liveness with one loopback readiness gate. It performs
  no functional exercises and does not open the external application port.
- The application's connection to the remote MariaDB is a genuine network path. This is
  distinct from external client reachability of the application port and must not be
  described as loopback.

### Day-2 state and upgrade safety

A `bootc upgrade` or rollback must leave onboarding state and data intact: the live key
present, the PSK absent, database data preserved, the deployed payload and `/etc` runtime
configuration preserved, and services still enabled. Image-owned standards under `/usr` are
replaced atomically by the upgrade; `/var` is not re-applied.

## Integration agreement with the fixture

The project depends on the supplied fixture only through the interfaces below. The concrete
values are **current fixture interfaces**, not general project requirements. Changing a
fixture interface requires updating this agreement and every dependent component together.

- **Runtime configuration.** The fixture reads `/etc/corp-app/config.ini` only. The file has
  `[database]` keys `host`, `port`, `name`, `user`, `password` and `[server]` key `port`.
  There is no bind-address key; the executable binds `0.0.0.0`. This agrees with G12.
- **HTTP surface.** `GET /health` returns 200 with `database == "connected"` on success and
  503 on database failure. `GET /` returns the stored records, including the migration seed.
  `POST /record` returns 201 for a valid write and 400 for malformed input. Error responses
  are JSON. The release readiness gate and the optional verification exercise use these
  endpoints.
- **Schema ownership.** The fixture's own migrations create its application table and apply
  one idempotent bootstrap seed row. The project creates no schema and no seed. The running
  service issues no DDL and uses only `SELECT`/`INSERT`.
- **Deployment shape.** The payload deploys under `/opt/corp-app` with its virtualenv at
  `/opt/corp-app/venv`; the service unit is `corp-app.service`; the listen port is the
  inventory's `app_port` (`8080` in this demo).
- **Network paths.** The payload connects to the remote MariaDB over the network. The
  application's own external port is closed until the deliberate exposure step (G19).
- **Credentials.** The application database account and password are supplied through the
  single shared secret described by G4; the same value is used when the account is created,
  when the runtime config is rendered, and when migrations run.
- **Privilege scope.** The application account holds DDL on its own database so the fixture
  can own its schema end to end. The running service needs only `SELECT` and `INSERT`. That
  gap is a deliberate, visibly flagged least-privilege lesson; the production split into a
  release-time migration credential and a least-privilege runtime credential remains a named
  next step.

## AAP compatibility

The playbooks must run unchanged under AAP as well as from a plain checkout.

- An AAP job template carries exactly one machine credential. Onboarding (the PSK rotation
  phase) must be runnable as its own job with the bootstrap credential.
- The workload phases — database foundation, deploy, and release — must be runnable as a job
  with the live credential. The live-key verification that is part of a standalone
  onboarding run is covered by the workload job's first connection or skipped by tag under
  AAP.
- Optional functional verification and optional firewall exposure are separate jobs or manual
  runs, never chained into the workload job.
- Under AAP, vault files are replaced by custom credentials or survey variables. Variable
  names remain identical so roles are unchanged. The README must document both the
  credential/job split and the vault-to-credential translation.

## Secrets and control-node requirements

- Sensitive values are vault-encrypted alongside their plaintext counterparts for standalone
  execution; the vault password file is gitignored.
- Private key material and the vault password file are gitignored; only the key-generation
  script is committed.
- The project is runnable from a plain local checkout: inventory, role paths, vault password
  file, and collection requirements all live in the repository. All `ansible-playbook`
  invocations run from the `ansible/` directory, and every path in `ansible.cfg` is relative
  to it.
- Required collections are `ansible.mariadb` and `ansible.posix`. The application plays use
  only `ansible.builtin` and introduce no collection dependency.
- SSH pipelining is enabled; it is required for correct sudo behaviour with NOPASSWD
  accounts.
- Host-key checking is relaxed for demo convenience and documented as such; production uses
  `accept-new` or a pre-populated `known_hosts`.
- A strict corporate posture is a named next step, not a deliverable: scope NOPASSWD sudo,
  split the migration and runtime database credentials, add database TLS, and sign images.

## Documentation requirements

Reader-facing documentation must explain the running implementation and must not become a
second normative requirements source:

- The bake-time/runtime boundary and why it is drawn where it is.
- The `rootfs/` mirror-tree convention: the build context documents what lands where.
- Key generation must precede any image build; a later build produces a key mismatch.
- Collection installation, vault setup, and end-to-end execution from the `ansible/`
  directory.
- Team ownership, and that the layered `FROM base` structure and the Ansible phase split are
  the team boundaries.
- The corporate Python standard as one source of truth, the deployment target and virtualenv
  model, and the runtime package policy.
- Deploy versus verification versus exposure: the deployment sequence, schema/seed as
  application-owned migrations, optional verification as a separate user exercise, and the
  default-deny teaching arc (working on loopback does not mean reachable on the network).
- PSK lifecycle and the no-resurrection guarantee, including the stale-host policy
  (reprovision, never rescue).
- Operator tooling tiers: baked host-coupled tools versus break-glass versus one-shot
  containers.
- The AAP credential/job split and vault-to-credential translation.

The README is the entry point and must state the `ansible/` working-directory requirement.
Gate IDs quoted in comments and documents are defined in this file.

## Acceptance criteria

A reviewer can verify each criterion without reading this file's history.

1. All three images build cleanly, and `bootc container lint` passes on each. The expected
   warning about first-contact content under `/var` is documented as intentional, not
   suppressed.
2. The application runtime image bakes the corporate Python stack (DB drivers and
   SQLAlchemy/Alembic/Pydantic) and contains no application code, no systemd unit, and no
   application firewall port.
3. A freshly installed, never-onboarded host is reachable by Ansible using the PSK, and the
   key is present in the standard `authorized_keys` location with usable ownership and
   permissions.
4. Onboarding installs the live key and removes the PSK. Re-running onboarding against the
   same host makes no changes.
5. On a fresh instance, the DB foundation creates the application database and a host-scoped
   account with the DDL its migrations require, and narrows the database port to the
   application host. It creates no application schema or seed data.
6. Deploy installs the payload into a virtualenv that inherits the baked stack, adds only
   pinned application extras, writes the config and the instance-owned unit, and does not
   start the service. Release applies the application's migration (creating its schema and
   idempotent seed), starts the service, and passes a loopback readiness gate. The
   application authenticates to the database over the network.
7. Immediately after release, the application is usable on the host's loopback but is not
   reachable from the network. The optional functional verification runs on the application
   host over loopback and passes without opening the external port. Only the separate,
   explicitly labelled exposure action opens the external application port.
8. Deployment runs as a single `ansible-playbook site.yml` invocation (the deployment phases
   only; verification and exposure excluded), and as separable AAP jobs with no playbook
   changes.
9. A `bootc upgrade` leaves state intact: the live key present, the PSK absent, database data
   preserved, the deployed payload and `/etc` runtime configuration preserved, and services
   enabled.
10. On the database host the active tuned profile is the corporate database profile, and the
    MariaDB service runs with the corporate unit drop-in limits in effect.
11. No Ansible task installs an OS package. The only runtime package installs are
    application-specific Python extras, pinned, into the application virtualenv.
12. Documentation and code comments contain no claim that deployment opens the application's
    external port, and no claim that the optional functional verification performs network
    exposure or network verification. Comments distinguish the application's genuine
    network connection to MariaDB from external client reachability of the application port.
