# AAP Compatibility

How the same playbooks run unchanged under Ansible Automation Platform, including the credential and job-template split.

The playbooks run **unchanged** under Ansible Automation Platform (AAP). The design
decisions that make this work are listed here.

## The one-credential-per-job constraint

An AAP job template carries exactly one machine credential. This matters because
`bootstrap.yml` uses two different SSH private keys: the bootstrap PSK for Plays 1
and 2, and the live key for Play 3. Locally, the playbook handles the credential switch
via per-play `ansible_ssh_private_key_file` overrides. Under AAP, the job template
level is the credential boundary.

## Job template split

Split the deployment playbooks across two AAP job templates, with proof-of-life and firewall exposure as optional additional jobs:

| AAP Job | Playbook(s) | Machine credential |
|---------|-------------|--------------------|
| **Bootstrap** | `playbooks/bootstrap.yml` | Bootstrap credential (`ansible/keys/id_bootstrap`) |
| **Workload** | `playbooks/database.yml` + `playbooks/app_deploy.yml` + `playbooks/app_release.yml` | Live credential (`ansible/keys/id_live`) |
| **Proof-of-life (optional)** | `playbooks/proof_of_life.yml` | Live credential (`ansible/keys/id_live`) |
| **Firewall expose (optional)** | `playbooks/expose_app.yml` | Live credential (`ansible/keys/id_live`) |

**Bootstrap job:** Uses the bootstrap private key. Plays 1 and 2 probe and rotate the
PSK. Play 3 (live-key verify) uses the live key — but under AAP's single-credential
constraint only the bootstrap key is available in this job. For that reason, configure the
Bootstrap Job Template with `--skip-tags live_key_verify` (Play 3 carries this tag).
**Play 3's live-key verification is then handled by the workload job's first connection** rather than
inside the bootstrap job. The bootstrap job's responsibility ends when the PSK is
successfully rotated out in Play 2.

**Workload job:** Uses the live private key. Runs `database.yml`, then `app_deploy.yml`,
then `app_release.yml`. No PSK is referenced in these playbooks. The workload job's first
connection implicitly proves live-key connectivity. The live credential is sufficient for all
workload automation.

**Proof-of-life job (optional):** A separate job a user triggers to demonstrate the system.
It is never chained into the workload job — it is verification and a teaching exercise, not
deployment. External firewall exposure is the separate optional `expose_app.yml` job.

> **The two-job split is mandatory under AAP.** Do not attempt to run `site.yml` as a
> single AAP job with the bootstrap credential. Once Play 2 rotates out the PSK, all
> subsequent SSH connections (including Play 3 and the workload plays) require the live
> key — which the bootstrap credential does not carry. The single-job approach fails
> after key rotation.

## Vault files → AAP Custom Credentials or Survey variables

Under AAP, vault-encrypted files are replaced by AAP Custom Credentials or Survey
variables. **Variable names remain identical**; roles are unchanged.

| Vault file | Variable | AAP replacement |
|------------|----------|----------------|
| `ansible/inventory/group_vars/all/vault.yml` | `app_db_password` | AAP Custom Credential (type: Secret Text) injected as an extra var, or a Survey variable — named `app_db_password` |

Because the variable names are unchanged, the `mariadb_instance` role (which references
`app_db_password` in its `mariadb_user` task) and the `app_deploy` role (which renders
it into `config.ini.j2`, and which `app_release` uses as the Alembic migration credential) work
without modification.

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

## Running site.yml standalone vs. under AAP

```
Standalone:   cd ansible/ && ansible-playbook site.yml
              cd ansible/ && ansible-playbook playbooks/proof_of_life.yml   (verify on-host)
              cd ansible/ && ansible-playbook playbooks/expose_app.yml      (open firewall)
AAP Job 1:    ansible-playbook playbooks/bootstrap.yml   (bootstrap credential, --skip-tags live_key_verify)
AAP Job 2:    ansible-playbook playbooks/database.yml    (live credential)
              ansible-playbook playbooks/app_deploy.yml  (live credential, same job)
              ansible-playbook playbooks/app_release.yml (live credential, same job)
AAP Job 3:    ansible-playbook playbooks/proof_of_life.yml (live credential, optional)
AAP Job 4:    ansible-playbook playbooks/expose_app.yml    (live credential, optional)
```

No playbook files are changed between standalone and AAP operation. The only difference
is which credential the job carries and how secrets are injected.
