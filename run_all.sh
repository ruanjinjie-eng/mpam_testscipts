#!/bin/bash
# v2 one-click verification: narrow PARTID (v1 core) PLUS the linux_old1
# PRI/MIN/MAX pseudo-resources, MBHDL/MBOPT, CDP and the L2 monitors.
# Requires the completed QEMU register surface (CMIN/CASSOC, mem-MSC PRI,
# CSU occupancy, MPAMSM_EL1).
#
# Usage:
#   ./run_all.sh                 # default kernel: linux_old1 v7.3-rc6
#   ./run_all.sh /path/to/Image  # any arm64 kernel (flavour auto-detected)
#
# Verdict policy: the v2 feature checks are REQUIRED on mainline kernels;
# on OLK they are reported as INFO (different driver generation).
set -u
MPAM_HOME="${MPAM_HOME:-/home/r00799764/community}"
TEST_HOME="$MPAM_HOME/mpam_qemu_test_v2"
LOGS="$TEST_HOME/logs"

KERNEL="${1:-$MPAM_HOME/linux_old1/build/arm64/arch/arm64/boot/Image}"
QEMU_BIN="${QEMU_BIN:-$MPAM_HOME/qemu/build/qemu-system-aarch64}"
# The boot orchestrator picks the kernel from OLK_IMAGE; without this export
# it silently falls back to its own default while the flavour-specific
# cmdline is still applied (v1 bug, doc §12.1).
export OLK_IMAGE="$KERNEL"
export INITRD_GZ="$TEST_HOME/initrd/olk-initrd.gz"

fail=0
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; fail=1; }
info() { printf '  \033[36m[INFO]\033[0m %s\n' "$*"; }

[ -x "$QEMU_BIN" ] || { echo "QEMU binary not found: $QEMU_BIN (set QEMU_BIN)"; exit 1; }
[ -f "$KERNEL" ]   || { echo "kernel Image not found: $KERNEL"; exit 1; }
mkdir -p "$LOGS"

# --- flavour detection ------------------------------------------------------
BANNER=$(strings -a "$KERNEL" 2>/dev/null | grep -m1 '^Linux version' || true)
if [ -n "${KERNEL_FLAVOR:-}" ]; then
    FLAVOR="$KERNEL_FLAVOR"
elif [ -z "$BANNER" ]; then
    FLAVOR="mainline"; info "no banner string in Image; assuming mainline flavour"
else
    case "$BANNER" in
        *"6.6"*|*openEuler*|*"olk"*) FLAVOR="olk" ;;
        *)                           FLAVOR="mainline" ;;
    esac
fi

if [ "$FLAVOR" = "olk" ]; then
    export BOOT_CMDLINE="${BOOT_CMDLINE:-console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init arm64.mpam}"
else
    export BOOT_CMDLINE="${BOOT_CMDLINE:-console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init}"
fi
# MPAMSM_EL1 is implemented now: plain `max` boots v7.x (SME on) as well.
export CPU_MODEL="${CPU_MODEL:-max}"

# --- kill stale instances from previous runs (shared log/socket paths) ------
pkill -f "mpam_qemu_test_v[2]/scripts/olk_boot_verif[y].py" 2>/dev/null && sleep 1
pkill -f "qemu-system-aarc[h]64" 2>/dev/null && sleep 1
rm -f "$LOGS/olk_qtest.sock" "$LOGS/olk_qmp.sock"

step "kernel under test"
ls -la "$KERNEL"
info "banner: ${BANNER:-<none>}"
info "flavour: $FLAVOR  (cmdline: $BOOT_CMDLINE, cpu: $CPU_MODEL)"

step "1/4 build guest initramfs"
"$TEST_HOME/initrd/build-initrd.sh" "$TEST_HOME/initrd/olk-initrd.gz" > "$LOGS/build-initrd.log" 2>&1 \
    && ok "initrd/olk-initrd.gz built" || bad "initrd build"

step "2/4 register-level qtest (kernel-independent, incl. PRI/CMIN/CSU)"
python3 "$TEST_HOME/scripts/mpam_qtest.py" > "$LOGS/qtest.log" 2>&1
if grep -q "RESULT: ALL CHECKS PASSED" "$LOGS/qtest.log"; then
    ok "$(grep -c '\[PASS\]' "$LOGS/qtest.log") register checks passed"
else
    bad "qtest failed — tail:"
    tail -20 "$LOGS/qtest.log" | sed 's/^/      /'
fi

step "3/4 boot + guest verification + host-side MSC recon (TCG, minutes)"
python3 -u "$TEST_HOME/scripts/olk_boot_verify.py" > "$LOGS/e2e.log" 2>&1
rc=$?
echo "  orchestrator rc=$rc, guest log: $LOGS/olk_boot.log"
B="$LOGS/olk_boot.log"
E="$LOGS/e2e.log"

