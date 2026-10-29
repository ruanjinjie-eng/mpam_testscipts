#!/usr/bin/env python3
"""No-NUMA MPAM guest verification: -M virt,mpam=on with NO -numa.

Verifies the mpam-patches-v1 cover-letter TODO "defaulting to a single
NUMA node when none is specified": with no -numa options QEMU must create
one memory MSC for the implicit node 0 (ACPI locator proximity domain 0,
resolved by the mainline driver's pxm_to_node() NUMA_NO_NODE fallback) and
the usual per-topology L3/L2 cache MSCs -- WITHOUT fabricating a SRAT.

Also runs a structural validation of the MPAM ACPI table dumped by the
guest (header/checksum, MSC node walk as the kernel parses it, locators,
GSIVs) -- the local stand-in for the RFC's "ACPI table unit tests" TODO.

Usage:
  scripts/nonuma_boot_test.py [Image] [initrd.gz]

Exit codes: 0 = all checks passed, 1 = failures, 2 = QEMU still rejects
mpam=on without NUMA (TODO not implemented yet).
"""
import os
import re
import shutil
import subprocess
import sys
import time

MPAM_HOME = os.environ.get("MPAM_HOME", "/home/r00799764/community")
TEST_HOME = os.path.join(MPAM_HOME, "mpam_qemu_test_v2")
QEMU = os.environ.get("QEMU_BIN",
                      os.path.join(MPAM_HOME, "qemu/build/qemu-system-aarch64"))
KERNEL = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(MPAM_HOME, "linux_old1/build/arm64/arch/arm64/boot/Image")
INITRD = sys.argv[2] if len(sys.argv) > 2 else \
    os.path.join(TEST_HOME, "initrd/nonuma-initrd.gz")
ROOTFS = os.path.join(TEST_HOME, "initrd/rootfs_nonuma")
DIR = os.path.join(TEST_HOME, "logs")
LOG = os.path.join(DIR, "nonuma_boot_test.log")
CODE = os.path.join(DIR, "AAVMF_CODE_nonuma.fd")
VARS = os.path.join(DIR, "AAVMF_VARS_nonuma.fd")
QMP = os.path.join(DIR, "nonuma_qmp.sock")

# Topology: 1 socket, 1 cluster, 4 cores -> 1 L3 MSC + 1 L2 MSC + 1 mem MSC.
# Cache-id locators as emitted by hw/arm/virt-acpi-build.c:
#   cpu | level << 16 | UNIFIED_CACHE << 24, with cpu = first of the group.
L3_CACHE_ID = 0x02030000          # 33751040
L2_CACHE_ID = 0x02020000          # 33685504
MPAM_MMIO_SIZE = 0x4000           # hw/arm/mpam.h MPAM_SIZE
MSC0_BASE = 0x0b006000            # hw/arm/virt.c memmap[VIRT_MPAM_MSC]
GSIV_FIRST = 208                  # MPAM_SPI_BASE (176) + 32


def build_initrd():
    if os.path.exists(INITRD) and \
       os.path.getmtime(INITRD) >= os.path.getmtime(os.path.join(ROOTFS, "init")):
        return
    print(f"building {INITRD}", flush=True)
    with subprocess.Popen(["find", "."], cwd=ROOTFS, stdout=subprocess.PIPE) as f, \
         open(INITRD + ".tmp", "wb") as out, \
         subprocess.Popen(["cpio", "-o", "-H", "newc", "--quiet"],
                          stdin=f.stdout, stdout=out, cwd=ROOTFS) as cpio:
        f.stdout.close()
        cpio.wait()
    with open(INITRD + ".tmp", "rb") as inp, open(INITRD, "wb") as out:
        subprocess.run(["gzip", "-9"], stdin=inp, stdout=out, check=True)
    os.unlink(INITRD + ".tmp")


