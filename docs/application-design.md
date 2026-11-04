# Application Design

The corporate Python runtime standard, the application payload's deploy/release lifecycle, and the firewall posture that separates working from reachable.

## Corporate Python standard: one runtime, few choices

The application image is a **reusable corporate Python runtime baseline**. It bakes, in one place,
the stack every app host inherits:

- **Core runtime:** `python3`, pip, virtualenv, ca-certificates, tzdata.
- **Helpers & packaging:** git, tar, gzip, unzip, setuptools, wheel.
- **Corporate DB drivers:** `python3-PyMySQL` (MariaDB/MySQL) and `python3-psycopg2` (PostgreSQL)
  — two drivers on purpose, signalling a centrally-standardized runtime rather than per-app driver
  choices.
- **Corporate data-abstraction layer:** SQLAlchemy, Alembic, Pydantic.

App teams make as few choices as possible: they inherit the whole stack through a venv built with
`--system-site-packages` and declare only *app-specific* extras in `app/corp-app/requirements.txt`,
always pinned. There is one source of truth for shared Python infrastructure (the image) and one
small place for per-app decisions (the extras file). Changing the corporate stack is an image
rebuild + `bootc upgrade`, governed by Middleware — not an ad-hoc per-host pip install.

## Standard deployment target: `/opt/corp-app`

Applications deploy to `/opt/corp-app`, with the virtualenv at `/opt/corp-app/venv`, config at
`/etc/corp-app/config.ini`, and the unit at `/etc/systemd/system/corp-app.service`.

**How bootc handles `/opt` cleanly:** In bootc the root filesystem (`/`) is mounted read-only.
Rather than modifying the system-level `/opt` root directory, the application runtime image
follows the documented bootc pattern ("symlink just the parts of `/opt` needed into `/var`"):
it creates `/var/opt/corp-app` on persistent storage and links `/opt/corp-app -> /var/opt/corp-app`.
This gives Ansible a writable, persistent deployment target on `/var` that survives upgrades and
rollbacks (just like `/var/lib/mysql`) while preserving standard FHS path expectations for
systemd and operators without any root filesystem workarounds.

## Deploy vs. release vs. proof-of-life

Deployment is split from demonstration on purpose:

- **`app_deploy.yml` (app play 1, "deploy"):** lay the payload down — install to `/opt/corp-app`,
  build the venv, install pinned extras, render config and the unit. Nothing is started; no schema;
  no port opened.
- **`app_release.yml` (app play 2, "release/go-live"):** run the app's Alembic migrations (schema +
  `ansible-bootstrap` seed), enable and start the service, and confirm liveness with a single
  **loopback readiness gate** (`/health`). No functional exercises here.
- **`proof_of_life.yml` (optional, user-run):** real functional verification **on the host over
  loopback** (health, seed visibility, a write, and a malformed-input rejection). This is where
  "does it actually work?" is answered on the host, and it is never part of `site.yml`.
- **`expose_app.yml` (optional, operator action):** the deliberate firewall-exposure step that
  opens the application port for external access. It is a separate operator action, never part of
  `site.yml`.

This mirrors a real CI/CD deploy → release → smoke-test flow, and keeps write-probes and other
non-idempotent demonstration side effects out of the convergent deployment path.

## Database privilege scope: the app owns its schema

The DB foundation role (the `mariadb_instance` role, applied by the `database.yml` playbook) creates the application database and an account
scoped to **that database only**, granting it the DDL (CREATE/ALTER/DROP) its migrations need plus
the usual DML. It creates **no** tables and **no** seed data. The app team then owns its schema
end to end via Alembic (run in `app_release.yml`).

