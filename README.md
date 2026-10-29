# mpam_qemu_test_v2 — PRI/MIN/MAX + CDP + L2 monitor 验证包

在 v1（narrow PARTID，`../mpam_qemu_test/`）之上，验证 linux_old1 主线系驱动的新特性：

- **PRI / MIN / MAX 伪资源**（L3PRI/L2PRI/MBPRI、L3MIN/L2MIN/MBMIN、L3MAX/L2MAX、MBOPT、MBHDL）
  → QEMU 侧 MPAMCFG_PRI(0x400)、CMIN(0x110)、CASSOC(0x118)、MBW_MIN(0x200)、
  MBW_MAX.HARDLIM(bit31)、MBW_PROP(0x500)
- **CDP**（`mount -o cdp`，num_closids 32→16，L3CODE:/L3DATA: 独立 schemata）
- **L2 monitor**（`mount -o l2`：mbm_core_bytes + llc_occupancy）
- **CSU occupancy**（占用 = 容量 × CPBM 份数；L3=32MiB、L2=512KiB）

## 一键运行

```bash
cd mpam_qemu_test_v2
./run_all.sh                 # 默认 linux_old1 v7.3-rc6（mainline，全项必判）
./run_all.sh $MPAM_HOME/OLK-6.6/arch/arm64/boot/Image   # OLK 路径：v1 项必判，v2 项 INFO
```

mainline 流程用 `-cpu max`（SME 开启）——**这本身就是 MPAMSM_EL1 补丁的活体验证**
（v2 之前 v7.x 需要 `max,sme=off` 规避）。

## 依赖

QEMU 需含以下补丁（本仓库 `mpam-narrow-partid` 分支 `62b0494..fc4a55a`）：

| 补丁 | 内容 |
|---|---|
| `62b0494` | MPAMSM_EL1（FEAT_MPAM_SMS，S3_0_C10_C5_3）——v7.x SME 引导崩溃根治 |
| `ce61872` | CMIN/CASSOC 实现（原只宣告 HAS_CMIN/HAS_CASSOC 未实现）、mem MSC PRI（MBPRI 后端）、CSU 监控仿真 |
| `570fa8f` | MSC 错误/溢出中断接 GIC（per-MSC SPI 176+、ESR/ECR、ACPI 真实 GSIV、reset） |
| `2e1a54c` | 多拓扑修复：L2 per-cluster 分组分母（`smp.clusters` 是 per-socket 值）、mem MSC `node` 属性 + ACPI locator 修复 |
| `fc4a55a` | 修复 `per_nrw_id` 初始化越界（256 写 32 元素数组，曾致 `malloc(): corrupted top size`） |

补丁备份：`../mpam_qemu_test/qemu_patches/` 0001-0010（v1 的 7 个 + 上述 3 个）+
`0008-0010-all-in-one.diff`。

**内核必须构建在 linux_old1 ≥ 930dc5a（伪资源）/ 81b92c9（L2 monitor pick）之后**。
坑：Image 若早于这些提交构建，所有伪资源写入报 "Unknown or unsupported resource
name"、L2_MON 无 mon_features —— 是内核陈旧，不是 QEMU 问题（本次实测踩坑：
Image 10-14 15:18 构建，伪资源提交 17:14 才合入）。改动后记得
`make O=build/arm64 ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- Image`。

## 判定（mainline 必判项）

核心（v1 同款）：resctrl mount、num_closids=32、L3 位图写入+读回、MB 写入、host CPBM 闭环。
v2 新增：

- guest 12 项伪资源写入（L3PRI=2/L3MIN=25/L3MAX=50、L2PRI=1/L2MIN=25/L2MAX=50、
  MBPRI=1/MBMIN=25/MBOPT=8191/MBHDL=1）+ 读回
- host 侧寄存器闭环（判 e2e.log）：L3 MSC partid0 `PRI=2 CMIN=0x3000 CMAX=0x7000`、
  L2 MSC `PRI=1 CMIN=0x3000 CMAX=0x7000`、mem MSC `PRI=1 + HARDLIM=Y`、
  CSU 模型 L3 `16MiB` / L2 `256KiB` 均 `[OK]`
