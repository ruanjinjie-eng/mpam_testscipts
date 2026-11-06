#!/usr/bin/env python3
"""Assignable MBM counters when a monitor domain is re-created.

Topology: 1 socket x 2 clusters x 2 cores, L3 per cluster, so there are
two L3 monitor domains. The guest creates the default group plus g1..g8.
With 8 assignable counters per domain, the default group and g1..g7 take
one each and g8 gets none. All CPUs of the second cluster are offlined
and brought back: the new domain starts with an empty cntr_cfg array.

Checks:
  - the existing groups keep the counter IDs they use on the other
    domain: ROOT-ASSIGN-AFTER has "=e" on both domains;
  - the group that had no counter does not take one of those IDs:
    G8-ASSIGN-AFTER is "_" on both domains;
  - the root group reads a value on the re-created domain and g8 reads
    "Unassigned";
  - no WARNING/BUG in dmesg.

Requires QEMU with pmg-max=1 (two PMGs per PARTID, num_rmids=64) and
MBWU monitors, e.g. the mpam-upstream-v4-split build.

Usage:
  QEMU_BIN=/path/to/qemu-system-aarch64 scripts/abmc_domain_test.py [Image]
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

MPAM_HOME = os.environ.get("MPAM_HOME", "/home/r00799764/community")
TEST_HOME = os.path.join(MPAM_HOME, "mpam_qemu_test_v2")
QEMU = os.environ.get("QEMU_BIN",
                      os.path.join(MPAM_HOME, "qemu/build/qemu-system-aarch64"))
KERNEL = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(MPAM_HOME, "linux_old1/build/arm64/arch/arm64/boot/Image")
INITRD = os.path.join(TEST_HOME, "initrd/abmc-initrd.gz")
ROOTFS = os.path.join(TEST_HOME, "initrd/rootfs_abmc")
DIR = os.path.join(TEST_HOME, "logs")
LOG = os.path.join(DIR, "abmc_domain_test.log")
QMP = os.path.join(DIR, "abmc_domain_qmp.sock")
CODE = os.path.join(DIR, "AAVMF_CODE_abmc.fd")
VARS = os.path.join(DIR, "AAVMF_VARS_abmc.fd")


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


def qmp_recv(sock):
    buf = b""
    while True:
        d = sock.recv(1 << 20)
        if not d:
            raise RuntimeError("qmp eof")
        buf += d
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "event" in obj:
                continue
            return obj


def boot():
    os.makedirs(DIR, exist_ok=True)
    for p in (QMP,):
        if os.path.exists(p):
            os.unlink(p)
    for src, dst in (("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd", CODE),
                     ("/usr/share/AAVMF/AAVMF_VARS.fd", VARS)):
        shutil.copyfile(src, dst)
        with open(dst, "r+b") as f:
            f.truncate(64 * 1024 * 1024)

    args = [
        QEMU, "-M", "virt,mpam=on,gic-version=3", "-cpu", "max", "-m", "2G",
        "-smp", "4,sockets=1,clusters=2,cores=2,threads=1",
        "-numa", "node,memdev=m0",
        "-object", "memory-backend-ram,size=2G,id=m0",
        "-drive", f"if=pflash,format=raw,unit=0,file={CODE},readonly=on",
        "-drive", f"if=pflash,format=raw,unit=1,file={VARS}",
        "-kernel", KERNEL, "-initrd", INITRD,
        "-append", "console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init",
        "-nographic", "-no-reboot", "-preconfig",
        "-qmp", f"unix:{QMP},server,nowait",
    ]
    logf = open(LOG, "wb")
    proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT)
    print(f"QEMU pid {proc.pid}, log {LOG}", flush=True)

    q = None
    deadline = time.time() + 60 * 10
    while time.time() < deadline and proc.poll() is None:
        if os.path.exists(QMP):
            try:
                q = socket.socket(socket.AF_UNIX)
                q.connect(QMP)
                qmp_recv(q)
                q.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
                qmp_recv(q)
                q.sendall(json.dumps({
                    "execute": "qom-set",
                    "arguments": {
                        "path": "/machine", "property": "smp-cache",
                        "value": [
                            {"cache": "l1d", "topology": "core"},
                            {"cache": "l1i", "topology": "core"},
                            {"cache": "l2", "topology": "core"},
                            {"cache": "l3", "topology": "cluster"},
                        ]}}).encode() + b"\n")
                r = qmp_recv(q)
                print(f"smp-cache qom-set: {r}", flush=True)
                if "error" in r:
                    raise RuntimeError(f"smp-cache set failed: {r['error']}")
                q.sendall(json.dumps({"execute": "x-exit-preconfig"}).encode() + b"\n")
                try:
                    qmp_recv(q)
                except (OSError, RuntimeError):
                    pass
                q.close()
                break
            except (FileNotFoundError, ConnectionRefusedError, RuntimeError,
                    AttributeError, KeyError, OSError):
                if q:
                    q.close()
                q = None
                time.sleep(1)
    return proc, logf


def guest_done():
    deadline = time.time() + 60 * 25
    while time.time() < deadline:
        try:
            blob = open(LOG, "rb").read().decode(errors="replace")
        except FileNotFoundError:
            blob = ""
        if "MARKER:DONE" in blob:
            return True, blob
        time.sleep(2)
    return False, blob


def field(blob, name):
    m = re.search(rf"^{re.escape(name)}: (.*)$", blob, re.M)
    return m.group(1).strip() if m else None


def check(name, ok, detail=""):
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}" +
          (f" ({detail})" if detail and not ok else ""), flush=True)
    return ok


def main():
    for p in (QEMU, KERNEL):
        if not os.path.exists(p):
            sys.exit(f"missing {p}")
    build_initrd()
    proc, logf = boot()
    ok, blob = guest_done()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    logf.close()

    if not ok:
        print("TIMEOUT waiting for MARKER:DONE; log tail:")
        print(blob[-3000:])
        sys.exit(1)

    blob = blob.replace("\r", "")

    results = []
    results.append(check("resctrl mount", field(blob, "MOUNT-OK") is not None or
                         "MOUNT-OK" in blob))
    redis = field(blob, "NUM-RMIDS")
    results.append(check("num_rmids=64 (QEMU pmg-max=1)",
                         redis is not None and redis.strip() == "64", redis))
    cntrs = field(blob, "NUM-MBM-CNTRS")
    results.append(check("8 assignable counters per domain",
                         cntrs is not None and len(re.findall(r"=\d+", cntrs)) == 2 and
                         len(re.findall(r"=8", cntrs)) == 2, cntrs))
    groups = field(blob, "GROUPS")
    results.append(check("8 groups created", groups == "8", groups))

    before = field(blob, "ROOT-ASSIGN-BEFORE") or ""
    results.append(check("root has a counter on both domains before hotplug",
                         len(re.findall(r"\d+=e", before)) == 2, before))
    g8_before = field(blob, "G8-ASSIGN-BEFORE") or ""
    results.append(check("g8 has no counter before hotplug",
                         len(re.findall(r"\d+=_", g8_before)) == 2, g8_before))

    off = field(blob, "DOMS-OFF") or ""
    after = field(blob, "DOMS-AFTER") or ""
    new_dom = sorted(set(after.split()) - set(off.split()))
    results.append(check("second L3 domain was destroyed and re-created",
                         len(off.split()) == 1 and len(after.split()) == 2 and
                         len(new_dom) == 1, f"off={off!r} after={after!r}"))

    root_after = field(blob, "ROOT-ASSIGN-AFTER") or ""
    results.append(check("root keeps its counter on the new domain",
                         len(re.findall(r"\d+=e", root_after)) == 2, root_after))
    g8_after = field(blob, "G8-ASSIGN-AFTER") or ""
    results.append(check("g8 did not take another group's counter",
                         len(re.findall(r"\d+=_", g8_after)) == 2, g8_after))
    g1_after = field(blob, "G1-ASSIGN-AFTER") or ""
    results.append(check("g1 keeps its counter on the new domain",
                         len(re.findall(r"\d+=e", g1_after)) == 2, g1_after))
    g7_before = field(blob, "G7-ASSIGN-BEFORE") or ""
    results.append(check("g7 unassigned by hand before hotplug",
                         field(blob, "G7-UNASSIGN-RC") == "0" and
                         len(re.findall(r"\d+=_", g7_before)) == 2, g7_before))
    g7_after = field(blob, "G7-ASSIGN-AFTER") or ""
    results.append(check("g7 stays unassigned on the new domain",
                         len(re.findall(r"\d+=_", g7_after)) == 2, g7_after))

    if new_dom:
        d = new_dom[0]
        root_val = field(blob, f"ROOT {d}")
        g8_val = field(blob, f"G8 {d}")
        results.append(check("root reads a value on the new domain",
                             root_val is not None and
                             re.fullmatch(r"\d+", root_val.strip()) is not None,
                             root_val))
        results.append(check("g8 reads Unassigned on the new domain",
                             g8_val is not None and g8_val.strip() == "Unassigned",
                             g8_val))
    else:
        results.append(check("root reads a value on the new domain", False))
        results.append(check("g8 reads Unassigned on the new domain", False))

    warn = field(blob, "DMESG-WARN")
    results.append(check("no WARNING/BUG in dmesg", warn == "0", warn))

    print(f"\nRESULT: {'ALL CHECKS PASSED' if all(results) else 'FAILURES'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
