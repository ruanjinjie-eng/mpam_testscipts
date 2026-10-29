#!/usr/bin/env python3
"""Register-level verification of the QEMU MPAM MSC emulation, v2:
narrow PARTID (v1) plus PRI / CMIN / CASSOC / CSU occupancy."""
import json, os, socket, subprocess, sys, time

MPAM_HOME = os.environ.get("MPAM_HOME", "/home/r00799764/community")
QEMU = os.environ.get("QEMU_BIN",
    os.path.join(MPAM_HOME, "qemu", "build", "qemu-system-aarch64"))
DIR = os.path.join(MPAM_HOME, "mpam_qemu_test_v2", "logs")
QTEST = f"{DIR}/qtest.sock"
QMP = f"{DIR}/qmp.sock"
LOG = f"{DIR}/qemu_test.log"

os.makedirs(DIR, exist_ok=True)
for p in (QTEST, QMP):
    if os.path.exists(p):
        os.unlink(p)

args = [
    QEMU,
    "-M", "virt,mpam=on,gic-version=3",
    "-cpu", "max", "-m", "1G", "-smp", "2",
    "-numa", "node,memdev=m0",
    "-object", "memory-backend-ram,size=1G,id=m0",
    "-S", "-display", "none", "-no-reboot",
    "-qtest", f"unix:{QTEST},server,nowait",
    "-qmp", f"unix:{QMP},server,nowait",
]
logf = open(LOG, "wb")
proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT)