- guest `llc_occupancy=16777216`（L3）
- L2 monitor：资源/文件创建（`L2_MON/mon_features: l2c_occupancy mbm_core_bytes`、
  `mon_L2_*/` 域目录与文件）、host 侧 L2 CSU 模型、**guest 实数读回**
  （`l2c_occupancy=262144`、`mbm_core_bytes` delta>0；需内核 ≥ `69d324f68c12`，
  见"已知语义"L2 条目）
- CDP：mount OK、num_closids 32→16（读 `info/L3DATA/num_closids`；CDP 下 info/L3
  改名 L3DATA/L3CODE）、L3DATA 读回、卸载后重挂、`num_rmids` 64→32

## 已知语义（本轮实测确认）

- **CMIN/CMAX 是 band 编码**（cmax_wd=4 → 16 档）：写百分比经
  `round(pc*16/100)` 再 `max(,1)-1`，回读 `((band+1)*100)/16`。1%/8% 都落在 band0
  → 回读 6 —— 不是 bug。用 25/50 才能往返（→ raw 0x3000/0x7000）。
- MBOPT 写原始分数（0..0xffff），MB 写百分比，两者写同一 MBW_MAX 寄存器，
  最终值以最后一次写为准（MBOPT=8191 后 MB 回读 13）。
- monitor 域目录 id 沿用 `any_mon_comp` 的 id，本拓扑下 `mon_L3_33685504`
  （L2 的 id）而非 33751040 —— 判定用 glob `mon_L3_[0-9]*`。
- **L2 计数读回**：主线曾返回 "Unavailable"（`fs/resctrl/monitor.c` 的
  `domain_header_is_valid(..., RDT_RESOURCE_L3)` 硬编码 L3，b7e1d3d/81b92c9 只建了
  L2 monitor 资源/文件，读路径没跟上）。**已在 linux_old1 `69d324f68c12` 修复**
  （L2 走 per-domain 计数路径；mbm_core_bytes 每次 read 借一个 MBWU monitor），
  补丁备份在 `../mpam_qemu_test/qemu_patches/kernel_l2_read_fix/`。内核旧于该提交
  时仍会 "Unavailable" —— 是内核陈旧，不是 QEMU 问题。
- L3 MBM 走 ABMC 预分配计数（`mbm_assign_mode=[mbm_event]`），root 组即可读到
  mbm_total_bytes 增量（~20GB/2s）。
- CDP 需 `CONFIG_EXPERT=y`（linux_old1 已开）；CDP 下 monitor 计数文件名是
  `num_rmids`（复数）。
- init 顺序：CDP 在写矩阵**之前**（重挂会重置 schemata），host 侦察看到的才是最终写入值。

## 中断端到端测试（一次性，独立于 run_all.sh）

`scripts/irq_path_test.py` 验证 MSC 错误中断全链路（注入 → ESR → irq 线 → GIC →
guest handler → 清除）。**破坏性**：guest MPAM 驱动收到错误中断后按设计自禁用，
所以它不在 run_all.sh 里，单独跑：

```bash
# 需要一个 CONFIG_STRICT_DEVMEM=n 的内核（devmem 写 MSC 寄存器）
python3 scripts/irq_path_test.py [Image] [initrd.gz]   # 默认 /tmp/Image.irqtest + initrd/irqtest-initrd.gz
```

要点：
- guest init（`initrd/rootfs_irqtest/`）devmem 写 L3 MSC `PART_SEL`（RIS=15）触发
  Undefined_RIS_PART_SEL，随后复查 ECR/ESR、/proc/interrupts、dmesg；
- 判据：dmesg 出现 `error irq from msc:1 'Undefined_RIS_PART_SEL', partid:5`、
  post `ECR=0`（handler 关 INTEN）、ESR low=0（已清除）；
- 脚本自带 `-preconfig` + QMP `qom-set /machine smp-cache`（l1d/l1i=core、
  l2=cluster、l3=socket）—— **没有这一步 PPTT 不含 cache 节点，guest MPAM 驱动
  全部 probe 失败**（"Bad level (-2) for cache with id ..."），这是任何手工复现
  MPAM 引导时的通用前置条件；
- 证据：`logs/irq_path_test.log`。

## 无 NUMA 引导验证 + MPAM ACPI 表结构校验

`scripts/nonuma_boot_test.py` + `initrd/rootfs_nonuma/`（nonuma-initrd.gz，
脚本自动构建）：验证 cover letter 的 TODO「无 -numa 时默认单节点」（QEMU
补丁 0011，`hw/arm/virt.c` 隐式 node 0）：

