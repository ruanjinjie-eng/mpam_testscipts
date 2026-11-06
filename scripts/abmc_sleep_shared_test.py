#!/usr/bin/env python3
"""Assignable MBM counters: sleep trigger and the MPAM shared monitor.

Same topology as abmc_domain_test.py: 1 socket x 2 clusters x 2 cores,
L3 per cluster, so two L3 monitor domains, 8 assignable counters each.

--case pm      (initrd/rootfs_abmc_pm)
    Checks which system sleep states re-create a monitor domain. Each
    state runs the real kernel path with /sys/power/pm_test, counted by
    kprobes on resctrl_{online,offline}_mon_domain():
      - freeze (s2idle, pm_test=freezer): CPUs stay online, no domain
        is re-created, the counters stay assigned;
      - mem (pm_test=processors): QEMU virt only offers s2idle, which
        rejects this test level, so the state is reported as skipped;
      - disk (hibernation, pm_test=processors): the non-boot CPUs go
        offline through pm_sleep_disable_secondary_cpus(), so the
        second domain is re-created once, and root and g1 must still
        have a counter on it.

--case shared  (initrd/rootfs_abmc_shared)
    The default group takes counter 0 and g1 counter 1. The second
    domain is re-created by CPU hotplug and g1 is assigned by hand on
    it. A kprobe on mpam_msmon_read() records the hardware monitor of
    each read. g1 must read monitor 1 on both domains and the default
    group monitor 0. Before the fix the hand assignment gives g1 the
    lowest free counter, 0, and as MPAM maps a counter ID to one monitor
    for the whole resource, g1 then reads monitor 0 on both domains.

Requires QEMU with pmg-max=1 and MBWU monitors (mpam-upstream-v4-split)
and a kernel with CONFIG_KPROBE_EVENTS, CONFIG_HIBERNATION and
CONFIG_PM_DEBUG.

Usage:
  QEMU_BIN=/path/to/qemu-system-aarch64 \
    scripts/abmc_sleep_shared_test.py --case pm|shared [Image]
"""
import argparse
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
KERNEL = os.path.join(MPAM_HOME, "linux_old1/build/arm64/arch/arm64/boot/Image")
DIR = os.path.join(TEST_HOME, "logs")
QMP = os.path.join(DIR, "abmc_case_qmp.sock")
CODE = os.path.join(DIR, "AAVMF_CODE_abmc_case.fd")
VARS = os.path.join(DIR, "AAVMF_VARS_abmc_case.fd")
INITRD = ROOTFS = LOG = None


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


def checks_pm(blob):
    res = []
    res.append(check("resctrl mount", "MOUNT-OK" in blob))
    for s in ("ROOT-ASSIGN-BEFORE", "G1-ASSIGN-BEFORE"):
        v = field(blob, s) or ""
        res.append(check(f"{s.split('-')[0].lower()} has a counter on both "
                         "domains before sleep",
                         len(re.findall(r"\d+=e", v)) == 2, v))

    rc = field(blob, "SLEEP-freeze-RC") or ""
    res.append(check("freeze: s2idle test returns 0", rc.startswith("0"), rc))
    on = field(blob, "SLEEP-freeze-ONLINE-CALLS")
    off = field(blob, "SLEEP-freeze-OFFLINE-CALLS")
    res.append(check("freeze: no monitor domain re-created",
                     on == "0" and off == "0", f"off={off} on={on}"))

    rc = field(blob, "SLEEP-mem-RC") or ""
    if rc.startswith("0"):
        on = field(blob, "SLEEP-mem-ONLINE-CALLS")
        res.append(check("mem: domain re-created and counters kept",
                         on == "1" and
                         len(re.findall(r"\d+=e", field(blob, "SLEEP-mem-ROOT") or "")) == 2,
                         f"on={on}"))
    else:
        print(f"  [SKIP] mem: no suspend-to-RAM on this machine ({rc})", flush=True)

    rc = field(blob, "SLEEP-disk-RC") or ""
    res.append(check("disk: hibernation test returns 0", rc.startswith("0"), rc))
    on = field(blob, "SLEEP-disk-ONLINE-CALLS")
    off = field(blob, "SLEEP-disk-OFFLINE-CALLS")
    res.append(check("disk: one monitor domain offlined and re-created",
                     on == "1" and off == "1", f"off={off} on={on}"))
    doms = (field(blob, "SLEEP-disk-DOMS") or "").split()
    res.append(check("disk: both domains present afterwards", len(doms) == 2,
                     " ".join(doms)))
    for g in ("ROOT", "G1"):
        v = field(blob, f"SLEEP-disk-{g}") or ""
        res.append(check(f"disk: {g.lower()} keeps its counter on the new domain",
                         len(re.findall(r"\d+=e", v)) == 2, v))
    return res


