#!/usr/bin/env python3
"""Boot an arm64 kernel (mainline arm_mpam or OLK-6.6) under QEMU
virt,mpam=on and verify MPAM narrow PARTID plus the v2 feature set
(PRI/capacity/limit controls, CSU occupancy, L2 monitors) end-to-end:
poll the boot log for the guest markers, then introspect the emulated
MSCs via qtest/QMP to see what the guest driver programmed.

Kernel selection/flavour is via env (set by run_all.sh): OLK_IMAGE,
BOOT_CMDLINE, CPU_MODEL, INITRD_GZ, MPAM_HOME."""
import json, os, socket, subprocess, sys, time

MPAM_HOME = os.environ.get("MPAM_HOME", "/home/r00799764/community")
QEMU = os.environ.get("QEMU_BIN",
    os.path.join(MPAM_HOME, "qemu", "build", "qemu-system-aarch64"))
KERNEL = os.environ.get("OLK_IMAGE",
    os.path.join(MPAM_HOME, "linux_old1", "build", "arm64",
                 "arch", "arm64", "boot", "Image"))
INITRD = os.environ.get("INITRD_GZ",
    os.path.join(MPAM_HOME, "mpam_qemu_test_v2", "initrd", "olk-initrd.gz"))
DIR = os.path.join(MPAM_HOME, "mpam_qemu_test_v2", "logs")
# OLK-6.6 needs the arm64.mpam cmdline gate; mainline-style kernels do not
# (override via BOOT_CMDLINE when testing other kernels).
CMDLINE = os.environ.get("BOOT_CMDLINE",
    "console=ttyAMA0 earlycon=pl011,0x09000000 rdinit=/init")
# MPAMSM_EL1 is implemented now, so plain `max` (SME on) boots v7.x too.
CPU_MODEL = os.environ.get("CPU_MODEL", "max")
QTEST = f"{DIR}/olk_qtest.sock"
QMP = f"{DIR}/olk_qmp.sock"
LOG = f"{DIR}/olk_boot.log"
VARS = f"{DIR}/AAVMF_VARS_olk.fd"

os.makedirs(DIR, exist_ok=True)
for p in (QTEST, QMP):
    if os.path.exists(p):
        os.unlink(p)
subprocess.run(["cp", "/usr/share/AAVMF/AAVMF_VARS.fd", VARS], check=True)
CODE = f"{DIR}/AAVMF_CODE_olk.fd"
subprocess.run(["cp", "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd", CODE], check=True)
with open(CODE, "r+b") as f:            # virt flash0 is 64MiB; pad the 2MiB image
    f.truncate(64 * 1024 * 1024)
with open(VARS, "r+b") as f:
    f.truncate(64 * 1024 * 1024)

args = [
    QEMU,
    "-M", "virt,mpam=on,gic-version=3",
    "-cpu", CPU_MODEL, "-m", "2G",
    "-smp", "2,sockets=1,clusters=1,cores=2,threads=1",
    "-numa", "node,memdev=m0",
    "-object", "memory-backend-ram,size=2G,id=m0",
    "-drive", f"if=pflash,format=raw,unit=0,file={CODE},readonly=on",
    "-drive", f"if=pflash,format=raw,unit=1,file={VARS}",
    "-kernel", KERNEL,
    "-initrd", INITRD,
    "-append", CMDLINE,
    "-nographic", "-no-reboot", "-preconfig",
    "-qtest", f"unix:{QTEST},server,nowait",
    "-qmp", f"unix:{QMP},server,nowait",
]
logf = open(LOG, "wb")
proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT)
print(f"QEMU pid {proc.pid}, log {LOG}", flush=True)


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

