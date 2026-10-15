#!/usr/bin/env bash
# Backup everything da-plus install/apply/uninstall can touch, before any real test.
# Read-only for the system: it only copies files and records state. It never deletes or changes anything.
#
# Usage (as root on the server):
#   bash backup-da-plus.sh
#
# Output: /root/da-plus-backups/<timestamp>/ containing backup.tgz, SHA256SUMS, state.txt, RESTORE.txt

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "error: run as root (needed to read /etc/systemd and DirectAdmin files)" >&2
    exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="/root/da-plus-backups/${STAMP}"
mkdir -p "${DEST}"
chmod 700 "${DEST}"

# Paths that install, apply and uninstall read or write.
CANDIDATES=(
    /etc/systemd/system
    /usr/local/directadmin/scripts/custom
    /usr/local/directadmin/data/users
    /etc/da-plus
    /var/lib/da-plus
    /var/log/da-plus
)

PATHS=()
for p in "${CANDIDATES[@]}"; do
    if [ -e "$p" ]; then
        PATHS+=("$p")
    else
        echo "skip (not present): $p"
    fi
done

if [ "${#PATHS[@]}" -eq 0 ]; then
    echo "error: none of the expected paths exist; nothing to back up" >&2
    exit 1
fi

echo "backing up:"
printf '  %s\n' "${PATHS[@]}"

# Archive with numeric owners so restore keeps the same uid/gid.
tar --numeric-owner -czf "${DEST}/backup.tgz" "${PATHS[@]}"

# Verify the archive can be read and count its entries.
ENTRIES="$(tar -tzf "${DEST}/backup.tgz" | wc -l)"
if [ "${ENTRIES}" -eq 0 ]; then
    echo "error: archive is empty; do not continue" >&2
    exit 1
fi

# Record the current state so you can compare after a test.
{
    echo "== date"; date
    echo "== uname"; uname -a
    echo "== systemd version"; systemctl --version | head -1
    echo "== da-plus unit files"; systemctl list-unit-files 'da-plus*' 2>&1 || true
    echo "== timer"; systemctl is-enabled da-plus-reconcile.timer 2>&1 || true; systemctl is-active da-plus-reconcile.timer 2>&1 || true
    echo "== user@0 delegation"; systemctl show user@0.service -p Delegate -p DelegateControllers 2>&1 || true
    echo "== hooks"; ls -la /usr/local/directadmin/scripts/custom/ 2>&1 | grep -i 'user_' || true
    echo "== directadmin service"; systemctl is-active directadmin 2>&1 || true
} > "${DEST}/state.txt"

# Checksums for later verification.
( cd "${DEST}" && sha256sum backup.tgz state.txt > SHA256SUMS )

cat > "${DEST}/RESTORE.txt" <<EOF
To restore the files from this backup (run as root):

  tar xzf ${DEST}/backup.tgz -C /
  systemctl daemon-reload

Check first with:  sha256sum -c ${DEST}/SHA256SUMS
List contents with: tar -tzf ${DEST}/backup.tgz
EOF

# Verify checksums from inside the backup folder (SHA256SUMS holds relative names).
# If this fails, the script exits non-zero and does NOT print "backup complete".
if ! ( cd "${DEST}" && sha256sum -c SHA256SUMS ); then
    echo "error: checksum verification FAILED for ${DEST}; do not rely on this backup" >&2
    exit 1
fi

echo
echo "backup complete: ${DEST}"
echo "  archive entries: ${ENTRIES}"
echo "restore instructions: ${DEST}/RESTORE.txt"
