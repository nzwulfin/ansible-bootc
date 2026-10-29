# bootc + Ansible Reference Pattern

A reference implementation showing the correct division of responsibility between
**bootc** (image-mode Linux, CentOS Stream 10) and **Ansible** for a corporate
standard: how a MariaDB server runs, consumed by a minimal application service.
Optimised for clarity as a teachable pattern — simple and explicit, not production
automation.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Image Layering and Repository Structure](#2-image-layering-and-repository-structure)
3. [PSK Lifecycle](#3-psk-lifecycle)
4. [Key Generation → Image Build Ordering](#4-key-generation--image-build-ordering)
5. [Setup and Execution](#5-setup-and-execution)
6. [Practitioner Notes](#6-practitioner-notes)
7. [AAP Compatibility](#7-aap-compatibility)

---

## 1. Architecture Overview

### The bake-time / runtime boundary

The central design question is: *which system owns each piece of configuration?*
This project answers with a single rule:

| Configuration kind | Owner | Mechanism |
|--------------------|-------|-----------|
| Platform standard (SSH policy, management account, firewall baseline) | Base bootc image | `COPY rootfs/ /` in the Containerfile |
| Workload standard (MariaDB resource limits, tuned profile, corporate DB config) | Workload bootc image | `COPY rootfs/ /` in the workload Containerfile |
| Instance-specific runtime state (database credentials, app config, live SSH key) | Ansible | Roles: `mysql_instance`, `app_instance`, `bootstrap_key_rotation` |

Anything that is **the same on every host running this image** lives in the image.
Anything that **differs between hosts** or **must not exist until after first contact**
lives in Ansible.

### The three-tier configuration domain rule

```
Platform standard  →  base image       (time sync, SELinux posture, firewall policy)
Workload standard  →  workload image   (MariaDB tuning, app unit, corporate DB config)
Instance-specific  →  Ansible          (credentials, live SSH key, runtime config)
```

Configuration domains where the workload answer is the same as the platform answer —
time sync, SELinux posture, global firewall policy — are **base-only**: no workload
image overrides them.

### Why the boundary is drawn where it is

**bootc's `/usr` is immutable at runtime.** Service unit files, executables, sysusers
and tmpfiles declarations, and tuned profiles all live under `/usr/lib`. They update
atomically when the image updates and cannot be edited on a running host. The corporate
standard for *how MariaDB runs* (file-descriptor limits, character set, tuned profile)
lives here so that a `bootc upgrade` automatically propagates it to every host in the
fleet without a separate configuration management run.

**bootc's `/etc` is machine-local config subject to 3-way merge.** The SSH hardening
drop-in, the MariaDB baseline `cnf`, and the application runtime config land here.
Ansible writes instance-specific values (credentials, database host pointer) into
`/etc`; a `bootc upgrade` merges them so local changes survive.

**bootc's `/var` is first-contact state.** The pre-shared bootstrap key lives here
because bootc applies `/var` only at initial install — it is never re-applied by
`bootc upgrade`. That is exactly the "present at first contact, never resurrected"
lifecycle the bootstrap key requires. See [§3 PSK Lifecycle](#3-psk-lifecycle).

---

## 2. Image Layering and Repository Structure

### Image layering is the team boundary

```
quay.io/centos-bootc/centos-bootc:stream10
    └── corp-base:latest          ← platform/security team
          ├── corp-db:latest      ← database workload team
          └── corp-app-img:latest ← application workload team
```

Layering via `FROM corp-base:latest` is the organisational boundary: the platform
team owns the base image; workload teams derive from it. No multi-stage Containerfile
builds are needed — nothing is compiled. Each layer adds only what its workload requires.

### The `rootfs/` mirror-tree convention

Every image directory contains a `rootfs/` subtree that mirrors the target filesystem.
The single Containerfile step `COPY rootfs/ /` copies it all in one layer. This
convention makes the build context the documentation of what lands where on a deployed
system — you can navigate `images/base/rootfs/` to understand the deployed state
without reading Containerfile logic.

```
images/base/rootfs/
├── etc/
│   ├── sudoers.d/99-ansible                          → /etc/sudoers.d/99-ansible
│   └── ssh/sshd_config.d/50-corporate-hardening.conf → /etc/ssh/sshd_config.d/
├── var/
│   └── home/ansible/.ssh/authorized_keys            → PSK, first-contact only
└── usr/lib/
    ├── sysusers.d/ansible.conf                       → management account, UID 5000
    └── tmpfiles.d/ansible-home.conf                  → home dir (d entries only)
```

### Repository layout

```
ansible-bootc/
├── README.md
├── docs/
│   ├── plan.md                               Requirements and design decisions
│   └── design-spec.md                        Technical design specification
├── scripts/
│   └── generate-demo-keys.sh                 MUST run before any image build
├── images/
│   ├── base/
│   │   ├── Containerfile
│   │   └── rootfs/                           Mirrors /; COPY rootfs/ / installs all
│   │       ├── etc/sudoers.d/99-ansible
│   │       ├── etc/ssh/sshd_config.d/50-corporate-hardening.conf
│   │       ├── var/home/ansible/.ssh/authorized_keys   ← gitignored, generated
│   │       └── usr/lib/
│   │           ├── sysusers.d/ansible.conf
│   │           └── tmpfiles.d/ansible-home.conf
│   ├── database/
│   │   ├── Containerfile
│   │   └── rootfs/
│   │       ├── etc/my.cnf.d/00-corporate.cnf
│   │       └── usr/lib/
│   │           ├── systemd/system/mariadb.service.d/10-corporate.conf
│   │           └── tuned/corp-database/tuned.conf
│   └── application/
│       ├── Containerfile
│       └── rootfs/
│           └── usr/
│               ├── lib/systemd/system/corp-app.service
│               └── local/bin/corp-app.py
└── ansible/
    ├── ansible.cfg                           Run from ansible/ directory
    ├── requirements.yml                      community.mysql, ansible.posix
    ├── site.yml                              Master entrypoint
    ├── inventory/
    │   ├── hosts.yml                         db01, app01 — update IPs here
    │   └── group_vars/
    │       ├── all/vars.yml                  Shared non-sensitive vars
    │       ├── all/vault.yml                 app_db_password (vault-encrypted)
    │       ├── database/vars.yml             app_host_address
    │       └── application/vars.yml          db_host, app_port
    ├── keys/                                 Generated key material — all gitignored
    ├── playbooks/
    │   ├── 00_bootstrap.yml                  PSK probe → rotation → live key verify
    │   ├── 01_database.yml                   MariaDB provisioning
    │   └── 02_application.yml               App config, enable, health check
    └── roles/
        ├── bootstrap_key_rotation/           PSK → live key rotation
        ├── mysql_instance/                   DB + account + firewall narrowing
        └── app_instance/                     Config, enable, health verify
```

---

## 3. PSK Lifecycle

The pre-shared bootstrap key (PSK) is the sole mechanism for reaching a freshly
provisioned, never-onboarded host. Its lifecycle is invariant:

| Phase | `authorized_keys` state | Mechanism |
|-------|------------------------|-----------|
| Image build | PSK entry (with restrictive options), owned `5000:5000`, mode `0600` | `scripts/generate-demo-keys.sh` writes the file; Containerfile sets ownership/mode |
| First boot | PSK entry present and usable | bootc applies `/var` tree at initial install only |
| After `00_bootstrap.yml` | Live key only; PSK absent | `bootstrap_key_rotation` role installs live key, removes PSK |
| `bootc upgrade` | Unchanged (live key only) | `/var` is never merged or re-applied by bootc upgrade |
| Reboot | Unchanged | `tmpfiles.d` has only `d` entries for `.ssh/`; existing files are not recreated |
| Re-run `00_bootstrap.yml` | Unchanged | Play 1 probe fails (PSK gone); Play 2 skipped; Play 3 verifies live key still works |
| Stale host (old image, new PSK generated) | Cannot be onboarded | Decommission and reprovision; never rescued |

### No resurrection path

After rotation, **nothing re-creates the PSK entry** — not upgrades, not reboots,
not re-running playbooks. The only way to restore first-contact behaviour is a fresh
provisioning from the image (which re-applies `/var`). This is the "install-only `/var`
semantics" guarantee.

### Restrictive SSH options on the PSK line

The PSK entry in `authorized_keys` carries four SSH option tokens:

```
no-pty,no-X11-forwarding,no-agent-forwarding,no-user-rc ssh-ed25519 AAAA...
```

These limit what the shared provisioning key can do:

| Option | Restriction |
|--------|-------------|
| `no-pty` | No interactive terminal — Ansible's non-interactive command execution does not require one |
| `no-X11-forwarding` | No X11 session forwarding |
| `no-agent-forwarding` | No SSH agent forwarding |
| `no-user-rc` | Do not execute `~/.ssh/rc` on login |

Ansible's `authorized_key` module matches keys by fingerprint and correctly identifies
and removes the PSK entry even though it carries this options prefix.

### Expected `bootc container lint` warning

Running `bootc container lint` on the base image will produce a warning about content
under `/var`:

```
WARN: Image contains content under /var — bootc expects /var to be managed at
      runtime by the host, not baked into the image.
```

**This warning is intentional, not a defect.** The PSK must live under `/var` because
that is the only bootc filesystem zone with "applied at initial install only, never
re-applied on upgrade" semantics — exactly what the first-contact-only lifecycle
requires. The alternatives (tmpfiles.d `f~` entry, `/etc` with Ansible cleanup) all
introduce a resurrection path that would defeat the guarantee. The warning is documented
here so operators know to expect it and need not investigate.

---

## 4. Key Generation → Image Build Ordering

**The key generation script must run before any image build.** This is a hard ordering
constraint, not a suggestion:

```
scripts/generate-demo-keys.sh       ←  MUST come first
    ↓
podman build -t corp-base:latest images/base/
    ↓
podman build -t corp-db:latest images/database/
podman build -t corp-app-img:latest images/application/
```

**Why the order matters:** `scripts/generate-demo-keys.sh` writes the bootstrap public
key into `images/base/rootfs/var/home/ansible/.ssh/authorized_keys`. The base
Containerfile copies that file into the image layer (`COPY rootfs/ /`). If you build
the image first and generate the keys after, the image contains no `authorized_keys`
file (the build actually fails with an explicit error) — or worse, if a previous
`authorized_keys` was present, the image and the Ansible key material are out of sync
and newly provisioned hosts cannot be onboarded.

**What happens if you regenerate keys after provisioning:** Any host already provisioned
from an image built with the old PSK becomes unreachable with the new bootstrap key.
The script guards against this by refusing to overwrite existing keys:

```
ERROR: Keys already exist in ansible/keys/.
       Regeneration would invalidate all images built with the current
       bootstrap key. Hosts provisioned from those images cannot be
       onboarded with a new PSK and must be decommissioned and
       reprovisioned from a freshly built image.
```

---

## 5. Setup and Execution

All steps from a fresh checkout to a running system, in order.

### Step 1 — Generate the demo key pairs

Run from the repository root:

```bash
# Working directory: repo root (ansible-bootc/)
bash scripts/generate-demo-keys.sh
```

This generates four files in `ansible/keys/`:

```
ansible/keys/id_bootstrap       Private bootstrap (PSK) key — Ansible bootstrap credential
ansible/keys/id_bootstrap.pub   Public bootstrap key — written into the base image
ansible/keys/id_live            Private live management key — all post-bootstrap access
ansible/keys/id_live.pub        Public live key — installed by bootstrap_key_rotation role
```

All four files are gitignored.

### Step 2 — Build the images

Build in order: base first, then database and application (both depend on the base tag):

```bash
# Working directory: repo root (ansible-bootc/)
podman build -t corp-base:latest images/base/
podman build -t corp-db:latest images/database/
podman build -t corp-app-img:latest images/application/
```

### Step 3 — Provision VMs

Converting bootc images to qcow2 disk images and provisioning libvirt VMs is known
procedure and is out of scope for this repository. Use `bootc-image-builder` or the
equivalent for your environment:

```bash
# Example only — adapt to your environment
sudo podman run --rm --privileged \
    --volume /var/lib/containers/storage:/var/lib/containers/storage \
    quay.io/centos-bootc/bootc-image-builder:latest \
    --type qcow2 \
    quay.io/yourrepo/corp-db:latest
```

Provision one VM for the database host and one for the application host, noting their
IP addresses.

### Step 4 — Update the inventory

Edit the inventory to match your VM addresses:

```bash
# Working directory: repo root (ansible-bootc/)
# Edit ansible/inventory/hosts.yml
# Edit ansible/inventory/group_vars/database/vars.yml   — set app_host_address
# Edit ansible/inventory/group_vars/application/vars.yml — set db_host
```

The IP of `app01` and `app_host_address` must be identical. The IP of `db01` and
`db_host` must be identical. A divergence breaks the database account host restriction
and the config template silently.

### Step 5 — Install Ansible collections

All subsequent Ansible commands must be run from the `ansible/` directory (G11). Paths
in `ansible.cfg` are relative to that directory; running from the repo root causes
path-resolution failures.

```bash
# Working directory: ansible/
cd ansible/
ansible-galaxy collection install -r requirements.yml
```

### Step 6 — Create the vault password file and encrypt the vault

```bash
# Working directory: ansible/
# Create a vault password file (gitignored)
echo 'your-strong-random-passphrase' > keys/.vault_pass
chmod 0600 keys/.vault_pass

# Edit the vault file to set app_db_password, then encrypt it
# The vault file is at inventory/group_vars/all/vault.yml
ansible-vault encrypt inventory/group_vars/all/vault.yml

# Also encrypt the placeholder vault files for the host groups (optional)
# These files are empty placeholders for future group-specific secrets.
# Encrypting them is optional; they contain no sensitive values in this reference pattern.
ansible-vault encrypt inventory/group_vars/database/vault.yml
ansible-vault encrypt inventory/group_vars/application/vault.yml
```

The `all/vault.yml` file contains `app_db_password`. This is the only place this
variable may appear (design constraint G4): it must be visible to both the
`mysql_instance` role (database group) and the `app_instance` role (application group),
which requires placement in `all/` rather than any group-specific vault.

### Step 7 — Run the playbooks

**Standalone (single invocation):**

```bash
# Working directory: ansible/
ansible-playbook site.yml
```

`site.yml` imports the three playbooks in order:

1. `playbooks/00_bootstrap.yml` — PSK probe → key rotation → live key verify
2. `playbooks/01_database.yml` — MariaDB provisioning
3. `playbooks/02_application.yml` — App config, enable, health check

**Or run individually:**

```bash
# Working directory: ansible/
ansible-playbook playbooks/00_bootstrap.yml
ansible-playbook playbooks/01_database.yml
ansible-playbook playbooks/02_application.yml
```

**Verify the health endpoint (run on the application host):**

```bash
curl -s http://127.0.0.1:8080/health
# Expected: {"database": "connected", "detail": "ok"}
```

---

## 6. Practitioner Notes

### Team split: layering is the boundary

The `FROM corp-base:latest` line in the database and application Containerfiles is
literally the team boundary in code. The platform/security team publishes `corp-base`;
workload teams pull from it and add their layer. No multi-stage build stages are
needed — nothing in this pattern is compiled, so there is no "build artefact"
separation requirement. The layered `FROM` is the full story.

### Shared PSK: intentional design and blast radius

One bootstrap key pair per image build covers first-contact access for the entire
fleet built from that image. The blast radius of a compromised PSK is limited to
unprovisioned hosts only — once a host is onboarded, the PSK is gone and the live
key is the sole management path.

Rotating the PSK means rebuilding the base image (which requires re-running
`scripts/generate-demo-keys.sh`, rebuilding the base image, and reprovisioning new
hosts from the new image). Stale hosts — provisioned from an older image with the old
PSK, not yet onboarded — cannot be bootstrapped with a new PSK. The stale-host
policy is clear: decommission and reprovision from the current image. There is no
rescue path.

### PSK lifecycle and the no-resurrection guarantee

The PSK lives under `/var` because bootc applies `/var` at initial install only. Nothing
in this design recreates the PSK after rotation:

- **`bootc upgrade`** — does not re-apply `/var`; the live key survives unchanged.
- **Reboots** — `tmpfiles.d/ansible-home.conf` contains only `d` (directory create)
  entries for `.ssh/`. A `d` entry on a directory that already exists is a no-op;
  existing files (including `authorized_keys`) are never touched.
- **Re-running `00_bootstrap.yml`** — Play 1 fails to authenticate via PSK (it is
  already gone); Play 2 skips rotation (gated on explicit probe success); Play 3
  confirms the live key still works. `authorized_keys` is left unchanged.
- **Rollback** — if a host rolls back to a previous image, `/var` is not re-applied;
  the live key remains.

This is the "install-only `/var` semantics" guarantee, and it is the reason the PSK
must live under `/var` rather than in a `tmpfiles.d` `f~` entry or any other mechanism
that would reintroduce a resurrection path.

### Stale-host policy

An unprovisioned host that outlives a PSK rotation (i.e., a host provisioned from an
old image after the key material has been regenerated) must be **decommissioned and
reprovisioned from the current image**. There is no way to onboard such a host: the
new PSK is not in the old image, and there is no interactive login path and no fallback
credential. The design accepts this constraint as the price of the no-resurrection
guarantee.

### No interactive login path: intentional design

The SSH hardening drop-in (`50-corporate-hardening.conf`) sets `AllowUsers ansible`.
This means:

- No root SSH login (`PermitRootLogin no`).
- No password authentication (`PasswordAuthentication no`).
- No SSH access for any account other than `ansible`.
- A fresh VM has no interactive login path for any human operator.

This is intentional. Break-glass access is:

1. **Live key** — the normal management path after onboarding.
2. **Full re-provisioning** — the recovery path if the live key is lost after rotation.

A host whose live key is lost after bootstrap rotation must be re-provisioned. There is
no fallback credential, and that is by design: a fallback credential would be a
persistent attack surface on every host in the fleet.

### Firewall architecture: bake-time open narrowed at runtime

The database image ships with the MySQL service open in the firewall:

```
# Containerfile — bake-time posture
RUN firewall-offline-cmd --add-service=mysql
```

This broad open is intentional for test environments: any host can reach the database
port without any runtime configuration. The `mysql_instance` Ansible role narrows it at
runtime to the application host's IP address only:

```yaml
# Remove the broad service rule
- ansible.posix.firewalld:
    service: mysql
    state: disabled
    permanent: true
    immediate: true

# Add a source-restricted rich rule
- ansible.posix.firewalld:
    rich_rule: "rule family='ipv4' source address='{{ app_host_address }}/32' service name='mysql' accept"
    state: enabled
    permanent: true
    immediate: true
```

This is the bake-time/runtime split made concrete: bake what is safe for the whole fleet
(broad open for any test environment), narrow what is instance-specific (restrict to the
actual application host IP) at runtime via Ansible.

**Why the base image explicitly opens the `ssh` service:** firewalld's default zone
(`public`) drops all incoming traffic. An image that installs firewalld but does not
explicitly open SSH would lock out every management connection — the Ansible account
could not reach the host at all. The base Containerfile uses
`firewall-offline-cmd --add-service=ssh` to declare SSH as part of the image's own
management surface, ensuring that a freshly provisioned host is always reachable for
first contact.

Note: all firewall configuration in Containerfiles uses `firewall-offline-cmd`, not
`firewall-cmd`. The firewalld daemon is not running during an image build; the offline
tool edits zone files on disk directly. The runtime Ansible tasks correctly use the
`ansible.posix.firewalld` module with `immediate: true` (which requires a running
daemon, available at runtime).

### Application service: baked but NOT enabled (G7)

`corp-app.service` is baked into the application image under
`/usr/lib/systemd/system/corp-app.service` but the Containerfile **never runs
`systemctl enable corp-app`**. The reason is ordering:

1. The service reads `/etc/corp-app/config.ini` at startup.
2. That file does not exist until the `app_instance` Ansible role writes it.
3. Enabling the unit in the image causes a crash-loop on a fresh install before Ansible
   has run.

Ansible performs the enable and start after writing the config:

```yaml
- ansible.builtin.systemd:
    name: corp-app
    state: started
    enabled: true
    daemon_reload: true
```

**Override mechanism:** Operators tune service behaviour via `systemctl edit corp-app`,
which writes drop-ins into `/etc/systemd/system/corp-app.service.d/`. This layers on
top of the image-owned unit without modifying it (the unit would be lost on the next
image update if edited directly).

**Systemd sandboxing directives baked into `corp-app.service`:** The unit carries a
deliberately small set of hardening directives to reduce the blast radius of the service
without making the demo difficult to debug:

| Directive | Effect |
|-----------|--------|
| `NoNewPrivileges=yes` | Prevents the process (and any child) from gaining elevated privileges via `setuid` binaries or capabilities |
| `ProtectSystem=strict` | Mounts the entire filesystem read-only except for `/dev`, `/proc`, and `/sys`; the service cannot modify system files |
| `ProtectHome=yes` | Makes `/home`, `/root`, and `/run/user` invisible and inaccessible to the service |
| `PrivateTmp=yes` | Gives the service its own isolated `/tmp` and `/var/tmp` namespaces |

These are overridable via `systemctl edit corp-app` drop-ins. For example, a production
workload that requires write access to a specific path would add a `ReadWritePaths=`
directive in a drop-in rather than loosening `ProtectSystem`.

**Runs as root:** The service unit does not set `User=` or `Group=`, so it runs as
root. This is intentional for the demo: the config file `/etc/corp-app/config.ini` is
mode `0640` owned `root:root`, so the service must run as root to read it. A production
service would create a dedicated service account and adjust the config permissions
accordingly.

### Service tuning: `/usr/lib/` placement and why `limits.d` does not work

The `mariadb.service` resource-limit drop-in lives at
`usr/lib/systemd/system/mariadb.service.d/10-corporate.conf` in the database image's
`rootfs/`. This is the image-owned location (G5). The corporate standard —
`LimitNOFILE=65536`, `LimitNPROC=4096` — updates unconditionally with the image.

**Why not `/etc/systemd/system/`?** The `/etc/` location is machine-local config
subject to 3-way merge. Placing the corporate standard there would mean Ansible has to
enforce it at runtime, and an operator `systemctl edit mariadb` drop-in would have to
fight with it. The `/usr/lib/` location is clean: image owns the baseline, operators
layer instance-specific overrides via `systemctl edit` into `/etc/`.

**Why not PAM `limits.d`?** PAM limit files apply only to login sessions that pass
through PAM. `mariadb.service` is started by systemd and bypasses PAM entirely. A
`/etc/security/limits.d/mariadb.conf` entry has zero effect on the database process.
The unit drop-in is the correct and only reliable mechanism for enforcing resource
limits on a systemd-managed service.

The same reasoning applies to the `corp-database` tuned profile: it lives under
`usr/lib/tuned/corp-database/tuned.conf` (image-owned, G6), inherits from
`throughput-performance` (the platform baseline), and adds database-specific settings
(THP disabled, swappiness minimised, dirty-ratio raised, read-ahead configured). The
Containerfile activates it by writing `corp-database` to `/etc/tuned/active_profile`
at build time; `tuned-adm` requires a running daemon and cannot be used in a
Containerfile.

### `bootc upgrade` safety

A `bootc upgrade` replaces `/usr` atomically and applies a 3-way merge on `/etc`.
The onboarding state survives cleanly:

| What | Zone | Behaviour on upgrade |
|------|------|----------------------|
| Live SSH key (`/var/home/ansible/.ssh/authorized_keys`) | `/var` | Never touched by upgrade |
| Application config (`/etc/corp-app/config.ini`) | `/etc` | 3-way merge; local Ansible-written content survives |
| MariaDB config (`/etc/my.cnf.d/00-corporate.cnf`) | `/etc` | 3-way merge; image version wins if no local change |
| Service units (`/usr/lib/systemd/system/`) | `/usr` | Replaced; corporate standard always applied |
| Tuned profile (`/usr/lib/tuned/corp-database/`) | `/usr` | Replaced; corporate standard always applied |
| Management account (`systemd-sysusers`) | `/usr/lib/sysusers.d/` | Re-applied on every boot; drift-free |
| Home directory structure (`systemd-tmpfiles`) | `d` entries only | Directories re-created if absent; existing files never touched |

### Package rationale

| Package | Image | Reason |
|---------|-------|--------|
| `sudo` | base | Ansible `become: true` without a password; NOPASSWD:ALL in `99-ansible` |
| `openssh-server` | base | SSH daemon; key-only management |
| `python3` | base | Ansible module runtime |
| `rsync` | base | Ansible file-transfer backend |
| `iproute` | base | Operator tools: `ip`, `ss` |
| `firewalld` | base | Firewall daemon; offline config at build time |
| `tuned` | base | Platform tuning daemon |
| `tmux` | base | Persistent debug sessions for operators |
| `vim-minimal` | base | In-place editing of host config |
| `bind-utils` | base | DNS lookups (`dig`, `host`) |
| `mariadb-server` | database | Database engine |
| `mariadb` | database | Client CLI for local administrative access (root via unix socket) |
| `python3-PyMySQL` | database | Required by `community.mysql` Ansible modules AND any local DB client work |
| `python3` | application | App runtime; `corp-app.py` is a Python service |
| `python3-PyMySQL` | application | The app probes MariaDB at runtime; runtime dependency, baked per G9 |
| `curl` | application | Operator inspection of the loopback health endpoint (`curl http://127.0.0.1:8080/health`); G9 forbids runtime installs |

**No runtime package management (G9).** Ansible never installs or updates packages.
Every runtime dependency is baked into the appropriate image. This is an invariant:
a `dnf install` in an Ansible task is a design violation, not a shortcut.

### Database host discovery: static variable design decision

The `db_host` variable in `ansible/inventory/group_vars/application/vars.yml` is a
static string set by the operator at inventory-configuration time (Step 4). This is a
deliberate simplification for a reference pattern: the operator knows both VM addresses
when they populate the inventory, and a static variable is the most transparent and
debuggable mechanism.

Real-world options for making this dynamic:

| Option | When to use |
|--------|-------------|
| **Fact caching** | After a database play runs, the `ansible_default_ipv4.address` fact can be cached and referenced by the application play in the same run or a later run. Requires a fact cache backend (`jsonfile`, `redis`, etc.) in `ansible.cfg`. |
| **Dynamic inventory** | A dynamic inventory script or plugin (e.g., querying libvirt, a CMDB, or a cloud API) returns host addresses at runtime. The `hostvars['db01']['ansible_host']` reference then works without a static variable. |
| **Discovery task** | A dedicated pre-task or role queries the environment (DNS, a service registry, or the database group's `ansible_host`) and sets a fact used by later roles. Explicit and auditable. |

For this pattern, the static variable is correct: it matches the documented inventory
update step (Step 4) and avoids introducing a caching or discovery dependency that would
obscure the core bake-time/runtime design.

### Static IP inventory assumption

The reference inventory (`ansible/inventory/hosts.yml`) uses static IP addresses for
`db01` and `app01`. This is a deliberate choice for the reference pattern:

- Static IPs match the libvirt VM provisioning context (Step 3), where the operator
  assigns and knows both addresses.
- Static IPs make the inventory fully self-contained and auditable — no DNS dependency,
  no dynamic resolution, no external lookup at play time.
- The `app_host_address` and `db_host` variables that must match the inventory IPs are
  documented explicitly in Step 4 to prevent silent divergence.

In production, replace static addresses with DNS names or use a dynamic inventory
plugin. The playbooks and roles require no changes — only the inventory source changes.

### Security non-additions: deliberate decisions

The following things are **not** in this project, and that is intentional:

| Not done | Why it is a deliberate decision |
|----------|--------------------------------|
| No runtime update daemon (e.g., `dnf-automatic`) | The patching unit is image rebuild + `bootc upgrade`. Runtime package management contradicts the image-mode model. |
| No custom SELinux policy | The baked executables and standard paths use default contexts. The app runs unconfined; standard bootc images carry the base policy. A custom policy is a named next step. |
| No sysctl pile-up in `/etc/sysctl.d/` | Database-relevant sysctl tuning (`vm.swappiness`, dirty ratios) is delivered through the image-owned tuned profile (`corp-database`), not through a separate sysctl file. |
| No image signing | Image signing (e.g., sigstore cosign) is a named next step for production adoption. |
| No database TLS | MariaDB TLS configuration is a named next step. The current demo uses unix-socket root access and a network-restricted firewall rule. |
| NOPASSWD:ALL sudo | A demo convenience. Production would scope the sudo rule to specific commands required by Ansible modules. |
| `host_key_checking = False` | A demo convenience. Production should use `accept-new` or a pre-populated `known_hosts`. |

---

## 7. AAP Compatibility

The playbooks run **unchanged** under Ansible Automation Platform (AAP). The design
decisions that make this work are listed here.

### The one-credential-per-job constraint

An AAP job template carries exactly one machine credential. This matters because
`00_bootstrap.yml` uses two different SSH private keys: the bootstrap PSK for Plays 1
and 2, and the live key for Play 3. Locally, the playbook handles the credential switch
via per-play `ansible_ssh_private_key_file` overrides. Under AAP, the job template
level is the credential boundary.

### Job template split

Split the three playbooks across two AAP job templates:

| AAP Job | Playbook | Machine credential |
|---------|----------|--------------------|
| **Bootstrap** | `playbooks/00_bootstrap.yml` | Bootstrap credential (`ansible/keys/id_bootstrap`) |
| **Workload** | `playbooks/01_database.yml` + `playbooks/02_application.yml` | Live credential (`ansible/keys/id_live`) |

**Bootstrap job:** Uses the bootstrap private key. Plays 1 and 2 probe and rotate the
PSK. Play 3 (live-key verify) uses the live key — but under AAP's single-credential
constraint only the bootstrap key is available in this job. For that reason, **Play 3's
live-key verification is handled by the workload job's first connection** rather than
inside the bootstrap job. The bootstrap job's responsibility ends when the PSK is
successfully rotated out.

**Workload job:** Uses the live private key. Runs `01_database.yml` then
`02_application.yml`. No PSK is referenced in these playbooks. The workload job's first
connection implicitly proves live-key connectivity. The live credential is sufficient for
all workload automation.

> **The two-job split is mandatory under AAP.** Do not attempt to run `site.yml` as a
> single AAP job with the bootstrap credential. Once Play 2 rotates out the PSK, all
> subsequent SSH connections (including Play 3 and the workload plays) require the live
> key — which the bootstrap credential does not carry. The single-job approach fails
> after key rotation.

### Vault files → AAP Custom Credentials or Survey variables

Under AAP, vault-encrypted files are replaced by AAP Custom Credentials or Survey
variables. **Variable names remain identical**; roles are unchanged.

| Vault file | Variable | AAP replacement |
|------------|----------|----------------|
| `ansible/inventory/group_vars/all/vault.yml` | `app_db_password` | AAP Custom Credential (type: Secret Text) injected as an extra var, or a Survey variable — named `app_db_password` |

Because the variable names are unchanged, the `mysql_instance` role (which references
`app_db_password` in its `mysql_user` task) and the `app_instance` role (which renders
it into `config.ini.j2`) work without modification.

**Custom Credential type** (recommended for `app_db_password`):

```yaml
# AAP Custom Credential type definition
injectors:
  extra_vars:
    app_db_password: !unsafe "{{ app_db_password }}"
```

The Custom Credential type exposes the secret as an extra variable at job runtime,
scoped to the job and never written to disk in plaintext.

**Survey variable alternative:**

In an AAP job template survey, add a password-type question with variable name
`app_db_password`. The value is passed as an extra variable and overrides the inventory
var — the same mechanism, slightly less secure because survey values appear in the job's
extra-vars log. Custom Credentials are preferred for secrets.

### Running site.yml standalone vs. under AAP

```
Standalone:   cd ansible/ && ansible-playbook site.yml
AAP Job 1:    ansible-playbook playbooks/00_bootstrap.yml   (bootstrap credential)
AAP Job 2:    ansible-playbook playbooks/01_database.yml    (live credential)
              ansible-playbook playbooks/02_application.yml  (live credential, same job)
```

No playbook files are changed between standalone and AAP operation. The only difference
is which credential the job carries and how secrets are injected.
