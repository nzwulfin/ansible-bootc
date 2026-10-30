#!/usr/bin/env bash
# scripts/setup-demo-host.sh
#
# Sets up a RHEL 10 host as the Ansible control node and KVM hypervisor for
# the ansible-bootc demo. Run as a normal user — sudo is used only for the
# operations that actually require it (package install, service management,
# group membership).
#
# Usage:
#   bash scripts/setup-demo-host.sh           # first run
#   bash scripts/setup-demo-host.sh --replace # tear down existing keys and VMs and rebuild

set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: Run as a normal user, not root. The script uses sudo internally." >&2
    exit 1
fi

REPLACE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --replace) REPLACE=true ;;
        *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

IMG_DB=corp-database
IMG_APP=corp-application
DB_NAME="${IMG_DB}-image.qcow2"
APP_NAME="${IMG_APP}-image.qcow2"
LIBVIRT_IMAGES=/var/lib/libvirt/images
DB_IMAGE="${LIBVIRT_IMAGES}/${DB_NAME}"
APP_IMAGE="${LIBVIRT_IMAGES}/${APP_NAME}"
VAULT_FILE="${REPO_ROOT}/ansible/inventory/group_vars/all/vault.yml"
VAULT_PASS_FILE="${REPO_ROOT}/ansible/keys/.vault_pass"

# ---------------------------------------------------------------------------
# Packages — dnf is idempotent; already-installed packages are skipped.
# ---------------------------------------------------------------------------
PACKAGES=(
    ansible-core
    python3
    git
    openssh-clients
    podman
    image-builder
    qemu-kvm
    libvirt
    libvirt-client
    libvirt-nss
    virt-install
)

echo "==> Installing packages..."
sudo dnf install -y "${PACKAGES[@]}"

# ---------------------------------------------------------------------------
# libvirt NSS — enables hostname resolution for VMs by name
# ---------------------------------------------------------------------------
if grep -q '\blibvirt\b' /etc/nsswitch.conf; then
    echo "==> libvirt NSS already configured, skipping."
else
    echo "==> Configuring libvirt NSS..."
    sudo sed -i '/^hosts:/ s/\bfiles\b/files libvirt/' /etc/nsswitch.conf
fi

# ---------------------------------------------------------------------------
# libvirtd — skip if already active
# ---------------------------------------------------------------------------
if systemctl is-active --quiet libvirtd; then
    echo "==> libvirtd already running, skipping."
else
    echo "==> Enabling and starting libvirtd..."
    sudo systemctl enable --now libvirtd
fi

# ---------------------------------------------------------------------------
# libvirt group — skip if already a member
# ---------------------------------------------------------------------------
if id -Gn | tr ' ' '\n' | grep -qx libvirt; then
    echo "==> ${USER} already in libvirt group, skipping."
else
    echo "==> Adding ${USER} to the libvirt group..."
    sudo usermod -aG libvirt "$USER"
    echo "    Log out and back in (or run 'newgrp libvirt') for group membership to take effect."
fi

# ---------------------------------------------------------------------------
# Ansible collections — already at required version is a no-op
# ---------------------------------------------------------------------------
echo "==> Installing Ansible collections..."
ansible-galaxy collection install -r "${REPO_ROOT}/ansible/requirements.yml"

# ---------------------------------------------------------------------------
# KVM support check
# ---------------------------------------------------------------------------
if grep -qE 'vmx|svm' /proc/cpuinfo; then
    echo "==> KVM hardware support detected."
else
    echo "==> WARNING: No vmx/svm in /proc/cpuinfo."
    echo "    If this host is itself a VM, enable nested virtualization on the outer hypervisor."
fi

# ---------------------------------------------------------------------------
# SSH key pairs
# --replace removes existing keys so generate-demo-keys.sh can run fresh.
# Without --replace the script exits early if keys already exist.
# ---------------------------------------------------------------------------
echo ""
if [[ "$REPLACE" == true ]]; then
    echo "==> --replace: removing existing SSH key pairs..."
    rm -f "${REPO_ROOT}/ansible/keys/id_bootstrap" \
          "${REPO_ROOT}/ansible/keys/id_bootstrap.pub" \
          "${REPO_ROOT}/ansible/keys/id_live" \
          "${REPO_ROOT}/ansible/keys/id_live.pub"
fi
echo "==> Generating SSH key pairs..."
bash "${SCRIPT_DIR}/generate-demo-keys.sh"

# ---------------------------------------------------------------------------
# Vault password and credentials
# ---------------------------------------------------------------------------
echo ""
if [[ -f "${VAULT_PASS_FILE}" ]]; then
    echo "==> Vault password file already exists, skipping."
else
    echo "==> Generating vault password..."
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "${VAULT_PASS_FILE}"
    chmod 0600 "${VAULT_PASS_FILE}"
fi

if grep -q '^\$ANSIBLE_VAULT' "${VAULT_FILE}"; then
    echo "==> Vault file already encrypted, skipping."
