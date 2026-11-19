# ZLH74GiR 固件改造工具（Tkinter GUI）

把 ZLH74GiR（长虹 hi3751v551 平台）海思 LOAD 升级包的改造流程，封装成可点选操作的中文图形工具。

核心算法均基于对真实固件包的镜像级分析与真机刷写验证：LOAD 包解析、Android sparse 的 **1:1 原位重组**、
ext4 就地编辑（extents / 线性目录 / gdt_csum）、LOAD 重打包与全面校验。GUI 只负责参数收集与流程编排。

> 本工具面向**在自己拥有的设备上**做固件研究与精简，不提供任何固件本体。

---

## 功能

| 能力 | 说明 |
|---|---|
| **解包** | 导出 LOAD 包内 17 项分区 payload 与表后签名段；把 system payload（sparse）展开为 ext4 |
| **系统精简** | 直接扫描镜像 `/app` 下的应用（含从 APK 提取图标），勾选后原地改名/置零，可选择释放 `.apx` 占用的数据块 |
| **root / ADB / telnetd** | 改写 `build.prop`、注入 `init.bigfish.sh`、开机启动 busybox telnetd，提供可用的 root 通道 |
| **预置 APK** | 支持本地多选 APK，或从 HTTP(S) 直链批量下载后预置；可写入 `/priv-app`，或复用应用商店槽位原地覆写 |
| **打包** | 按原厂 sparse chunk 布局逐块复刻 system payload，重算 CRC 后重打包 LOAD，包体积保持不变 |
| **校验** | 包大小、文件头、17 项分区表、签名段逐字节比对；每项 payload 的 CRC32/MPEG-2；sparse chunk 结构；反解 system 的 SHA256 |

---

## 从源码运行

```bash
git clone <仓库地址>
cd zlh74gir-fw-gui
python main.py
```

- 需要 **Python 3.11**（Tkinter 随官方安装包自带，勾选 tcl/tk 即可）。
- **零第三方依赖**：CRC32/MPEG-2 与 ext4 gdt_csum（CRC16/ARC）均为标准库 + 查表实现，不需要 `crcmod`。
- 支持 Windows / Linux / macOS；主流程在 Windows 上验证。

---

## 打包成 Windows exe（PyInstaller onedir）

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --onedir `
  --name ZLH74GiR-Firmware-GUI --icon assets\app.ico --add-data "assets;assets" main.py
Copy-Item -Recurse -Force assets dist\ZLH74GiR-Firmware-GUI\assets
```