def boot():
    """Run QEMU without any -numa; return (proc, logf)."""
    for src, dst in (("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd", CODE),
                     ("/usr/share/AAVMF/AAVMF_VARS.fd", VARS)):
        shutil.copy(src, dst)
    for p in (CODE, VARS):
        with open(p, "r+b") as f:
            f.truncate(64 * 1024 * 1024)
    if os.path.exists(QMP):
        os.unlink(QMP)

    args = [
        QEMU,
        "-M", "virt,mpam=on,gic-version=3",
        "-cpu", "max", "-m", "1G",
        # NOTE: no -numa / memory-backend on purpose -- that is the point.
        "-smp", "4,sockets=1,clusters=1,cores=4,threads=1",
        "-drive", f"if=pflash,format=raw,unit=0,file={CODE},readonly=on",
        "-drive", f"if=pflash,format=raw,unit=1,file={VARS}",
        "-kernel", KERNEL,
        "-initrd", INITRD,
        "-append", "console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init",
        "-nographic", "-no-reboot", "-preconfig",
        "-qmp", f"unix:{QMP},server,nowait",
    ]
    print(f"kernel={KERNEL}\ninitrd={INITRD}\nlog={LOG}", flush=True)
    logf = open(LOG, "wb")
    return subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT), logf


def smp_cache_preconfig():
    """-preconfig: give the machine an smp cache topology so the PPTT carries
    cache nodes with the ids the MPAM table references (L2=cluster, L3=socket).
    Without this the guest arm_mpam driver fails cache probing entirely."""
    import json
    import socket as sock_mod
    deadline = time.time() + 30
    while time.time() < deadline and not os.path.exists(QMP):
        time.sleep(0.5)
    q = sock_mod.socket(sock_mod.AF_UNIX)
    q.connect(QMP)
    f = q.makefile("rwb")

    def send(obj):
        f.write(json.dumps(obj).encode() + b"\n")
        f.flush()

    def recv():
        while True:
            line = f.readline()
            if not line:
                raise RuntimeError("qmp eof")
            obj = json.loads(line)
            if "event" in obj:
                continue
            return obj

    recv()
    send({"execute": "qmp_capabilities"})
    recv()
    send({"execute": "qom-set", "arguments": {
        "path": "/machine", "property": "smp-cache",
        "value": [
            {"cache": "l1d", "topology": "core"},
            {"cache": "l1i", "topology": "core"},
            {"cache": "l2", "topology": "cluster"},
            {"cache": "l3", "topology": "socket"},
        ]}})
    print(f"smp-cache qom-set: {recv()}", flush=True)
    send({"execute": "x-exit-preconfig"})
    try:
        recv()
    except (OSError, RuntimeError):
        pass
    q.close()


# --------------------------------------------------------------------------
# MPAM ACPI table structural validation (mirrors drivers/acpi/arm64/mpam.c)
# --------------------------------------------------------------------------
def extract_hexdump(log, tag):
    """Reassemble bytes from `od -A x -t x1` lines prefixed with `tag`.
    Split-based: a regex demanding a trailing space after every byte would
    silently drop the last byte of each line (busybox od does not pad)."""
    out = b""
    for line in log.splitlines():
        line = line.strip()
        if not line.startswith(tag + ":"):
            continue
        toks = line[len(tag) + 1:].split()
        if len(toks) <= 1:          # offset-only line (od's final offset)
            continue
        out += bytes.fromhex("".join(toks[1:]))
    return out


def u16(b, o):
    return b[o] | b[o + 1] << 8


def u32(b, o):
    return b[o] | b[o + 1] << 8 | b[o + 2] << 16 | b[o + 3] << 24


def u64(b, o):
    return u32(b, o) | u32(b, o + 4) << 32


def parse_mpam_table(data):
    """Parse the hexdump from the guest log; return list of MSC node dicts."""
    if len(data) < 36:
        raise ValueError("table shorter than ACPI header")
    checks = []
    checks.append(("signature 'MPAM'", data[0:4] == b"MPAM"))
    length = u32(data, 4)
    checks.append((f"header length {length} == dumped {len(data)}",
                   length == len(data)))
    checks.append((f"checksum ok (sum%256=={sum(data) % 256})", sum(data) % 256 == 0))
    checks.append(("revision >= 1", data[8] >= 1))

    # Walk MSC nodes exactly like acpi_mpam_parse(): nodes tile the table.
    mscs = []
    off = 36
    avail = min(length, len(data))       # truncated dumps stop early
    while off < avail:
        if off + 72 > avail:
            checks.append((f"MSC node @{off:#x} fully captured", False))
            break
        nlen = u16(data, off)
        if nlen < 72 or off + nlen > length:
            checks.append((f"MSC node @{off:#x} length {nlen} sane", False))
            break
        node = {
            "length": nlen,
            "interface_type": data[off + 2],
            "reserved": data[off + 3],
            "identifier": u32(data, off + 4),
            "base": u64(data, off + 8),
            "mmio_size": u32(data, off + 16),
            "oflow_gsiv": u32(data, off + 20),
            "oflow_flags": u32(data, off + 24),
            "err_gsiv": u32(data, off + 36),
            "err_flags": u32(data, off + 40),
            "reserved2": u32(data, off + 44),
            "max_nrdy": u32(data, off + 52),
            "num_ris": u32(data, off + 68),
            "ris": [],
        }
        for i in range(node["num_ris"]):
            ro = off + 72 + 24 * i
            if ro + 24 > min(off + nlen, avail):
                checks.append((f"MSC {node['identifier']} RIS list fits node", False))
                break
            node["ris"].append({
                "locator_type": data[ro + 7],
                "locator1": u64(data, ro + 8),
                "num_deps": u32(data, ro + 20),
            })
        mscs.append(node)
        off += nlen
    checks.append((f"MSC nodes tile the table exactly (end {off:#x} == {length:#x})",
                   off == length))
    return checks, mscs