def monitors(blob, name):
    """Hardware monitors used by one read, from the mpam_msmon_read kprobe."""
    v = field(blob, name) or ""
    return re.findall(r"mon=(\d+) partid=(\d+)", v)


def checks_shared(blob):
    res = []
    res.append(check("resctrl mount", "MOUNT-OK" in blob))
    cntrs = field(blob, "NUM-MBM-CNTRS") or ""
    res.append(check("8 assignable counters per domain",
                     len(re.findall(r"=8\b", cntrs)) == 2, cntrs))
    off = (field(blob, "DOMS-OFF") or "").split()
    after = (field(blob, "DOMS-AFTER") or "").split()
    res.append(check("second L3 domain was destroyed and re-created",
                     len(off) == 1 and len(after) == 2, f"off={off} after={after}"))
    v = field(blob, "G1-ASSIGN-AFTER-ONLINE") or ""
    res.append(check("g1 keeps its counter on the new domain",
                     len(re.findall(r"\d+=e", v)) == 2, v))
    v = field(blob, "ROOT-ASSIGN-FINAL") or ""
    res.append(check("root keeps its counter on the new domain",
                     len(re.findall(r"\d+=e", v)) == 2, v))
    res.append(check("kprobe on mpam_msmon_read registered",
                     field(blob, "KPROBE-RC") == "0", field(blob, "KPROBE-RC")))

    dom0 = after[0] if after else ""
    dom1 = after[1] if len(after) > 1 else ""
    root = monitors(blob, f"TRACE-ROOT-{dom0}")
    res.append(check("root reads monitor 0 on the first domain",
                     root and all(m == "0" and p == "0" for m, p in root), root))
    for d in (dom1, dom0):
        g1 = [(m, p) for m, p in monitors(blob, f"TRACE-G1-{d}") if p == "1"]
        res.append(check(f"g1 reads its own monitor 1 on {d}",
                         g1 and all(m == "1" for m, _ in g1), g1))
    return res


CASES = {
    "pm": ("rootfs_abmc_pm", checks_pm),
    "shared": ("rootfs_abmc_shared", checks_shared),
}


def main():
    global INITRD, ROOTFS, LOG, KERNEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=CASES, required=True)
    ap.add_argument("image", nargs="?", default=KERNEL)
    a = ap.parse_args()
    KERNEL = a.image
    rootfs, checks = CASES[a.case]
    ROOTFS = os.path.join(TEST_HOME, "initrd", rootfs)
    INITRD = os.path.join(TEST_HOME, "initrd", f"{rootfs[len('rootfs_'):]}-initrd.gz")
    LOG = os.path.join(DIR, f"abmc_{a.case}_test.log")

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
    results = checks(blob)
    warn = field(blob, "DMESG-WARN")
    results.append(check("no WARNING/BUG in dmesg", warn == "0", warn))

    print(f"\nRESULT: {'ALL CHECKS PASSED' if all(results) else 'FAILURES'}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