```bash
python3 scripts/nonuma_boot_test.py [Image] [initrd.gz]
```

- **不带任何 -numa** 启动 `-M virt,mpam=on`（1 socket × 1 cluster × 4 cores）；
- 判据（40 项）：QEMU 不再报 "requires NUMA"、guest 仅 node0 且 **无 SRAT**
  （隐式节点不伪造 NUMA）、3 个 MSC（mem+L3+L2）、resctrl 挂载、
  num_closids=32、MB/L3 写入读回（mem MSC 经驱动 `pxm_to_node()` 回退解析到
  node 0）、6 个互异 GSIV；
- **附带 MPAM ACPI 表结构校验**（RFC TODO「ACPI table unit tests」的本地替身）：
  guest 全量 od dump MPAM 表，host 侧按 `drivers/acpi/arm64/mpam.c` 的解析
  逻辑校验——签名/长度/校验和、MSC 节点精确平铺、locator 类型与取值
  （mem=PXM 0、L3=0x2030000、L2=0x2020000）、中断 flags=0、reserved 清零；
- 坑：QOM 子设备枚举是**创建顺序的逆序**（mem MSC 的 identifier 最大），
  判定按 locator 类型识别角色，不能按顺序解包；busybox od 行尾最后字节
  无尾随空格，hexdump 提取用 split 而非带尾随空格的正则；
- 退出码：0=全过，1=有失败，2=QEMU 仍未实现（报 requires NUMA）；
- 证据：`logs/nonuma_boot_test.log`。

## 多拓扑验证（2 sockets × 2 clusters × 2 cores，2 NUMA）

`scripts/topo_boot_test.py` + `initrd/rootfs_topo/`（topo-initrd.gz）：验证
per-socket L3 / per-cluster L2 / per-NUMA mem MSC 的创建、ACPI locator、中断
分配与 guest 域数量（需 QEMU ≥ `2e1a54c`）：

```bash
python3 scripts/topo_boot_test.py [Image] [initrd.gz]
```

- 判据：8 个 mpam_msc 设备、8 条 `mpam:msc:error`（GSIV 208-223）、
  `SCHEMATA L3:33751040=…;33751044=…`（2 域）、`L2:33685504/06/08/10`（4 域）、
  mon_L3×2 + mon_L2×4；
- **已知限制（驱动侧）**：多 NUMA 节点下 MBA 资源不导出——主线 arm_mpam
  `traffic_matches_l3()` 对 `num_possible_nodes()>1` 与多 L3 直接拒绝（MBA 需
  与 L3 出口流量一一对应），非仿真缺陷；单 NUMA 拓扑（常规套件）不受影响；
- 证据：`logs/topo_boot_test.log`。

## OLK-6.6 路径观察

- OLK 驱动代际比预期新：`-o l2`/`-o cdp,l2` 均可挂载，CDP 下 num_closids 同样
  减半，且已有 L2 monitor（`l2c_occupancy`/`mbm_core_bytes`）与 `mon_MB_00` 域。
- OLK 上 v2 的伪资源只有 MBPRI 可写（其余不在该代驱动）；按判定策略记 INFO。
- **已知限制**：v2 流程做过 `-o l2`/CDP 挂载实验后，OLK 驱动的计数读回持续为空
  （即使重新净挂载）——OLK 驱动代际问题，非仿真缺陷。单挂载下的 25% 钳制测量由
  v1 包覆盖（`../mpam_qemu_test/run_all.sh` OLK 路径，delta≈5e9 PASS）。

```
run_all.sh                  一键入口（flavour 自动识别）
initrd/rootfs/init          guest 验证脚本（CDP→矩阵→采样→dmesg）
initrd/rootfs_irqtest/      中断注入测试的 guest init（devmem，一次性）
initrd/rootfs_nonuma/       无 -numa 引导 + MPAM ACPI 表 dump 的 guest init
scripts/mpam_qtest.py       寄存器级（无 guest）：v1 30 项 + PRI/CMIN/CASSOC/CSU/L2 约 20 项
scripts/olk_boot_verify.py  端到端编排 + host 侧 L3/L2/mem MSC PRI/容量/CSU 侦察
scripts/irq_path_test.py    MSC 中断端到端注入测试（一次性，见上节）
scripts/nonuma_boot_test.py 无 -numa 引导验证 + MPAM ACPI 表结构校验（见上节）
logs/                       产物
```
