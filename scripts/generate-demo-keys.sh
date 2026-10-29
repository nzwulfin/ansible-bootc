#!/usr/bin/env bash
# scripts/generate-demo-keys.sh — Project-wide bootstrap key generation script.
#
# Generates the bootstrap (PSK) and live SSH key pairs, then writes the PSK
# public key — with mandatory restrictive SSH options — into the base image
# build context at images/base/rootfs/var/home/ansible/.ssh/authorized_keys.
#
# Must be run before any image build. After running this script the base image
# MUST be rebuilt so the new PSK is baked in.
#
# Placement rationale (G13): This script lives under scripts/ at the repo root
# because it crosses the images/ansible boundary — it writes into
# images/base/rootfs/ (image build context) AND generates keys consumed by
# ansible/keys/ (Ansible runtime). It must not be placed inside either subtree.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

KEYS_DIR="${REPO_ROOT}/ansible/keys"
SSH_DIR="${REPO_ROOT}/images/base/rootfs/var/home/ansible/.ssh"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"

# ---------------------------------------------------------------------------
# Guard: abort if keys already exist.
#
# Regenerating the bootstrap key invalidates ALL images built with the old
# key. Hosts that were provisioned from those images cannot be onboarded using
# the new PSK — they must be decommissioned and reprovisioned from the new
# image. Silently overwriting keys would cause an outage that is hard to
# diagnose. Abort loudly instead.
# ---------------------------------------------------------------------------
if [[ -f "${KEYS_DIR}/id_bootstrap" ]]; then
    echo "ERROR: Keys already exist in ${KEYS_DIR}." >&2
    echo "" >&2
    echo "       Keys already exist. Regeneration would invalidate all images built" >&2
    echo "       with the current bootstrap key. Hosts provisioned from those images" >&2
    echo "       cannot be onboarded with a new PSK and must be decommissioned and" >&2
    echo "       reprovisioned from a freshly built image." >&2
    echo "" >&2
    echo "       To regenerate intentionally:" >&2
    echo "         rm ${KEYS_DIR}/id_bootstrap ${KEYS_DIR}/id_bootstrap.pub" >&2
    echo "         rm ${KEYS_DIR}/id_live ${KEYS_DIR}/id_live.pub" >&2
    echo "         $0" >&2
    echo "" >&2
    echo "       After regeneration, rebuild the base image before provisioning." >&2
    exit 1
fi

# Ensure the target SSH directory exists in the base image build context.
mkdir -p "${SSH_DIR}"

echo "Generating Ed25519 key pairs in ${KEYS_DIR}/ ..."
echo ""

# ---------------------------------------------------------------------------
# Bootstrap key (PSK)
#
# Installed into the base image via authorized_keys (with restrictive options).
# Used exclusively for first-contact onboarding. The bootstrap_key_rotation
# Ansible role removes this key after installing the live key (G3).
# ---------------------------------------------------------------------------
ssh-keygen -t ed25519 -N "" \
    -C "ansible-bootc-bootstrap" \
    -f "${KEYS_DIR}/id_bootstrap"

# ---------------------------------------------------------------------------
# Live key
#
# The long-term management key. Installed during onboarding by the
# bootstrap_key_rotation role and used for all subsequent Ansible runs.
# ---------------------------------------------------------------------------
ssh-keygen -t ed25519 -N "" \
    -C "ansible-bootc-live" \
    -f "${KEYS_DIR}/id_live"

# Set private key permissions to 0600; SSH refuses keys with broader permissions.
chmod 0600 "${KEYS_DIR}/id_bootstrap"
chmod 0600 "${KEYS_DIR}/id_live"

# ---------------------------------------------------------------------------
# Write the PSK public key into the base image build context.
#
# The authorized_keys line MUST begin with the four restrictive SSH option
# tokens. These restrict what the shared provisioning key can do:
#
#   no-pty              — no interactive terminal; Ansible's non-interactive
#                         command execution does not require one
#   no-X11-forwarding   — no X11 session forwarding
#   no-agent-forwarding — no SSH agent forwarding
#   no-user-rc          — do not execute ~/.ssh/rc on login
#
# Use printf '%s\n' (not echo -e) to write the line without interpreting any
# escape sequences that might appear in the key material.
# ---------------------------------------------------------------------------
BOOTSTRAP_PUB="$(cat "${KEYS_DIR}/id_bootstrap.pub")"
printf '%s\n' "no-pty,no-X11-forwarding,no-agent-forwarding,no-user-rc ${BOOTSTRAP_PUB}" \
    > "${AUTHORIZED_KEYS}"

# Set mode 0600 on authorized_keys. The base image Containerfile also enforces
# ownership (5000:5000) and mode (0600) after COPY rootfs/ /, but setting the
# mode here ensures the file is correctly protected in the build context too.
chmod 0600 "${AUTHORIZED_KEYS}"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Key generation complete ==="
echo ""
echo "Generated files:"
printf "  %-52s  %s\n" "${KEYS_DIR}/id_bootstrap"     "(private — Ansible bootstrap/PSK key)"
printf "  %-52s  %s\n" "${KEYS_DIR}/id_bootstrap.pub" "(public  — baked into base image as PSK)"
printf "  %-52s  %s\n" "${KEYS_DIR}/id_live"          "(private — Ansible live management key)"
printf "  %-52s  %s\n" "${KEYS_DIR}/id_live.pub"      "(public  — installed by bootstrap_key_rotation role)"
echo ""
echo "PSK destination (base image build context):"
echo "  ${AUTHORIZED_KEYS}"
echo ""
echo "==========================================================="
echo "  WARNING: Rebuild the base image before provisioning any"
echo "           new hosts:"
echo ""
echo "    podman build -t corp-base:latest images/base/"
echo ""
echo "  Any host provisioned from an older image cannot be"
echo "  onboarded with this new bootstrap key."
echo "==========================================================="