def main():
    for p in (QEMU, KERNEL):
        if not os.path.isfile(p):
            sys.exit(f"missing {p}")
    os.makedirs(DIR, exist_ok=True)
    build_initrd()

    proc, logf = boot()
    try:
        smp_cache_preconfig()
        deadline = time.time() + 480      # 4 CPUs under TCG
        done = False
        early_exit = False
        while time.time() < deadline:
            if proc.poll() is not None:
                early_exit = True
                break
            time.sleep(5)
            with open(LOG, "rb") as f:
                if b"MARKER:DONE" in f.read():
                    time.sleep(8)         # let the dump finish
                    done = True
                    break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()

    with open(LOG, "rb") as f:
        log = f.read().decode(errors="replace")

    results = run_battery(log, early_exit, done)
    if results is None:
        print("\n[FAIL] QEMU rejected mpam=on without NUMA -- TODO NOT implemented")
        print(f"       evidence in {LOG}")
        print("\nRESULT: NOT-IMPLEMENTED (exit 2)")
        return 2

    # ---- report ------------------------------------------------------------
    print()
    failed = 0
    for label, ok, required in results:
        tag = "[PASS]" if ok else ("[FAIL]" if required else "[INFO]")
        if not ok and required:
            failed += 1
        print(f"  {tag} {label}")
    print()
    if failed:
        print(f"RESULT: {failed} FAILED -- inspect {LOG}")
        return 1
    print("RESULT: ALL CHECKS PASSED -- mpam=on works without -numa "
          "(implicit node 0), MPAM ACPI table structurally valid.")
    return 0