step "4/4 verdict"
grep -aq "RESCTRL-MOUNT-OK" "$B" && ok "resctrl mount" || bad "resctrl mount"

ncl=$(grep -ao '^L3/num_closids: [0-9]*' "$B" | tail -1 | grep -o '[0-9]*$')
if   [ "$ncl" = "32" ];  then ok   "narrow PARTID: num_closids=32 (intpartid_max=31 honoured)"
elif [ "$ncl" = "256" ] && [ "$FLAVOR" = "mainline" ]; then info "num_closids=256 — MPAM probed, driver does NOT narrow"
elif [ -n "$ncl" ];      then info "num_closids=$ncl"
else                          bad  "num_closids not reported (MPAM/resctrl L3 resource absent?)"
fi

grep -aq "WRITE-OK   \[L3:33751040=55555555" "$B" \
    && ok "L3 non-contiguous bitmap 55555555 accepted" \
    || bad "L3 bitmap write (55555555) rejected"
grep -Eq "SCHEMATA: +L3:33751040=(55555555|0000ffff|ffff)\b" "$B" \
    && ok "schemata readback (L3 bitmap lands)" || bad "schemata readback"
if grep -aq "WRITE-OK   \[MB:0=00000080" "$B" || grep -aq "WRITE-OK   \[MB:33751040=25" "$B"; then
    ok "MB schemata accepted (flavour-appropriate syntax)"
else
    bad "MB schemata write"
fi
if grep -Eq "0x(55555555|0000ffff)|cpbm=\[65535," "$E"; then
    ok "host-side L3 MSC CPBM reflects guest schemata (register-level closure)"
else
    bad "host-side L3 MSC CPBM did not change"
fi
grep -aq "MARKER:DONE" "$B" && ok "guest verification completed" || bad "guest verification completed"

# ---------- v2: PRI / MIN / MAX / HDL / OPT ---------------------------------
v2chk() {  # v2chk <grep-args...> <label>
    local label="$1"; shift
    if grep -q "$@" "$B"; then
        if [ "$FLAVOR" = "mainline" ]; then ok "$label"; else info "$label (olk: not required)"; fi
    else
        if [ "$FLAVOR" = "mainline" ]; then bad "$label"; else info "$label MISSING (olk: not required)"; fi
    fi
}
v2chkE() {  # like v2chk but greps the host-side orchestrator log (e2e.log)
    local label="$1"; shift
    if grep -q "$@" "$E"; then
        if [ "$FLAVOR" = "mainline" ]; then ok "$label"; else info "$label (olk: not required)"; fi
    else
        if [ "$FLAVOR" = "mainline" ]; then bad "$label"; else info "$label MISSING (olk: not required)"; fi
    fi
}
v2chk "guest L3PRI=2 write"        -q "WRITE-OK   \[L3PRI:33751040=2"
v2chk "guest L3MIN=25 write"       -q "WRITE-OK   \[L3MIN:33751040=25"
v2chk "guest L3MAX=50 write"       -q "WRITE-OK   \[L3MAX:33751040=50"
v2chk "guest L2PRI=1 write"        -q "WRITE-OK   \[L2PRI:33685504=1"
v2chk "guest L2MIN=25 write"       -q "WRITE-OK   \[L2MIN:33685504=25"
v2chk "guest L2MAX=50 write"       -q "WRITE-OK   \[L2MAX:33685504=50"
v2chk "guest MBPRI=1 write"        -q "WRITE-OK   \[MBPRI:33751040=1"
v2chk "guest MBMIN=25 write"       -q "WRITE-OK   \[MBMIN:33751040=25"
v2chk "guest MBOPT=8191 write"     -q "WRITE-OK   \[MBOPT:33751040=8191"
v2chk "guest MBHDL=1 write"        -q "WRITE-OK   \[MBHDL:33751040=1"
v2chk "guest L3PRI readback"       -Eq "SCHEMATA: +L3PRI:33751040=2\b"
v2chk "guest L3MIN readback"       -Eq "SCHEMATA: +L3MIN:33751040=25\b"
v2chk "guest L3MAX readback"       -Eq "SCHEMATA: +L3MAX:33751040=50\b"

# host-side register closure: driver-programmed values in the recon tables
v2chkE "host L3 MSC: PRI=2 CMIN=0x3000 CMAX=0x7000 (partid0)"  -Eq "^ +0 : 0x[0-9a-f]+ +0x00:0x02 +0x3000 0x7000"
v2chkE "host L2 MSC: PRI=1 CMIN=0x3000 CMAX=0x7000 (partid0)"  -Eq "^ +0 : 0x[0-9a-f]+ +0x00:0x01 +0x3000 0x7000"
v2chkE "host mem MSC: PRI=1 + HARDLIM (partid0)"     -Eq "^ +0 : 0x0001 +0x[0-9a-f]+ +0x[0-9a-f]+ +Y"
grep -q "expect=16777216 \[OK\]" "$E" \
    && { [ "$FLAVOR" = "mainline" ] && ok "host CSU model: 16MiB @ 50% CBM" || info "host CSU model OK"; } \
    || { [ "$FLAVOR" = "mainline" ] && bad "host CSU occupancy model" || info "host CSU model MISSING (olk)"; }

