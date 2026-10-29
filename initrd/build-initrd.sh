#!/bin/bash
# Build the guest initramfs used by the OLK-6.6 MPAM verification boot.
# Output: initrd/olk-initrd.gz  (override with $1)
set -e
cd "$(dirname "$0")"
OUT="${1:-olk-initrd.gz}"
[ -x rootfs/bin/busybox ] || { echo "ERROR: rootfs/bin/busybox missing"; exit 1; }
[ -x rootfs/init ]        || { echo "ERROR: rootfs/init missing"; exit 1; }
(cd rootfs && find . | cpio -o -H newc --quiet) | gzip -9 > "$OUT"
echo "built $OUT:"
zcat "$OUT" | cpio -t --quiet | sed 's/^/  /'