qmp_sock = None
deadline = time.time() + 60 * 25          # 25 min max under TCG
up_seen = done_seen = False
qmp_ready = False
while time.time() < deadline:
    if proc.poll() is not None:
        print(f"QEMU exited early rc={proc.returncode}")
        sys.exit(1)
    if not qmp_ready and os.path.exists(QMP):
        try:
            qmp_sock = socket.socket(socket.AF_UNIX)
            qmp_sock.connect(QMP)
            qmp_recv(qmp_sock)
            qmp_sock.sendall(json.dumps(
                {"execute": "qmp_capabilities"}).encode() + b"\n")
            qmp_recv(qmp_sock)
            # L3 at socket, L2 at cluster: makes the PPTT carry cache
            # nodes with ids the MPAM table can reference.
            qmp_sock.sendall(json.dumps({
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
            r = qmp_recv(qmp_sock)
            print(f"smp-cache qom-set: {r}", flush=True)
            if "error" in r:
                raise RuntimeError(f"smp-cache set failed: {r['error']}")
            qmp_sock.sendall(json.dumps(
                {"execute": "x-exit-preconfig"}).encode() + b"\n")
            try:
                qmp_recv(qmp_sock)
            except (OSError, RuntimeError):
                pass        # QMP may drop the channel during machine init
            qmp_ready = True
            print("preconfig smp-cache configured", flush=True)
            try:
                qmp_sock.close()   # free the single QMP client slot for the
            except OSError:        # final query-mpam-cache connection
                pass
        except (FileNotFoundError, ConnectionRefusedError, RuntimeError,
                AttributeError, KeyError, OSError):
            try:
                qmp_sock.close()
            except (OSError, AttributeError):
                pass
    try:
        blob = open(LOG, "rb").read().decode(errors="replace")
    except FileNotFoundError:
        blob = ""
    if "MARKER:USERSPACE-UP" in blob and not up_seen:
        up_seen = True
        print(f"[{time.strftime('%H:%M:%S')}] guest userspace is up", flush=True)
    if "MARKER:DONE" in blob:
        done_seen = True
        print(f"[{time.strftime('%H:%M:%S')}] guest verification script finished", flush=True)
        break
    time.sleep(5)

if not done_seen:
    print("TIMEOUT waiting for guest DONE marker; dumping log tail:")
    print(blob[-3000:])
    proc.terminate()
    sys.exit(1)

# Give the driver's async work a moment, then snapshot MSC state.
time.sleep(3)

qbuf = b""
def qt(cmd):
    global qbuf
    qs.sendall(cmd.encode() + b"\n")
    while b"\n" not in qbuf:
        d = qs.recv(4096)
        if not d:
            raise RuntimeError("qtest eof")
        qbuf += d
    line, qbuf = qbuf.split(b"\n", 1)
    return line.decode().strip()

def readq(a):  return int(qt(f"readq {a:#x}").split()[-1], 16)
def readl(a):  return int(qt(f"readl {a:#x}").split()[-1], 16)
def writel(a, v): qt(f"writel {a:#x} {v:#x}")

qs = socket.socket(socket.AF_UNIX)
qs.connect(QTEST)

MEMMSC = 0x0B006000
L3MSC  = 0x0B00A000
L2MSC  = 0x0B00E000

def recon_cache(msc, name):
    """Per-partid PRI / capacity / CPBM snapshot of a cache MSC."""
    print(f"\n=== host-side {name} MSC introspection (@ {msc:#x}) ===")
    print("part_sel : INTPARTID(map)  PRI(IN:DSP)  CMIN  CMAX  CASSOC  CPBM(w0)")
    nondefault = 0
    for p in range(4):
        writel(msc + 0x100, p)
        ip = readl(msc + 0x600) & 0xFFFF
        pri = readl(msc + 0x400)
        cmin = readl(msc + 0x110) & 0xFFFF
        cmax = readl(msc + 0x108) & 0xFFFF
        cassoc = readl(msc + 0x118) & 0xFFFF
        cpbm = readl(msc + 0x1000)
        mark = ""
        if (pri & 0xFFFF) or cmin or (cmax not in (0, 0xF)) or \
           (cpbm not in (0, 0xFFFFFFFF)):
            mark = "  <-- driver programmed"
            nondefault += 1
        print(f"{p:8d} : {ip:#06x}          {pri >> 16:#04x}:{pri & 0xFFFF:#04x}"
              f"   {cmin:#06x} {cmax:#06x}  {cassoc:#06x}    {cpbm:#010x}{mark}")
    return nondefault

nd_l3 = recon_cache(L3MSC, "L3 cache")
nd_l2 = recon_cache(L2MSC, "L2 cache")

print("\n=== host-side MSC introspection (mem MSC @ 0x0b006000) ===")
print("part_sel : PRI(IN)  MBW_MIN  MBW_MAX  HARDLIM  MBW_PROP  MBW_PBM(n/16)")
nd_mem = 0
for p in range(8):
    writel(MEMMSC + 0x100, p)
    pri = readl(MEMMSC + 0x400) & 0xFFFF
    mn = readl(MEMMSC + 0x200) & 0xFFFF
    mx = readl(MEMMSC + 0x208)
    prop = readl(MEMMSC + 0x500)
    pbm = readl(MEMMSC + 0x2000)
    hdl = "Y" if (mx >> 31) & 1 else "-"
    mark = ""
    if pri or mn or (mx & 0xFFFF) != 0xFFFF or (mx >> 31) & 1 or prop or \
       (pbm & 0xFFFF) != 0xFFFF:
        mark = "  <-- driver programmed"
        nd_mem += 1
    print(f"{p:8d} : {pri:#06x}   {mn:#06x}    {mx & 0xFFFF:#06x}     {hdl}"
          f"        {prop:#06x}     {pbm & 0xFFFF:#06x} "
          f"({bin(pbm & 0xFFFF).count('1')}/16){mark}")

# --- CSU occupancy closure on the L3 MSC: capacity x CPBM fraction -------
print("\n=== host-side CSU occupancy model check (L3 MSC, capacity 32MiB) ===")
writel(L3MSC + 0x100, 0)                      # part_sel 0
cpbm = readl(L3MSC + 0x1000)
writel(L3MSC + 0x800, 0)                      # MON_SEL: mon 0, ris 0
writel(L3MSC + 0x810, 0)                      # CSU_FLT: partid 0
writel(L3MSC + 0x818, 0x43 | (1 << 16) | (1 << 31))   # TYPE_CSU|MATCH_PARTID|EN
csu = readl(L3MSC + 0x840)
expect = 32 * 1024 * 1024 * bin(cpbm & 0xFFFFFFFF).count("1") // 32
verdict = "OK" if csu == expect else "MISMATCH"
print(f"partid0 CPBM={cpbm:#010x} ({bin(cpbm).count('1')}/32 bits) "
      f"CSU={csu} expect={expect} [{verdict}]")

# --- L2 CSU occupancy closure (capacity 512KiB, cpbm 16/32 bits) -----------
print("\n=== host-side CSU occupancy model check (L2 MSC, capacity 512KiB) ===")
writel(L2MSC + 0x100, 0)
cpbm2 = readl(L2MSC + 0x1000)
writel(L2MSC + 0x800, 0)
writel(L2MSC + 0x810, 0)
writel(L2MSC + 0x818, 0x43 | (1 << 16) | (1 << 31))
csu2 = readl(L2MSC + 0x840)
expect2 = 512 * 1024 * bin(cpbm2 & 0xFFFFFFFF).count("1") // 32
verdict2 = "OK" if csu2 == expect2 else "MISMATCH"
print(f"partid0 CPBM={cpbm2:#010x} ({bin(cpbm2).count('1')}/32 bits) "
      f"CSU={csu2} expect={expect2} [{verdict2}]")

# --- L2 MSC MBWU sanity ----------------------------------------------------
print("\n=== host-side L2 MSC MBWU counter sample ===")
writel(L2MSC + 0x800, 0)
writel(L2MSC + 0x820, 0)                      # MBWU_FLT: partid 0
writel(L2MSC + 0x828, 0x42 | (1 << 16) | (1 << 31))   # TYPE_MBWU|MATCH_PARTID|EN
def rd64(a):
    return readl(a) | (readl(a + 4) << 32)
a = rd64(L2MSC + 0x880)
time.sleep(1)
b = rd64(L2MSC + 0x880)
print(f"L2 MBWU: {a} -> {b} (delta {b - a})")

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

def qmp(**kw):
    qm.sendall(json.dumps(kw).encode() + b"\n")
    return qmp_recv(qm)

qm = socket.socket(socket.AF_UNIX)
qm.connect(QMP)
qmp_recv(qm)
qmp(execute="qmp_capabilities")

r = qmp(execute="query-mpam-cache")
infos = r.get("return", [])
print(f"\n=== cache MSCs via QMP: {len(infos)} ===")
for info in infos:
    for regs in info["regs"]:
        cps = regs["cfg-part-sel"]
        cpbm = [w for bm in regs["cfg-cpbm"] for w in bm["words"]]
        print(f"  L{info['level']} cpu{info['cpu']}: cfg_part_sel={cps:#x} cpbm={cpbm}")

proc.terminate()
print("\nVERIFICATION ARTIFACTS: guest log has driver messages + resctrl results;")
print(f"host-side non-default MSC registers: L3={nd_l3} L2={nd_l2} mem={nd_mem}")
