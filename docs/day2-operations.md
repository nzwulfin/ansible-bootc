# Day-2 Operations

How operational changes route to image rebuilds or Ansible, and what state survives `bootc upgrade` and rollback.

## Day-2 operations: change routing workflow

Once hosts are provisioned and onboarded, operational changes route according to where
the configuration lives:

| Kind of change | Example | Day-2 Workflow |
|---|---|---|
| Platform standard | SSH hardening update, base admin tooling | Rebuild `corp-base` → rebuild derived workload images → trigger `bootc upgrade` on hosts |
| Workload standard | MariaDB resource limits, TuneD profile, app release | Rebuild workload image (`corp-db` / `corp-app`) → trigger `bootc upgrade` on hosts |
| Instance runtime state | DB password rotation, host IP change, app config | Update `ansible/inventory/` or vault → re-run Ansible playbooks |

**Applying image updates atomically:**

```bash
# On the target host (or orchestrated via an Ansible command/task):
bootc status                       # View currently booted deployment and staged update
bootc upgrade                      # Pull new image layer and stage next deployment
systemctl reboot                   # Atomically boot into the updated image

# If an unexpected issue occurs post-reboot:
bootc rollback                     # Atomically revert to the prior working deployment
```

## `bootc upgrade` safety

A `bootc upgrade` replaces `/usr` atomically and applies a 3-way merge on `/etc`.
The onboarding state survives cleanly:

| What | Zone | Behaviour on upgrade |
|------|------|----------------------|
| Live SSH key (`/var/home/ansible/.ssh/authorized_keys`) | `/var` | Never touched by upgrade |
| App payload + venv (`/opt/corp-app`, resolves under `/var/opt`) | `/var` | Never touched by upgrade; the Ansible-deployed payload persists |
| MariaDB data (`/var/lib/mysql`) | `/var` | Never touched by upgrade |
| Application config (`/etc/corp-app/config.ini`) | `/etc` | 3-way merge; local Ansible-written content survives |
| Application unit (`/etc/systemd/system/corp-app.service`) | `/etc` | Instance-owned; 3-way merge; Ansible-rendered content survives |
| MariaDB config (`/etc/my.cnf.d/00-corporate.cnf`) | `/etc` | 3-way merge; image version wins if no local change |
| Database unit drop-in / tuned profile (`/usr/lib/...`) | `/usr` | Replaced; corporate standard always applied |
| Management account (`systemd-sysusers`) | `/usr/lib/sysusers.d/` | Re-applied on every boot; drift-free |
| Home directory structure (`systemd-tmpfiles`) | `d` entries only | Directories re-created if absent; existing files never touched |

Note the symmetry: the app payload under `/opt` (→ `/var/opt`) survives upgrades by the *exact same*
install-only `/var` mechanism that preserves the MariaDB data and prevents the PSK from
resurrecting. A `bootc upgrade` that ships a new runtime baseline (new Python, new corporate stack)
does not disturb the deployed app; re-running `app_deploy.yml`/`app_release.yml` is how a new app
version or a venv rebuild is rolled out.

## Database host discovery: static variable design decision

The `db_host` variable in `ansible/inventory/group_vars/application/vars.yml` is a
static string set by the operator at inventory-configuration time (Step 4 of the README
quick start). This is a deliberate simplification for a reference pattern: the operator
knows both VM addresses when they populate the inventory, and a static variable is the most
transparent and debuggable mechanism.

Real-world options for making this dynamic:

| Option | When to use |
|--------|-------------|
| **Fact caching** | After a database play runs, the `ansible_default_ipv4.address` fact can be cached and referenced by the application play in the same run or a later run. Requires a fact cache backend (`jsonfile`, `redis`, etc.) in `ansible.cfg`. |
| **Dynamic inventory** | A dynamic inventory script or plugin (e.g., querying libvirt, a CMDB, or a cloud API) returns host addresses at runtime. The `hostvars['db01']['ansible_host']` reference then works without a static variable. |
| **Discovery task** | A dedicated pre-task or role queries the environment (DNS, a service registry, or the database group's `ansible_host`) and sets a fact used by later roles. Explicit and auditable. |

For this pattern, the static variable is correct: it matches the documented inventory
update step (Step 4 of the README quick start) and avoids introducing a caching or discovery
dependency that would obscure the core bake-time/runtime design.

## Static IP inventory assumption

The reference inventory (`ansible/inventory/hosts.yml`) uses static IP addresses for
`db01` and `app01`. This is a deliberate choice for the reference pattern:

- Static IPs match the libvirt VM provisioning context (Step 3 of the README quick start),
  where the operator assigns and knows both addresses.
- Static IPs make the inventory fully self-contained and auditable — no DNS dependency,
  no dynamic resolution, no external lookup at play time.
- The `app_host_address` and `db_host` variables that must match the inventory IPs are
  documented explicitly in Step 4 of the README quick start to prevent silent divergence.

In production, replace static addresses with DNS names or use a dynamic inventory
plugin. The playbooks and roles require no changes — only the inventory source changes.