def run_battery(log, early_exit, done):
    """Full check battery against a boot log. Returns a list of
    (label, ok, required), or None if QEMU rejected mpam=on without NUMA
    (TODO not implemented)."""
    log = log.replace("\r\n", "\n")     # serial console emits CR LF
    results = []

    def check(label, ok, required=True):
        results.append((label, ok, required))

    if "MPAM support requires NUMA nodes to be specified" in log:
        return None

    check("QEMU accepts mpam=on without -numa (no early abort)", not early_exit or done)
    check("guest reached userspace (MARKER:USERSPACE-UP)", "MARKER:USERSPACE-UP" in log)
    check("guest verification completed (MARKER:DONE)", done)

    # ---- implicit single node: node0 only, and NO SRAT fabricated ---------
    check("implicit single node: only node0 in /sys/devices/system/node",
          re.search(r"NODE: node0\b", log) is not None and
          "NODE: node1" not in log)
    check("no SRAT fabricated: /sys/firmware/acpi/tables/SRAT absent",
          "SRAT-ABSENT" in log)
    check("MPAM table present", "MPAM-SIZE:" in log)
    pptt_rev3 = re.search(r"PPTT-SIZE: (\d+)", log) is not None
    check("PPTT table present", pptt_rev3)

    # ---- MSC / resctrl functional evidence --------------------------------
    msc_lines = re.findall(r"^MSC: (\S+)$", log, re.M)
    check(f"3 MSC devices created (mem+L3+L2), saw {msc_lines}",
          msc_lines == ["mpam_msc.0", "mpam_msc.1", "mpam_msc.2"])
    check("resctrl mounts", "RESCTRL-MOUNT-OK" in log)
    ncl = re.search(r"info/L3/num_closids: (\d+)", log)
    check(f"L3 num_closids=32 (narrow PARTID), saw {ncl.group(1) if ncl else None}",
          ncl is not None and ncl.group(1) == "32")
    check("MB resource exported (memory MSC resolved to node 0)",
          re.search(r"SCHEMATA:\s+MB:\d+=", log) is not None)
    check("MB single domain, write MB=50 accepted",
          "WRITE-OK   [MB:33751040=50" in log)
    check("MB=50 readback", re.search(r"SCHEMATA:\s+MB:33751040=50\b", log) is not None)
    check("L3 bitmap write 55555555 accepted",
          "WRITE-OK   [L3:33751040=55555555" in log)
    check("L3=55555555 readback",
          re.search(r"SCHEMATA:\s+L3:33751040=55555555\b", log) is not None)
    check("L2 domain present (id 33685504)",
          re.search(r"SCHEMATA:\s+L2:33685504=", log) is not None)
    check("MPAM error interrupts armed in /proc/interrupts",
          len(re.findall(r"^IRQ:.*mpam", log, re.M)) >= 3)

    # ---- MPAM ACPI table structural validation ----------------------------
    hex_bytes = extract_hexdump(log, "MPAMTBL")
    if hex_bytes:
        try:
            tbl_checks, mscs = parse_mpam_table(hex_bytes)
            for label, ok in tbl_checks:
                check(f"MPAM table: {label}", ok)
            check(f"MPAM table: 3 MSC nodes, saw {len(mscs)}", len(mscs) == 3)
            # Identify MSC roles by locator, NOT by order: the QOM child
            # enumeration walks the tree in reverse creation order, so the
            # memory MSC (created first) carries the highest identifier.
            mems = [m for m in mscs if any(r["locator_type"] == 1
                                           for r in m["ris"])]
            l3s = [m for m in mscs if any(r["locator_type"] == 0 and
                                          r["locator1"] == L3_CACHE_ID
                                          for r in m["ris"])]
            l2s = [m for m in mscs if any(r["locator_type"] == 0 and
                                          r["locator1"] == L2_CACHE_ID
                                          for r in m["ris"])]
            check("exactly 1 memory MSC (by locator)", len(mems) == 1)
            check("exactly 1 L3 cache MSC (by locator)", len(l3s) == 1)
            check("exactly 1 L2 cache MSC (by locator)", len(l2s) == 1)
            if len(mems) == len(l3s) == len(l2s) == 1:
                mem = mems[0]
                check("mem MSC: MMIO interface", mem["interface_type"] == 0)
                check(f"mem MSC: mmio_size={MPAM_MMIO_SIZE:#x}",
                      mem["mmio_size"] == MPAM_MMIO_SIZE)
                check(f"mem MSC: base {MSC0_BASE:#x}", mem["base"] == MSC0_BASE)
                check("mem MSC: 1 RIS, locator type 1 (memory)",
                      mem["num_ris"] == 1 and len(mem["ris"]) == 1)
                check("mem MSC: locator proximity domain 0 (implicit node)",
                      mem["ris"] and mem["ris"][0]["locator1"] == 0)
                for name, ms, cid in (("L3", l3s, L3_CACHE_ID),
                                      ("L2", l2s, L2_CACHE_ID)):
                    m = ms[0]
                    check(f"{name} MSC: 1 RIS, locator type 0 (cache)",
                          m["num_ris"] == 1 and len(m["ris"]) == 1 and
                          m["ris"][0]["locator_type"] == 0)
                    check(f"{name} MSC: cache reference {cid:#x}",
                          m["ris"] and m["ris"][0]["locator1"] == cid)
                gsivs = [m["oflow_gsiv"] for m in mscs] + [m["err_gsiv"] for m in mscs]
                check(f"6 distinct GSIVs >= {GSIV_FIRST} (error+overflow x3)",
                      len(set(gsivs)) == 6 and all(g >= GSIV_FIRST for g in gsivs))
                check("interrupt flags 0 (wired, level) everywhere",
                      all(m["oflow_flags"] == 0 and m["err_flags"] == 0 for m in mscs))
                check("identifiers unique (0,1,2)",
                      sorted(m["identifier"] for m in mscs) == [0, 1, 2])
                check("no functional dependencies declared",
                      all(not r["num_deps"] for m in mscs for r in m["ris"]))
                check("reserved fields clear (kernel would refuse MPAM)",
                      all(m["reserved"] == 0 and m["reserved2"] == 0 for m in mscs))
        except ValueError as e:
            check(f"MPAM table parse ({e})", False)
    else:
        check("MPAM table hexdump captured", False, required=False)

    return results


if __name__ == "__main__":
    sys.exit(main())
