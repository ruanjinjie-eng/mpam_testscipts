#!/usr/bin/env python3
"""QEMU test case: MPAM MBA software controller (mba_MBps) absolute target.

Boots an arm64 kernel under QEMU's MPAM model, mounts resctrl with the
"mba_MBps" option and writes an absolute bandwidth target (MiB/s) into the
MB schemata.  The kernel feedback loop must program MBW_MAX so that the
bandwidth reported by the MBWU counters converges to the target.

The controller must also set MBW_MAX.HARDLIM: without it MPAM services
requests above the maximum when no other request is waiting, which breaks
the absolute target.  The test reads the memory MSC register through qtest
and requires the bit.

QEMU models MBWU as bw-nominal-bps * MBW_MAX fraction, so with the default
10 GB/s nominal the full-bandwidth reading is ~9536 MiB/s.

Usage:
  scripts/mba_mbps_test.py --image Image
  scripts/mba_mbps_test.py --image Image --max-lim 1 --expect refuse
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
                      os.path.join(MPAM_HOME, "qemu", "build", "qemu-system-aarch64"))
ROOTFS = os.path.join(TEST_HOME, "initrd", "rootfs_mba_mbps")
DEFAULT_INITRD = os.path.join(TEST_HOME, "initrd", "mba-mbps-initrd.gz")
DIR = os.path.join(TEST_HOME, "logs", "mba_mbps_test")
LOG = os.path.join(DIR, "guest.log")
QTEST = os.path.join(DIR, "qtest.sock")
QMP = os.path.join(DIR, "qmp.sock")
CMDLINE = os.environ.get(
    "BOOT_CMDLINE",
    "console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init")

# First MPAM MSC in the virt machine is the memory-side MSC, which backs
# the MBA resource.
MSC_BASE = 0x0b006000
PART_SEL = MSC_BASE + 0x100
MBW_MAX = MSC_BASE + 0x208
HARDLIM = 1 << 31


def build_initrd(out):
    subprocess.run(
        "cd %s && find . | cpio -o -H newc --quiet | gzip -9 > %s" %
        (ROOTFS, out), shell=True, check=True)


def qmp_recv(sock):
    buf = b""
    while True:
        data = sock.recv(1 << 20)
        if not data:
            raise RuntimeError("QMP EOF")
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "event" in obj:
                continue
            return obj


def configure_machine(qmp_sock):
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.connect(qmp_sock)
        qmp_recv(sock)
        sock.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
        qmp_recv(sock)
        sock.sendall(json.dumps({
            "execute": "qom-set",
            "arguments": {
                "path": "/machine",
                "property": "smp-cache",
                "value": [
                    {"cache": "l1d", "topology": "core"},
                    {"cache": "l1i", "topology": "core"},
                    {"cache": "l2", "topology": "cluster"},
                    {"cache": "l3", "topology": "socket"},
                ],
            }}).encode() + b"\n")
        reply = qmp_recv(sock)
        if "error" in reply:
            raise RuntimeError("smp-cache set failed: %s" % reply["error"])
        sock.sendall(json.dumps({"execute": "x-exit-preconfig"}).encode() + b"\n")
        try:
            qmp_recv(sock)
        except (OSError, RuntimeError):
            pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def read_hardlim():
    """Read MBW_MAX of PARTID 0 from the memory-side MSC through qtest."""
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(QTEST)
    buf = b""

    def qt(cmd):
        nonlocal buf
        sock.sendall(cmd.encode() + b"\n")
        while b"\n" not in buf:
            data = sock.recv(4096)
            if not data:
                raise RuntimeError("qtest EOF")
            buf += data
        line, buf = buf.split(b"\n", 1)
        return line.decode().strip()

    try:
        qt("writel 0x%x 0x0" % PART_SEL)
        val = int(qt("readl 0x%x" % MBW_MAX).split()[-1], 16)
    finally:
        sock.close()
    return val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True,
                    help="arm64 Image to boot")
    ap.add_argument("--initrd", default=DEFAULT_INITRD,
                    help="guest initramfs (default: %s)" % DEFAULT_INITRD)
    ap.add_argument("--expect", choices=["pass", "fail", "skip", "refuse"],
                    default="pass",
                    help="expected result; 'refuse' expects mba_MBps to be "
                         "rejected while a plain mount works")
    ap.add_argument("--max-lim", type=int, default=0,
                    help="MPAMF_MBW_IDR.MAX_LIM value (0 both, 1 soft only, "
                         "2 hard only)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds to wait for guest MARKER:DONE")
    ap.add_argument("--no-build", action="store_true",
                    help="use an existing initrd instead of rebuilding it")
    args = ap.parse_args()

    if not os.path.exists(args.image):
        sys.exit("Image not found: %s" % args.image)
    if not args.no_build:
        build_initrd(args.initrd)
    if not os.path.exists(args.initrd):
        sys.exit("initrd not found: %s" % args.initrd)

    os.makedirs(DIR, exist_ok=True)
    for path in (LOG, QTEST, QMP):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    vars_fd = os.path.join(DIR, "AAVMF_VARS.fd")
    code_fd = os.path.join(DIR, "AAVMF_CODE.fd")
    shutil.copyfile("/usr/share/AAVMF/AAVMF_VARS.fd", vars_fd)
    shutil.copyfile("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd", code_fd)
    for path in (vars_fd, code_fd):
        with open(path, "r+b") as f:
            f.truncate(64 * 1024 * 1024)

    qemu_args = [
        QEMU,
        "-M", "virt,mpam=on,gic-version=3",
        "-cpu", "max", "-m", "2G",
        "-smp", "2,sockets=1,clusters=1,cores=2,threads=1",
        "-numa", "node,memdev=m0",
        "-object", "memory-backend-ram,size=2G,id=m0",
        "-drive", "if=pflash,format=raw,unit=0,file=%s,readonly=on" % code_fd,
        "-drive", "if=pflash,format=raw,unit=1,file=%s" % vars_fd,
        "-kernel", args.image,
        "-initrd", args.initrd,
        "-append", CMDLINE,
        "-nographic", "-no-reboot", "-preconfig",
        "-qtest", "unix:%s,server,nowait" % QTEST,
        "-qmp", "unix:%s,server,nowait" % QMP,
    ]
    if args.max_lim:
        qemu_args += ["-global", "mpam-msc-mem.mbw-max-lim=%d" % args.max_lim]

    logf = open(LOG, "wb")
    proc = subprocess.Popen(qemu_args, stdout=logf, stderr=subprocess.STDOUT)
    print("QEMU pid %d, log %s" % (proc.pid, LOG), flush=True)

    deadline = time.time() + args.timeout
    qmp_ready = False
    done = False
    blob = ""
    mbw_max = None
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if not qmp_ready and os.path.exists(QMP):
                try:
                    configure_machine(QMP)
                    qmp_ready = True
                    print("machine configured (smp-cache, exit-preconfig)",
                          flush=True)
                except (FileNotFoundError, ConnectionRefusedError,
                        RuntimeError, OSError, ValueError):
                    pass
            try:
                with open(LOG, "rb") as f:
                    blob = f.read().decode(errors="replace")
            except FileNotFoundError:
                blob = ""
            if "MARKER:DONE" in blob:
                done = True
                break
            time.sleep(2)
        if done and args.expect != "refuse":
            try:
                mbw_max = read_hardlim()
            except (OSError, RuntimeError, ValueError) as err:
                print("qtest read failed: %s" % err, flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()

    lines = [l for l in blob.splitlines()
             if ("MBAP-TEST:" in l or "MARKER:DONE" in l or
                 l.startswith("Linux version"))]
    print("\n".join(lines) if lines else blob[-4000:])
    if mbw_max is not None:
        print("MBW_MAX=0x%08x hardlim=%d" % (mbw_max, int(bool(mbw_max & HARDLIM))))

    if not done:
        print("\nTIMEOUT waiting for MARKER:DONE")
        return 1

    if args.expect == "refuse":
        refused = ("MBAP-TEST: mount_failed" in blob and
                   "MBAP-TEST: plain_mount_ok" in blob)
        result = "refuse" if refused else "none"
        print("\nobserved: %s, expected: refuse" % result)
        return 0 if refused else 1

    match = re.search(r"MBAP-TEST: RESULT=(PASS|FAIL|SKIP)", blob)
    result = match.group(1).lower() if match else "none"
    hardlim = mbw_max is not None and bool(mbw_max & HARDLIM)
    print("\nobserved: %s (hardlim=%s), expected: %s" %
          (result, hardlim, args.expect))
    if result != args.expect:
        return 1
    if result == "pass" and not hardlim:
        print("FAIL: the software controller did not set MBW_MAX.HARDLIM")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
