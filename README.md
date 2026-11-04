# bootc + Ansible Reference Pattern

A reference implementation showing the correct division of responsibility between
**bootc** (image-mode Linux, CentOS Stream 10) and **Ansible** for a corporate
standard: how a MariaDB server runs, and how a reusable corporate **Python runtime
baseline** hosts an application that is *deployed onto* it — not baked into it.

The application image is a **runtime baseline**, not an app image: it bakes the
corporate Python stack (DB drivers + SQLAlchemy/Alembic/Pydantic) and no application
code. The app — its source, its pinned extras, and its Alembic migrations — is a
payload Ansible installs to `/opt/corp-app`. Deployment is separated from
demonstration: the app team owns two plays (deploy, then release/go-live), and all
functional verification lives in a separate, optional **proof-of-life** exercise.

Optimised for clarity as a teachable pattern — simple and explicit, not production
automation.

This reference pattern is written for an enterprise Linux systems administrator:

- **Traditional Automation (Assumed Fluent)**: You are experienced with RHEL administration, Ansible (roles, playbooks, inventory, vault), systemd, SSH, and firewalld. The repository does not explain basic playbook syntax or service drop-ins; it audits and implements them to senior enterprise standards.
- **Image-Mode Linux (Assumed First Contact)**: You are encountering **bootc** and image-mode Linux for the first time. The repository actively bridges your mental model: teaching the bake-time vs. runtime boundary, `/usr`/`/etc`/`/var` filesystem semantics, and how traditional golden-image concepts map into bootable container images.
- **Automation Platform (Conceptual)**: You have a working familiarity with Red Hat Ansible Automation Platform (AAP) concepts like Job Templates and Machine Credentials.

---

## The bake-time / runtime boundary

The central design question is: *which system owns each piece of configuration?*
This project answers with a single rule:

| Configuration kind | Owner | Mechanism |
|--------------------|-------|-----------|
| Platform standard (SSH policy, management account, firewall baseline) | Base bootc image | `COPY rootfs/ /` in the Containerfile |
| Workload standard (MariaDB resource limits, tuned profile, corporate DB config; corporate Python runtime stack) | Workload bootc image | `COPY rootfs/ /` and baked packages in the workload Containerfile |
| Application payload (code, pinned extras, Alembic migrations) | Application Development | Deployed by Ansible to `/opt/corp-app` (`app_deploy`) — never baked |
| Instance-specific runtime state (database credentials, app config, live SSH key, app schema) | Ansible | Roles: `mariadb_instance`, `app_deploy`, `app_release`, `bootstrap_key_rotation` |

Anything that is **the same on every host running this image** lives in the image.
Anything that **differs between hosts** or **must not exist until after first contact**
lives in Ansible.

## The three-tier configuration domain rule

```
Platform standard  →  base image       (time sync, SELinux posture, firewall policy)
Workload standard  →  workload image   (MariaDB tuning, app unit, corporate DB config)
Instance-specific  →  Ansible          (credentials, live SSH key, runtime config)
```

Configuration domains where the workload answer is the same as the platform answer —
time sync, SELinux posture, global firewall policy — are **base-only**: no workload
image overrides them.

## Image layering

```
quay.io/centos-bootc/centos-bootc:stream10
    └── corp-base:latest          ← Infrastructure / Operations
          ├── corp-db:latest      ← DBA
          └── corp-app:latest     ← application RUNTIME baseline (Middleware)
```

## Who owns what

| Team | Owns | Controls | A change ships via |
|---|---|---|---|
| Infrastructure / Operations | `corp-base` base image | Platform standard: SSH policy, management account, firewall baseline | Base image rebuild + `bootc upgrade` |
| DBA | `corp-db` database image + the DB foundation play | MariaDB workload standard: resource limits, tuned profile, corporate DB config | Foundation: image rebuild; instance state: re-run `database.yml` |
| Middleware | `corp-app` runtime baseline | The corporate Python stack baked into the application image | Application image rebuild + `bootc upgrade` |
| Application Development | `app/corp-app/` payload | App code, pinned extras, Alembic migrations | Ansible: `app_deploy` + `app_release` — never baked |

The security team owns security policy.

## Quick start

All steps from a fresh checkout to a running system, in order.

> **💡 Fast Path (Automated Local Demo):** If you are running on a local RHEL/CentOS KVM host
> with libvirt, `bash scripts/setup-demo-host.sh` automates Steps 1 through 4: host packages,
> keys, vault generation, image builds, qcow2 conversion, VM launch, and auto-populating
> `hosts.yml` with the dynamic DHCP IPs. You can then jump directly to **Step 5**.
> The manual walkthrough below explains what the script does and is the canonical procedure
> for non-libvirt environments.

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
podman build -t corp-app:latest images/application/
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

