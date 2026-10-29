#!/usr/bin/env python3
"""Multi-topology MPAM guest verification (2 sockets x 2 clusters x 2 cores,
2 NUMA nodes). Complements irq_path_test.py: no injection, topology evidence.

Boots the mainline kernel + a minimal initramfs and captures per-socket L3,
per-cluster L2 and per-node MB domain evidence. Usage:
  scripts/topo_boot_test.py [Image] [initrd.gz]
"""
import os
import subprocess
import sys
import time

MPAM_HOME = os.environ.get("MPAM_HOME", "/home/r00799764/community")
TEST_HOME = os.path.join(MPAM_HOME, "mpam_qemu_test_v2")
QEMU = os.path.join(MPAM_HOME, "qemu/build/qemu-system-aarch64")
KERNEL = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(MPAM_HOME, "linux_old1/build/arm64/arch/arm64/boot/Image")
INITRD = sys.argv[2] if len(sys.argv) > 2 else \
    os.path.join(TEST_HOME, "initrd/topo-initrd.gz")
DIR = os.path.join(TEST_HOME, "logs")
CODE = os.path.join(DIR, "AAVMF_CODE_topo.fd")
VARS = os.path.join(DIR, "AAVMF_VARS_topo.fd")

subprocess.run(["cp", "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd", CODE], check=True)
subprocess.run(["cp", "/usr/share/AAVMF/AAVMF_VARS.fd", VARS], check=True)
for p in (CODE, VARS):
    with open(p, "r+b") as f:
        f.truncate(64 * 1024 * 1024)

args = [
    QEMU,
    "-M", "virt,mpam=on,gic-version=3",
    "-cpu", "max", "-m", "2G",
    "-smp", "8,sockets=2,clusters=2,cores=2,threads=1",
    "-numa", "node,memdev=m0,cpus=0-3",
    "-numa", "node,memdev=m1,cpus=4-7",
    "-object", "memory-backend-ram,size=1G,id=m0",
    "-object", "memory-backend-ram,size=1G,id=m1",
    "-drive", f"if=pflash,format=raw,unit=0,file={CODE},readonly=on",
    "-drive", f"if=pflash,format=raw,unit=1,file={VARS}",
    "-kernel", KERNEL,
    "-initrd", INITRD,
    "-append", "console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init",
    "-nographic", "-no-reboot", "-preconfig",
    "-qmp", f"unix:{os.path.join(DIR, 'topo_qmp.sock')},server,nowait",
]

LOG = os.path.join(DIR, "topo_boot_test.log")
QMP = os.path.join(DIR, "topo_qmp.sock")
if os.path.exists(QMP):
    os.unlink(QMP)

print(f"kernel={KERNEL}\ninitrd={INITRD}\nlog={LOG}", flush=True)
with open(LOG, "wb") as logf:
    proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT)

    # -preconfig: set the cache topology so the PPTT carries cache nodes
    # with the ids the MPAM table references (L2=cluster, L3=socket), then
    # let the machine start.
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
    r = recv()
    print(f"smp-cache qom-set: {r}", flush=True)
    send({"execute": "x-exit-preconfig"})
    try:
        recv()
    except (OSError, RuntimeError):
        pass
    q.close()
    deadline = time.time() + 600     # 8 CPUs under TCG are slow
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(5)
        with open(LOG, "rb") as f:
            data = f.read()
        if b"MARKER:DONE" in data:
            time.sleep(8)      # let the dump finish
            break
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
print("done", flush=True)