else
    echo "==> Setting app_db_password and encrypting vault..."
    APP_DB_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    sed -i "s/REPLACE_ME_WITH_A_STRONG_PASSWORD/${APP_DB_PASS}/" "${VAULT_FILE}"
    ansible-vault encrypt --vault-password-file "${VAULT_PASS_FILE}" "${VAULT_FILE}"
fi

# ---------------------------------------------------------------------------
# Build bootc container images
# ---------------------------------------------------------------------------
echo ""
echo "==> Building bootc container images..."
podman build -t corp-base:latest      "${REPO_ROOT}/images/base/"
podman build -t "${IMG_DB}:latest"    "${REPO_ROOT}/images/database/"
podman build -t "${IMG_APP}:latest"   "${REPO_ROOT}/images/application/"

# ---------------------------------------------------------------------------
# Convert to qcow2 disk images
# image-builder reads from root's container storage, so copy images from
# the user's rootless podman storage first.
# ---------------------------------------------------------------------------
echo ""
echo "==> Copying images to root container storage..."
podman image scp "$(id -un)@localhost::localhost/${IMG_APP}:latest"
podman image scp "$(id -un)@localhost::localhost/${IMG_DB}:latest"

echo "==> Converting bootc images to qcow2..."
sudo image-builder build qcow2 --bootc-ref "localhost/${IMG_APP}:latest" --output-name "${APP_NAME}" --output-dir "${LIBVIRT_IMAGES}"
sudo image-builder build qcow2 --bootc-ref "localhost/${IMG_DB}:latest" --output-name "${DB_NAME}" --output-dir "${LIBVIRT_IMAGES}"

# ---------------------------------------------------------------------------
# Provision target VMs
# --replace destroys and undefines existing VMs before recreating them.
# Without --replace existing VMs are left in place.
# ---------------------------------------------------------------------------
echo ""
for VM in db01 app01; do
    if virsh --connect qemu:///system list --all --name | grep -qx "${VM}"; then
        if [[ "$REPLACE" == true ]]; then
            echo "==> --replace: removing existing VM ${VM}..."
            virsh --connect qemu:///system destroy "${VM}" 2>/dev/null || true
            virsh --connect qemu:///system undefine "${VM}"
        else
            echo "==> VM ${VM} already exists, skipping."
            continue
        fi
    fi

    if [[ "$VM" == db01 ]]; then
        IMAGE="$DB_IMAGE"
    else
        IMAGE="$APP_IMAGE"
    fi

    echo "==> Provisioning VM ${VM}..."
    virt-install \
        --connect qemu:///system \
        --name "${VM}" \
        --memory 2048 \
        --vcpus 2 \
        --disk "${IMAGE}",format=qcow2 \
        --import \
        --osinfo centos-stream10 \
        --noautoconsole
done

# ---------------------------------------------------------------------------
# Retrieve VM IPs from DHCP leases and update group_vars
# ---------------------------------------------------------------------------
get_vm_ip() {
    local vm=$1
    local mac
    mac=$(virsh --connect qemu:///system domiflist "${vm}" | awk 'NR>2 && NF {print $5}' | head -1)
    local ip=""
    local attempts=0
    echo "==> Waiting for ${vm} to obtain a DHCP lease..." >&2
    while [[ -z "$ip" && $attempts -lt 30 ]]; do
        ip=$(virsh --connect qemu:///system net-dhcp-leases default \
            | awk -v mac="$mac" '$3 == mac {split($5, a, "/"); print a[1]}')
        [[ -z "$ip" ]] && sleep 5
        ((attempts++))
    done
    if [[ -z "$ip" ]]; then
        echo "ERROR: No DHCP lease found for ${vm} after 150s." >&2
        exit 1
    fi
    echo "$ip"
}

echo ""
DB_IP=$(get_vm_ip db01)
APP_IP=$(get_vm_ip app01)

echo "==> db01 IP: ${DB_IP}"
echo "==> app01 IP: ${APP_IP}"

echo "==> Updating group_vars with VM IPs..."
sed -i "s/^db_host:.*/db_host: ${DB_IP}/" \
    "${REPO_ROOT}/ansible/inventory/group_vars/application/vars.yml"
sed -i "s/^app_host_address:.*/app_host_address: ${APP_IP}/" \
    "${REPO_ROOT}/ansible/inventory/group_vars/database/vars.yml"

echo "==> Updating hosts.yml with VM IPs..."
sed -i "s/^\(\s*\)ansible_host:.*\(# Must match db01\)/\1ansible_host: ${DB_IP}   \2/" \
    "${REPO_ROOT}/ansible/inventory/hosts.yml"
sed -i "s/^\(\s*\)ansible_host:.*\(# Must match app01\)/\1ansible_host: ${APP_IP}   \2/" \
    "${REPO_ROOT}/ansible/inventory/hosts.yml"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "=== Setup complete. Run Ansible: ==="
echo ""
echo "    cd ansible && ansible-playbook site.yml"