All subsequent Ansible commands must be run from the `ansible/` directory. Paths
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
variable may appear (the shared secret rule): it must be visible to the `mariadb_instance` role
(database group, which creates the account) and to the `app_deploy`/`app_release` roles
(application group, which render it into the config and use it as the Alembic migration
credential), which requires placement in `all/` rather than any group-specific vault.

### Step 7 — Run the deployment sequence

**Standalone (single invocation):**

```bash
# Working directory: ansible/
ansible-playbook site.yml
```

`site.yml` imports the four deployment playbooks in sequence:

1. `playbooks/bootstrap.yml` — PSK probe → key rotation → live key verify (platform)
2. `playbooks/database.yml` — DB foundation: database, account, firewall narrowing (DBA; no schema/seed)
3. `playbooks/app_deploy.yml` — app play 1: payload to `/opt/corp-app`, venv, pinned extras, config, unit (not started)
4. `playbooks/app_release.yml` — app play 2: Alembic migrate (schema + seed), enable/start, loopback readiness gate

**Or run individually:**

```bash
# Working directory: ansible/
ansible-playbook playbooks/bootstrap.yml
ansible-playbook playbooks/database.yml
ansible-playbook playbooks/app_deploy.yml
ansible-playbook playbooks/app_release.yml
```

### Step 8 — Test, Troubleshoot, and Expose

Follow the operator workflow to experience the default-deny firewall posture:

1. **Attempt external curl (Expected to fail):**
   ```bash
   # From your control node terminal:
   curl -s --max-time 3 http://app01:8080/health || echo "Blocked by firewall (Expected default-deny)"
   ```
   *The connection times out because the application host enforces the corporate security baseline firewall posture inherited directly from `corp-base` (which opens SSH only).*

2. **Verify host connectivity (Is the VM alive?):**
   ```bash
   ping -c 1 app01
   ```
   *The ping returns immediately. This proves `app01` is online, network routing is intact, and `libvirt-nss` is actively resolving the hostname — isolating the failure directly to the host's firewall.*

3. **Verify app-to-database on the host (Proof of Life):**
   Run the verification playbook against `app01` to confirm the application is healthy and actively communicating with MariaDB:
   ```bash
   # Working directory: ansible/
   ansible-playbook playbooks/proof_of_life.yml
   ```
   *All checks succeed on-host over loopback (health connected, seed visible, live write probe, input validation).*

4. **Troubleshoot the firewall:**
   The host is reachable via ICMP, the application is healthy on loopback, and it communicates with MariaDB — so why did external `curl` fail? The application host is running the corporate security baseline firewall posture inherited from `corp-base`, which opens SSH only. Port 8080 has not yet been exposed by the operator.

5. **Expose the port:**
   Run the operational exposure playbook to open port 8080:
   ```bash
   # Working directory: ansible/
   ansible-playbook playbooks/expose_app.yml
   ```

6. **Test again from your workstation:**
   Now external network traffic reaches the application:
   ```bash
   curl -s http://app01:8080/health | jq .
   curl -s http://app01:8080/ | jq .
   ```

## Documentation

- [Architecture](docs/architecture.md) — the bake-time/runtime boundary, image layering, and repository layout.
- [PSK lifecycle](docs/psk-lifecycle.md) — first-contact key rotation, build ordering, and the no-resurrection guarantee.
- [Day-2 operations](docs/day2-operations.md) — how changes route to images or Ansible, and what survives upgrades.
- [Application design](docs/application-design.md) — the corporate Python standard, deploy/release/proof-of-life, and firewall posture.
- [AAP compatibility](docs/aap-compatibility.md) — running the same playbooks under Ansible Automation Platform.
- [Troubleshooting](docs/troubleshooting.md) — common failure modes and deliberate security non-additions.
- [Requirements](docs/project/requirements.md) — the canonical normative requirements and acceptance criteria for this pattern. Gate IDs (G1, G4, G9, …) quoted in comments and docs are defined there.

## Naming convention

Playbook files are named with the `.yml` form (`bootstrap.yml`, `database.yml`, `app_deploy.yml`, `app_release.yml`, `proof_of_life.yml`, `expose_app.yml`); roles are named by their bare stem (`mariadb_instance`, `app_deploy`, `app_release`, `app_proof_of_life`, `bootstrap_key_rotation`).
