# Troubleshooting

Common failure modes and the security controls this pattern deliberately omits.

## Troubleshooting common failure modes

| Symptom | Root Cause | Resolution |
|---|---|---|
| **Play 1 in `bootstrap.yml` reports `unreachable=1` on fresh VM** | PSK mismatch or SSH perms: keys regenerated after base image was built, or file permissions on `authorized_keys` are not `0600 (5000:5000)` | Rebuild `corp-base` after running `scripts/generate-demo-keys.sh`. Verify `id_bootstrap.pub` matches the baked PSK line. Check `bootc status` on the VM console. |
| **`database.yml` fails with "app_db_password is still set to default placeholder"** | Vault file was encrypted without editing the placeholder password | Decrypt `ansible-vault decrypt inventory/group_vars/all/vault.yml`, set a strong password, and re-encrypt. |
| **`database.yml` times out waiting for MariaDB socket** | MariaDB failed first-boot self-initialization or SELinux issue | Inspect target console: `journalctl -u mariadb -b`. Verify `00-corporate.cnf` syntax and disk space on `/var/lib/mysql`. |
| **External `curl http://<app01>:8080/` times out or Connection Refused** | Normal post-release default-deny state (**G19 teaching moment**); loopback works but port is closed | Run `ansible-playbook playbooks/expose_app.yml` to open the application port (`app_port`/tcp) in firewalld permanently and immediately, or manually run `ansible -m ansible.posix.firewalld -a "port=8080/tcp state=enabled permanent=true immediate=true" -b app01`. |
| **`bootc upgrade` shows `/etc` conflict or local modifications** | Local hand-edits made to 3-way merge files (`/etc/my.cnf.d/`, `/etc/corp-app/`) conflicting with updated image | On conflict, bootc preserves the machine-local file and saves `.rpmnew`/`.rpmsave`. Reconcile with `bootc status` and re-run Ansible to re-converge. |

## Security non-additions: deliberate decisions

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