- 产物在 `dist\ZLH74GiR-Firmware-GUI\`（约 27.5 MB / 989 个文件），分发时**整个文件夹一起拷贝**。
- 目标机无需安装 Python：`python311.dll`、Tcl/Tk 等依赖都在同目录 `_internal\` 内。
- onedir 形态启动时不解包到 `%TEMP%`，不产生 `_MEI*` 目录，冷启动明显快于 onefile。
- 仓库已附带 `ZLH74GiR-Firmware-GUI.spec`，也可直接 `python -m PyInstaller ZLH74GiR-Firmware-GUI.spec`。

---

## 使用流程

1. 顶部选择源升级包（下方各页路径按 `<包目录>\work\...` 自动推导，可逐项修改）
2. **解包** → 查看包信息 → 一键解包（导出 + 展开 system）
3. **系统精简** → 扫描镜像应用 → 勾选要删的应用 → 执行精简（如需预置到 `/priv-app`，保持「释放块」勾选）
4. **root / ADB / 预置** → 勾选系统选项 → 添加本地或链接 APK → 执行
5. **打包** → 确认输入/输出 → 执行打包
6. **校验** → 开始校验，全部通过后再刷机

### 界面约定

- 启动时**所有路径输入框为空**，只显示灰色占位「未选择」，不指向任何写死目录。
- 按钮状态与流程联动：必填路径为空、文件不存在、未勾选任何应用时，执行按钮自动禁用并给出原因。
- 精简、root/预置、覆盖输出文件前都会二次确认并列出将要执行的内容。
- 结果栏常驻显示上次结果：成功为绿色摘要与耗时，失败为红色并直接写明原因（完整堆栈在日志里）。
- 所有耗时操作运行在后台线程，界面不卡死。

---

## 关键约束（改代码前必读）

- **严格 1:1 复刻原厂 sparse 结构**：chunk 数量、类型、块数、顺序、总长度必须与原厂一致，禁止在末尾追加填充。
  否则真机升级器会在写入阶段失败（进入「我挂了」恢复菜单）。
- **DONT_CARE 区间必须保持全零**：重组时会逐块检查，发现非零字节立即报错中止。
- **不要"新增文件后普通打包"**：新块会落到原厂未分配的 DONT_CARE 区间，刷机时被升级器清零。
  新增文件只能走「释放已精简块 + 白名单分配」。
- **头部、17 项分区表、表后 256B 签名段原样保留**，只替换 system payload 并重算分块 CRC。
- 受系统分区容量限制，`/priv-app` 预置的空间全部来自第 ② 步释放的块，空间不足会明确报错。

### 性能参考

1.0 GB 原厂包（system payload 758 MB）、同一台机器的全流程耗时：

| 步骤 | 优化前 | 优化后 |
|---|---|---|
| 展开 sparse | 0.63 s | 0.54 s |
| sparse 1:1 重组 | 23.68 s | 0.81 s |
| LOAD 重打包 | 80.29 s | 2.40 s |
| 成品校验 | 123.99 s | 4.12 s |
| 包 CRC 逐项校验 | 106.20 s | 2.08 s |
| **合计** | **334.81 s** | **9.96 s（约 33.6 倍）** |

关键手段：`zlib.crc32` + 位反转查表替代逐位 CRC；重打包/校验改为流式单遍读写；DONT_CARE 补零复用 4 MB 缓冲区。

---

## 风险提示与免责声明

- **刷机有变砖风险。** 引导器是否强制验签取决于芯片 OTP，无法从固件包判定。本方案保持「头部 + 分区表」不变以兼容签名只覆盖头部的实现；
  若该机启用 secure boot 且校验覆盖 payload，刷入仍会被拒绝。
- **刷机前务必备份原厂包**，用于回滚；并确认已理解恢复菜单（「我挂了」）的用法。
- 所有结论来自镜像级分析与真机验证，但**不构成任何明示或暗示的担保**。
- 请仅对自己拥有的设备使用本工具，并遵守当地法律法规与设备保修条款。因使用本工具造成的设备损坏、数据丢失或保修失效，作者不承担责任。

---

## 隐私

- 源码中不含任何硬编码的本地绝对路径、用户名、计算机名或内网地址；运行期所有路径均由用户在界面中选择。
- 仓库通过 `.gitignore` 排除全部固件/镜像/APK、`dist/`、`build/`、`work/`、日志与虚拟环境，
  不含任何厂商固件或第三方 APK 内容。
- 预置 APK 的下载功能只访问用户填写的 URL，不做任何遥测或回传。

---

## 目录结构

```
zlh74gir-fw-gui/
├── main.py                     入口（Tkinter 界面）
├── assets/
│   ├── app.ico                 exe / 窗口图标（多尺寸）
│   └── app.png                 窗口图标（Tk iconphoto）
├── fwcore/
│   ├── zlh_load.py             LOAD 包解析 / 校验 / 导出
│   ├── sparse.py               sparse 展开、按原厂布局 1:1 重组、结构校验
│   ├── ext4fs.py               ext4 就地编辑器（extents / 线性目录 / gdt_csum / 块白名单）
│   ├── system_edit.py          精简、build.prop、init 注入、APK 预置、/app 应用扫描
│   ├── apkicon.py              从 APK 的 AXML + resources.arsc 提取应用图标
│   ├── downloader.py           APK 链接下载与合法性校验
│   ├── repack.py               LOAD 重打包
│   └── verify.py               成品包全面校验
├── ZLH74GiR-Firmware-GUI.spec  PyInstaller 构建脚本
└── README.md
```

运行期中间文件默认写在所选固件包同目录的 `work/`（`extract/`、`build/`、`out/`、`apk/`），可随时删除。

---

## 许可证

本项目以 **GNU General Public License v3.0** 发布，完整协议文本见 [LICENSE](LICENSE)。

你可以自由使用、修改和再分发，但衍生作品必须以同一许可证开源并提供对应源码。