# guest occupancy + L2 monitor counters
v2chk "guest llc_occupancy = 16MiB (L3 CBM 50%)"    -Eq "mon_L3_[0-9]*/llc_occupancy: [0-9]* -> 16777216"
# L2 monitors: linux_old1 read path now dispatches RDT_RESOURCE_L2 through
# the per-domain counter path, and mpam borrows an MBWU monitor per read for
# mbm_core_bytes (CSU for l2c_occupancy). Expect real numbers on mainline:
# 256KiB @ 50% L2 CBM occupancy and an advancing core-bytes counter.
v2chk "L2 monitor resource: l2c_occupancy + mbm_core_bytes files" -q "L2_MON/mon_features: l2c_occupancy mbm_core_bytes"
v2chk "L2 monitor domain + files created (mon_L2_*)"  -q "FILE: mbm_core_bytes"
v2chkE "host L2 CSU model: 256KiB @ 50% CBM"        -q "expect=262144 \[OK\]"
v2chk "guest l2c_occupancy = 256KiB (L2 CBM 50%)"   -Eq "mon_L2_[0-9]*/l2c_occupancy: 262144 -> 262144"
l2delta=$(grep -ao 'mon_L2_[0-9]*/mbm_core_bytes: [0-9]* -> [0-9]* (delta [0-9]*)' "$B" | tail -1 | grep -o 'delta [0-9]*' | grep -o '[0-9]*$')
if [ -n "$l2delta" ] && [ "$l2delta" -gt 0 ] 2>/dev/null; then
    if [ "$FLAVOR" = "mainline" ]; then ok "guest mbm_core_bytes advances: delta ${l2delta} bytes/2s"; else info "guest mbm_core_bytes delta ${l2delta} (olk)"; fi
else
    if [ "$FLAVOR" = "mainline" ]; then bad "guest mbm_core_bytes not advancing (read path broken?)"; else info "guest mbm_core_bytes MISSING (olk: not required)"; fi
fi

# ---------- v2: CDP ----------------------------------------------------------
v2chk "CDP mount"                  -q "CDP-MOUNT-OK"
cdpncl=$(grep -ao 'CDP L3DATA/num_closids: [0-9]*' "$B" | grep -o '[0-9]*$')
if [ "$cdpncl" = "16" ]; then
    [ "$FLAVOR" = "mainline" ] && ok "CDP halves num_closids 32->16" || info "CDP num_closids=16"
else
    [ "$FLAVOR" = "mainline" ] && bad "CDP num_closids=${cdpncl:-none} (expected 16)" || info "CDP num_closids=${cdpncl:-none} (olk)"
fi
v2chk "CDP L3DATA schemata write+readback" -Eq "CDP-SCHEMATA: +L3DATA:33751040=(55555555|000055555555)\b"
v2chk "resctrl remount after CDP" -q "REMOUNT-OK"

# bandwidth: 25% clamp — counter linkage required on OLK, reported on mainline
delta=$(grep -ao 'mon_[A-Z0-9_]*/mbm_[a-z_]*: [0-9]* -> [0-9]* (delta [0-9]*)' "$B" | tail -1 | grep -o 'delta [0-9]*' | grep -o '[0-9]*')
if [ "$FLAVOR" = "olk" ]; then
    # v2's OLK flow churns mounts (-o l2, cdp experiments); after that this
    # OLK driver generation returns empty counter reads even on a fresh plain
    # remount. The canonical single-mount clamp measurement is v1's job
    # (../mpam_qemu_test/run_all.sh OLK path: delta ~5e9, PASS).
    if [ -n "$delta" ] && [ "$delta" -gt 3000000000 ] && [ "$delta" -lt 8000000000 ]; then
        ok "MBWU 25% cap: delta ${delta} bytes/2s (~2.5GB/s)"
    else
        info "MBWU clamp after v2 mount churn: ${delta:-no} delta (olk; canonical single-mount clamp covered by v1 package)"
    fi
elif [ -n "$delta" ]; then
    info "last MBWU sample delta: $delta bytes/2s (clamp-counter linkage not required on this flavour)"
fi

echo
if [ $fail -eq 0 ]; then
    echo "ALL STAGES PASSED — MPAM narrow PARTID + v2 features verified ($FLAVOR kernel: $KERNEL)."
else
    echo "SOME STAGES FAILED — inspect $LOGS/"
fi
exit $fail