**This is a deliberate, flagged simplification — read it as a lesson, not a recommendation.** This
repo is an *example*, not a template to clone into production. A single account means the
long-running, network-exposed `corp-app` service holds a credential that can `DROP`/`ALTER` its own
schema — even though the service itself only ever runs `SELECT` and `INSERT`. That gap between *what
the account is granted* and *what the running service actually uses* is the least-privilege lesson,
and it is called out here (and in the role's own task comments) precisely so it is never copied
silently.

**The production posture** separates two credentials on the same database:

| Credential | Privileges | Used by | Lifetime |
|------------|-----------|---------|----------|
| Migration / schema-owner | DDL + DML | Alembic, at release time only | Ideally ephemeral — passed at `app_release.yml` run time, never persisted on the host |
| Runtime | Least privilege (here, `SELECT, INSERT`) | the running `corp-app` service | Long-lived, in `/etc/corp-app/config.ini` |

That split enforces least privilege on the exposed surface, separates a privileged, deliberate
operation (schema change) from steady-state data access, and lets the two credentials rotate
independently. It is intentionally **not** implemented in this example — the extra credential, grant,
and Alembic connection path are off-thesis and would compete with the bootc/Ansible lesson this repo
exists to teach — but it is the right answer for a real deployment.

## Firewall architecture: bake-time open narrowed at runtime

The database image ships with the MySQL service open in the firewall:

```
# Containerfile — bake-time posture
RUN firewall-offline-cmd --add-service=mysql
```

**Naming note:** MariaDB is a drop-in MySQL replacement, so upstream defaults keep
MySQL-compatible names — datadir `/var/lib/mysql`, socket `mysql.sock`, firewalld
service `mysql`, and config section `[mysqld]`. Only names this project controls — the
`mariadb_instance` role and the `ansible.mariadb` collection in `requirements.yml` —
say MariaDB; the MySQL-compatible names are not typos.

This broad open is intentional for test environments: any host can reach the database
port without any runtime configuration. The `mariadb_instance` Ansible role narrows it at
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

## The application port: runtime configuration vs. derived image build

The central design question returns: **Does this belong in runtime configuration or the derived image build?**

- **Database workload (`corp-db`):** Every host running this image is a MariaDB server listening on port 3306. Because the workload port is identical across all database hosts, opening the MySQL service in firewalld belongs in the **derived image build** (`firewall-offline-cmd --add-service=mysql`).
- **Application runtime baseline (`corp-app`):** Here the answer is the exact opposite: **runtime configuration, because different applications will expose different ports.** One app might listen on port 8080, another on 5000, 8443, or 9000 — and background worker services might listen on no network port at all! A reusable application server runtime image *cannot and must not bake a specific application port open*.

Consequently, the application host's initial firewall posture is **inherited directly from the corporate base image (`corp-base`)**, which enables firewalld and strictly opens SSH only. Neither `app_deploy` nor `app_release` touches the firewall.

This creates the deliberate **G19 teaching moment**:
1. After release, the service is running and healthy on loopback (`127.0.0.1:8080/health`), but external traffic is rejected because the host is enforcing the inherited corporate baseline `public` zone where no application port is open.
2. The operator runs `playbooks/expose_app.yml`, which explicitly opens the port matching `{{ app_port }}` for *this specific application*.
3. The lesson is twofold:
   - **"Working" and "reachable" are different states.** A service can be healthy on loopback while every external client is correctly refused.
   - **Port exposure is an application-specific runtime decision**, owned by the deployment configuration, not baked into a shared runtime baseline.

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

## Application service: instance-owned unit, started at release

Unlike `mariadb.service` (a corporate standard baked into the database image), `corp-app.service`
is **not baked into any image**. The application runtime image carries no workload unit at all —
it is a pure Python runtime baseline. The unit is an *instance* artifact: `app_deploy` renders it
from a template to `/etc/systemd/system/corp-app.service` alongside the payload and config, and
`app_release` enables and starts it. The ordering reason is the same as before — the service
cannot run until its payload, config, and schema exist — but the mechanism is now "Ansible owns
the unit," not "baked but not enabled":

1. `app_deploy.yml` installs the payload to `/opt/corp-app`, builds the venv, writes
   `/etc/corp-app/config.ini`, and renders the unit — but does **not** start it.
2. `app_release.yml` runs the Alembic migration (schema + seed), then enables and starts it:

```yaml
- ansible.builtin.systemd:
    name: corp-app
    state: started
    enabled: true
    daemon_reload: true
```

Because the unit lives in `/etc/systemd/system/` (instance-owned, machine-local), there is no
`/usr/lib` image-owned unit to override; operators edit it directly or via `systemctl edit
corp-app`. (Contrast the database, whose corporate unit drop-in *is* image-owned under `/usr/lib`.)

## Application endpoints: the full CRUD demonstration

`corp-app` (a SQLAlchemy + Pydantic service) implements three endpoints that prove the complete
bootstrap pattern end to end. The application **does not create schema** — its own **Alembic
migration**, run by `app_release.yml`, creates the `service_records` table and seeds the
`ansible-bootstrap` row before the service starts. (Schema is app-team territory; the DBA role
provides only the empty database and an account with DDL on it.)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /health` | GET | Database connectivity probe + row count. Returns `{"database": "connected", "count": N, "detail": "ok"}` on success, `{"database": "error", ...}` with HTTP 503 on failure. This is the endpoint the release **readiness gate** retries against (over loopback). |
| `GET /` | GET | Service metadata + record listing. Returns a JSON object with `service`, `status`, `database` info, total `count`, and a `records` array of the 50 most-recent rows (ordered by `id` DESC). The `ansible-bootstrap` seed row (created by the app's Alembic migration) is visible here — proving the app-owned bootstrap reached the data layer and is readable on the host. |
| `POST /record` | POST | Insert a new record. Accepts `{"message": "..."}` (validated with Pydantic). Returns HTTP 201 with the inserted row (`id`, `message`, `created_at`). Returns 400 for missing, empty, or non-string `message`. The optional `app_proof_of_life` role uses this endpoint to verify, on the host over loopback, that the application account has write access to the database. |

**Input validation contract for `POST /record`:**

```bash
# Valid — returns 201
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"message": "my record"}' \
     http://127.0.0.1:8080/record

# Missing message field — returns 400
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{}' http://127.0.0.1:8080/record

# Empty message — returns 400
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"message": ""}' http://127.0.0.1:8080/record

# Invalid JSON — returns 400
curl -s -X POST -H 'Content-Type: application/json' \
     -d 'not-json' http://127.0.0.1:8080/record
```

All error responses carry a JSON body with `error` and `detail` strings.
No response may return a non-JSON body or an empty body.

**How `systemctl enable` works during image builds:** Administrators may wonder how
`systemctl enable sshd` (in the base image) or `systemctl enable mariadb` (in the database
image) functions inside a Containerfile when systemd is not running as PID 1. In systemd,
enabling a unit is simply a filesystem operation: it creates symlinks under
`/etc/systemd/system/*.wants/`. Because bootc seeds `/etc` content from the container image
during initial deployment, those enablement symlinks are created at build time and take effect
on first boot without requiring a running systemd daemon during the build.

**Override mechanism:** The `corp-app` unit is instance-owned at
`/etc/systemd/system/corp-app.service` (rendered by `app_deploy`), so operators tune it either by
`systemctl edit corp-app` drop-ins or by re-templating via Ansible. Unlike the image-owned
`mariadb` unit (where `systemctl edit` is the *only* non-destructive path because the base unit is
immutable `/usr/lib` content), the `corp-app` unit is a normal machine-local file. Re-running
`app_deploy.yml` re-asserts the templated unit, so ad-hoc edits that must persist belong in the
template or a drop-in, not in hand-edits to the rendered file.

**Systemd sandboxing directives in `corp-app.service`:** The Ansible-rendered unit carries a
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