def wait_sock(path, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(path)
                return s
            except (FileNotFoundError, ConnectionRefusedError):
                pass
        if proc.poll() is not None:
            print("QEMU DIED, log tail:")
            print(open(LOG, errors="replace").read()[-3000:])
            sys.exit(1)
        time.sleep(0.1)
    print(f"TIMEOUT waiting for {path}")
    print(open(LOG, errors="replace").read()[-3000:])
    proc.terminate()
    sys.exit(1)


qs = wait_sock(QTEST)
qm = wait_sock(QMP)

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
def writeq(a, v): qt(f"writeq {a:#x} {v:#x}")
def writel(a, v): qt(f"writel {a:#x} {v:#x}")

def qmp_recv(sock):
    buf = b""
    while True:
        d = sock.recv(65536)
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

qmp_recv(qm)                       # greeting
qmp(execute="qmp_capabilities")    # handshake

MSC = 0x0b006000
PART_SEL   = MSC + 0x100
MBW_MAX    = MSC + 0x208
INTPARTID  = MSC + 0x600

failures = []
def check(name, got, want):
    ok = got == want
    if not ok:
        failures.append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: got {got:#x} want {want:#x}")

print("=== MPAMF_IDR / PARTID_NRW_IDR (MEM MSC, 256 partid / 32 int-partid) ===")
idr = readq(MSC + 0x000)
check("IDR.PARTID_MAX",       idr & 0xFFFF,        255)
check("IDR.HAS_MBW_PART",    (idr >> 26) & 1,     1)
check("IDR.EXT",             (idr >> 28) & 1,     1)
check("IDR.HAS_PARTID_NRW",  (idr >> 31) & 1,     1)
check("IDR.NO_IMPL_PART",    (idr >> 36) & 1,     1)
check("IDR.HAS_ESR",         (idr >> 39) & 1,     1)
check("IDR.NO_IMPL_MSMON",   (idr >> 37) & 1,     0)
check("IDR.HAS_MSMON",       (idr >> 30) & 1,     1)
check("IDR.HAS_RIS",         (idr >> 32) & 1,     0)
mbw_idr = readl(MSC + 0x040)
check("MBW_IDR.BWA_WD",       mbw_idr & 0x3F,      16)
check("MBW_IDR.HAS_MIN|MAX", (mbw_idr >> 10) & 3, 3)
check("MBW_IDR.BWPBM_WD",    (mbw_idr >> 16) & 0x1FFF, 4)
nrw = readl(MSC + 0x050)
check("PARTID_NRW_IDR.INTPARTID_MAX", nrw & 0xFFFF, 31)

print("=== MSMON idrs ===")
msmon_idr = readl(MSC + 0x080)
check("MSMON_IDR.MSMON_MBWU", (msmon_idr >> 17) & 1, 1)
mbwumon_idr = readl(MSC + 0x090)
check("MBWUMON_IDR.NUM_MON", mbwumon_idr & 0xFFFF, 8)
check("MBWUMON_IDR.HAS_LONG", (mbwumon_idr >> 30) & 1, 1)

print("=== MBWU monitoring: counter tracks MBW_MAX ===")
import time
MON_SEL, MBWU_FLT, MBWU_CTL, MBWU_L = MSC + 0x800, MSC + 0x820, MSC + 0x828, MSC + 0x880

writel(MON_SEL, 0)                     # mon 0, ris 0
writel(MBWU_FLT, 5)                    # filter: partid 5
writel(MSC + 0x100, 5)                 # PART_SEL = 5
writel(MBW_MAX, 0xFFFF)                # 100% bandwidth for intpartid 5
writel(MBWU_CTL, 0x42 | (1 << 16) | (1 << 31))   # TYPE_MBWU | MATCH_PARTID | EN
def read64(a):                         # driver-style: two 32-bit reads
    return readl(a) | (readl(a + 4) << 32)

v1 = read64(MBWU_L)
time.sleep(1.0)
v2 = read64(MBWU_L)
check("mbwu advances at full rate", 5e9 < v2 - v1 < 4e10, True)

writel(MBW_MAX, 0x1999)                # ~10% bandwidth
v3 = read64(MBWU_L)
time.sleep(1.0)
v4 = read64(MBWU_L)
check("mbwu rate scales with MBW_MAX", v4 - v3 < (v2 - v1) // 4, True)

writel(MBWU_CTL, 0x42 | (1 << 16))     # EN = 0
v5 = read64(MBWU_L)
time.sleep(0.3)
check("mbwu frozen when disabled", read64(MBWU_L) == v5, True)
writel(MBWU_CTL, 0)

print("=== narrow PARTID: default map (partid %% 32) shares configs ===")
writel(PART_SEL, 5)
writel(MBW_MAX, 0xAAAA)
check("partid5 MBW_MAX set", readl(MBW_MAX) & 0xFFFF, 0xAAAA)
writel(PART_SEL, 37)                      # 37 % 32 == 5
check("partid37 shares intpartid5 cfg", readl(MBW_MAX) & 0xFFFF, 0xAAAA)

print("=== narrow PARTID: MPAMCFG_INTPARTID remap ===")
writel(PART_SEL, 37)
writel(INTPARTID, (1 << 16) | 6)          # INTERNAL|6: remap partid37 -> intpartid6
check("partid37 INTPARTID readback", readl(INTPARTID) & 0xFFFF, 6)
check("remapped partid37 reads fresh cfg (reset=full)", readl(MBW_MAX) & 0xFFFF, 0xFFFF)
writel(PART_SEL, 5)
check("partid5 cfg untouched", readl(MBW_MAX) & 0xFFFF, 0xAAAA)
check("partid5 default INTPARTID", readl(INTPARTID) & 0xFFFF, 5)

print("=== narrowing indirection routes cfg writes via map ===")
writel(PART_SEL, 37)
writel(MBW_MAX, 0xBBBB)                   # -> per_nrw_id[6]
check("partid37 cfg after remap", readl(MBW_MAX) & 0xFFFF, 0xBBBB)
writel(PART_SEL, 6)                       # default map: 6 % 32 == 6
check("partid6 (same intpartid) sees 0xBBBB", readl(MBW_MAX) & 0xFFFF, 0xBBBB)

print("=== guest-error bounds checks ===")
writel(PART_SEL, (15 << 24) | 5)          # ris 15 out of range (num-ris 1)
idr2 = readq(MSC + 0x000)                 # must not crash; sel rejected
check("OOR ris write rejected (still alive)", (idr2 >> 31) & 1, 1)
writel(PART_SEL, 0x1FF)                   # ris=0, partid 511 -> %256=255
check("part_sel clamped, INTPARTID=cfg[255]", readl(INTPARTID) & 0xFFFF,
      255 % 32)


print("=== v2: PRI on the memory-side MSC (backs the MBPRI pseudo-resource) ===")
pri_idr = readl(MSC + 0x048)
check("mem PRI_IDR.HAS_INTPRI",    pri_idr & 1,          1)
check("mem PRI_IDR.INTPRI_WD",     (pri_idr >> 4) & 0x3F, 2)
idr_all = readq(MSC + 0x000)
check("mem IDR.HAS_PRI_PART",      (idr_all >> 27) & 1,  1)
writel(MSC + 0x100, 0)
writel(MSC + 0x400, 0x00010002)                 # DSPRI:INTPRI = 1:2
check("mem PRI write/readback",    readl(MSC + 0x400),   0x00010002)

print("=== v2: MBW_MAX HARDLIM bit (backs MBHDL) ===")
writel(MSC + 0x100, 1)
writel(MBW_MAX, 0x80000025)                     # 25% + hard limit
mx = readl(MBW_MAX)
check("mem MBW_MAX.HARDLIM",       (mx >> 31) & 1,       1)
check("mem MBW_MAX value",         mx & 0xFFFF,          0x25)

L3 = 0x0b00a000
print("=== v2: L3 cache MSC CMIN / CASSOC / PRI (back L3MIN/L3MAX/L3PRI) ===")
writel(L3 + 0x100, 0)
writel(L3 + 0x110, 5)
check("L3 CMIN write/readback",    readl(L3 + 0x110) & 0xFFFF, 5)
writel(L3 + 0x118, 3)
check("L3 CASSOC write/readback",  readl(L3 + 0x118) & 0xFFFF, 3)
writel(L3 + 0x400, 2)
check("L3 PRI write/readback",     readl(L3 + 0x400) & 0xFFFF, 2)
writel(L3 + 0x100, 1)                           # per-partid isolation
writel(L3 + 0x110, 9)
check("CMIN is per-partid",        readl(L3 + 0x110) & 0xFFFF, 9)
writel(L3 + 0x100, 0)
check("CMIN partid0 untouched",    readl(L3 + 0x110) & 0xFFFF, 5)

print("=== v2: CSU occupancy = capacity x CPBM fraction (L3 MSC) ===")
msmon = readl(L3 + 0x080)
check("L3 MSMON_IDR.MSMON_CSU",    (msmon >> 16) & 1,    1)
csumon = readl(L3 + 0x088)
check("L3 CSUMON_IDR.NUM_MON",     csumon & 0xFFFF,      4)
writel(L3 + 0x800, 0)                           # MON_SEL: mon 0, ris 0
writel(L3 + 0x810, 0)                           # CSU_FLT: partid 0
writel(L3 + 0x818, 0x43 | (1 << 16) | (1 << 31))  # TYPE_CSU|MATCH_PARTID|EN
writel(L3 + 0x100, 0)
writel(L3 + 0x1000, 0x55555555)                 # 16/32 portions
check("CSU @ 50% CBM",             readl(L3 + 0x840),    16 * 1024 * 1024)
writel(L3 + 0x1000, 0xFFFFFFFF)                 # 32/32 portions
check("CSU @ 100% CBM",            readl(L3 + 0x840),    32 * 1024 * 1024)
writel(L3 + 0x818, 0x43 | (1 << 16))            # EN = 0
check("CSU disabled reads 0",      readl(L3 + 0x840),    0)

L2 = 0x0b00e000
print("=== v2: first L2 cache MSC has CSU + MBWU too ===")
msmon2 = readl(L2 + 0x080)
check("L2 MSMON_IDR.MSMON_CSU",    (msmon2 >> 16) & 1,   1)
writel(L2 + 0x800, 0)
writel(L2 + 0x820, 0)
writel(L2 + 0x828, 0x42 | (1 << 16) | (1 << 31))
def rd64(a):
    return readl(a) | (readl(a + 4) << 32)
c1 = rd64(L2 + 0x880)
time.sleep(0.5)
c2 = rd64(L2 + 0x880)
check("L2 MBWU advances",          0 < c2 - c1 < 2 ** 48, True)

print("=== QMP query-mpam-cache (cache MSC introspection) ===")
r = qmp(execute="query-mpam-cache")
infos = r.get("return", r.get("error", {}))
if isinstance(infos, list):
    print(f"cache MSCs: {len(infos)}")
    for info in infos:
        regs = info["regs"][0]
        cidr = regs["idr"]
        print(f"  level={info['level']} cpu={info['cpu']} "
              f"IDR.CPOR={(cidr >> 25) & 1} IDR.CCAP={(cidr >> 24) & 1} "
              f"IDR.NRW={(cidr >> 31) & 1} "
              f"NRW_IDR={regs['partid-nrw-idr'] & 0xFFFF:#x}")
    check("cache MSC count", len(infos), 4)
else:
    failures.append("query-mpam-cache")
    print("query-mpam-cache ERROR:", infos)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURES: {failures}")
    rc = 1
else:
    print("RESULT: ALL CHECKS PASSED")
    rc = 0
proc.terminate()
sys.exit(rc)
