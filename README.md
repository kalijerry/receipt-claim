# receipt-claim · 报销助手

出差报销小工具：拍下票据，自动识别金额和日期，逐单人工核对后，一键生成改好名的票据、带公式的 Excel 报销表和 A4 扫描版 PDF。

本地运行，票据不会上传到任何服务器。支持 macOS 和 Windows，两个系统共用同一份代码（同一个分支）。

## 环境要求
| | macOS | Windows |
|---|---|---|
| 系统 | macOS 12 及以上 | Windows 10 / 11 |
| Python | 3.10 及以上 | 3.10 – 3.13 |
| 文字识别 / 扫描 | 系统自带 Vision / Core Image，需要 Xcode Command Line Tools（`swiftc`）：`xcode-select --install` | 纯 Python：RapidOCR 的两个互相独立的识别模型 + OpenCV；第一次启动时在这台电脑上实测 CPU、DirectML（NVIDIA / Intel / AMD 显卡）、OpenVINO（CPU 或 Intel 核显），选最快且识别结果与 CPU 一致的，通常比单纯 CPU 快一倍 |
| 首次安装 | 自动安装 [uv](https://github.com/astral-sh/uv)（Python 包管理器）、建虚拟环境、装依赖，并编译识别工具，需要联网，只此一次；之后完全离线 | 自动安装依赖（约 250 MB）并下载识别模型（约 30 MB），需要联网，只此一次；之后完全离线 |

## 下载即用
- **Windows（不需要装 Python）**：到 [Releases](https://github.com/kalijerry/receipt-claim/releases) 下载 `receipt-claim-windows-x64-v*.zip`，解压后双击 `报销助手.exe`。识别模型已包含在里面，全程不需要联网；票据和生成的报销包保存在 exe 旁边的 `output` 文件夹。没有代码签名，Windows 可能提示「已保护你的电脑」，点「更多信息」→「仍要运行」即可。
- **macOS**：到 [Releases](https://github.com/kalijerry/receipt-claim/releases) 下载 `receipt-claim-macos-v*.dmg`，挂载后把 `报销助手` 文件夹拖到任意位置，双击里面的 `启动报销助手.command`。第一次启动会自动装好 Python 依赖并编译识别工具（见下方「使用」）。没有 Apple 签名和公证，第一次打开会被 Gatekeeper 拦截，右键（或按住 Control 点按）选择「打开」即可，详见包里的 `使用说明.txt`。

## 使用（从源码运行）
- macOS：双击 `启动报销助手.command`
- Windows：双击 `启动报销助手.bat`

第一次会自动安装依赖（macOS 上先装 uv，再用它建虚拟环境、装依赖），之后浏览器会打开 http://127.0.0.1:8765

1. **新建出差**：一次出差 = 一个文件夹 `output/<出差名>/`，可以有多段日期
2. **报销日期**：每段包含首尾两天，总额度 = 总天数 × 每日额度（默认 £50）
3. **导入票据**：拖入照片（JPG / PNG / HEIC）或填文件夹路径（Windows 可直接粘贴资源管理器的「复制为路径」）；自动识别金额和日期，重复照片自动排除
4. **逐单核对**：对照原图确认每一单（识别到的金额、日期会在图上框出来）。相关票据（总账单 + AA 收据）勾选后「关联为一单」，按个人实付计算。全部确认后才能导出，确认后再改动会自动变回待核对
5. **扫描版（可选）**：一键转黑白扫描件，并重新识别扫描件，确认金额、日期一致、内容没被裁掉
6. **生成报销包**：
   - `报销票据/`：按 `序号_金额_日期` 命名（序号即日期顺序），关联票为 `-1`、`-2`；未计入和期外的在 `未计入/`
   - `<出差名>_报销表.xlsx`：汇总（含公式）/ 明细（每单一行）/ 票据清单（每张图一行，含识别值和核对时间）
   - `扫描版票据/` 和 `<出差名>_扫描版.pdf`：首页汇总目录，后面按单号排列，按票据实际大小排版
   - `原始票据/`：原图备份（你自己的原图不会被改动）

Windows 上如果提示「文件正被其他程序占用」，是因为生成的 Excel / PDF 还开着，关掉再生成一次即可。

## 自己打包
Windows（PyInstaller 冻结成 EXE）：
```bat
packaging\build_exe.bat
```
需要联网和 Python 3.11 – 3.13，约 5 分钟。会在 `packaging\dist\报销助手\` 生成可直接运行的程序，并压缩成 `packaging\receipt-claim-windows-x64-v<版本>.zip`（约 170 MB，版本号在 `app/version.py`）。用 PyInstaller 的「文件夹」模式而不是单文件 exe：单文件每次启动都要把几百 MB 解压到临时目录，慢得多，也更容易被杀毒软件误报。

macOS（打包源码 + 启动脚本成 DMG）：
```bash
packaging/build_dmg.sh
```
会在 `packaging/dist/报销助手/` 生成文件夹，并打包成 `packaging/receipt-claim-macos-v<版本>.dmg`。macOS 版不冻结 Python、不预编译识别工具——DMG 里是源码，`启动报销助手.command` 会在接收者自己的电脑上第一次启动时自动安装 uv、建虚拟环境、装依赖并编译识别工具（见上方「使用」）。这个 DMG 没有签名也没有公证（没有 Apple 开发者账号），第一次打开会被 Gatekeeper 拦截，需要右键「打开」，详见 `packaging/使用说明_macos.txt`。

## 项目结构
```
app/
  server.py          本地 HTTP 服务（标准库），出差数据存于 output/<出差名>/trip.json
  core/extract.py    从识别结果中提取金额、日期、商户，并定位它们在图上的位置
  core/trip.py       期间、关联单、核对状态、文件命名、汇总
  core/scan.py       扫描版一致性检查、A4 排版、PDF
  core/excel.py      Excel（含公式）
  native/            所有与操作系统有关的部分，统一接口见 native/__init__.py
    macos.py         调用 ocr/*.swift 编译出的工具、sips、open
    windows.py       RapidOCR（双读者）+ OpenCV + Pillow（HEIC 用 pillow-heif）
    devices.py       第一次启动时实测各种运行方式（CPU / DirectML / OpenVINO），选最快且结果一致的
  ocr/ocr.swift      macOS：Vision 文字识别
  ocr/scan.swift     macOS：找边、透视矫正、增强
  static/index.html  界面
  bench/             准确度评测（run.py）、退化副本（stress.py）、合成票据（synth.py）
packaging/           打包：build_exe.bat / receipt-claim.spec（Windows EXE），build_dmg.sh（macOS DMG）
  tests/
启动报销助手.command   macOS 启动器
启动报销助手.bat       Windows 启动器
```

## 识别是怎么做到准确的
金额和日期最终都由人核对，所以最重要的是不要出现「识别错了却没有任何提示」。几条做法：
- **两个独立的读者（Windows）**：PP-OCRv5 英文模型和 PP-OCRv4 中文模型各读一遍。两者一致才算有把握，不一致就在核对窗口里提示，并把另一个读到的金额 / 日期放在一键可选的位置。实测：英文模型把票面日期读出率从 53% 提到 100%，旧中文模型在部分照片上金额更准，合在一起互相兜底。
- **日期要对得上别的证据**（macOS 和 Windows 共用）：照片的拍摄时间、这次出差的日期段、「不会是三年前」。票面日期和拍摄日期相差一天以上、或不在出差期内，会给出提示。曾有一张票据 macOS 因为没读出日期而用了拍摄日期，实际票面印的是前一天，这个检查能抓到这类错误。
- **容忍 OCR 的常见笔误**：`050CT2026`（O 被读成 0）、`15/08/202616:56`（日期和时间粘在一起）、`9 Oct:'26`、`0ct`、`OC1`（T 被读成 1）、`f11.50`（£ 被读成 f）。
- **金额按「有标签的行」计分**：商品单价重复出现不再压过「BALANCE DUE」和刷卡行上的实付金额；`VISACONTACTLESS` 这类粘在一起的标签也能识别。
- 评测方法见下面「准确度评测」；`bench/run.py from-trip` 可以直接拿你已经核对过的出差当标准答案。

## 为什么两个系统共用一个分支
除了 `native/` 里的几个函数（文字识别、扫描增强、预览转换、打开文件夹、PDF 字体），其余约 90% 的代码——识别结果的解析规则、关联单与核对逻辑、Excel、PDF 排版、界面——与系统无关。分成两个长期分支，每次改解析规则或界面都要在两边各改一遍；共用一份代码，只需让 `native/` 两个后端输出同样格式的结果：

- `ocr(paths)` → 每张图的文字行，含归一化的位置框（左上角为原点）和 EXIF 拍摄日期
- `scan_image(src, out, mode)` → 扫描件，`mode` 为 `crop` / `box` / `enhance`
- `make_preview` / `open_folder` / `PDF_FONTS`

新增系统功能时，在这个接口上加函数并在两个后端各实现一次。Windows 的识别引擎会把一行文字切成几段、照片倾斜时同一行的几段高度不同，所以它额外给每段文字一个「摆正后」的位置 `rbox`，`core/extract.py` 的 `build_rows` 优先用它来拼行；macOS 不提供 `rbox`，行为不变。

## 开发与测试
macOS：
```bash
cd app
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
swiftc -O ocr/ocr.swift -o ocr/ocr && swiftc -O ocr/scan.swift -o ocr/scan
.venv/bin/python -m unittest discover -s tests -v
```
Windows（PowerShell，Python 3.10 – 3.13）：
```powershell
cd app
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m unittest discover -s tests -v
```
`tests/` 里与系统有关的测试（Windows 后端）会在其他系统自动跳过。

用真实票据做回归测试（可选）：把照片放进 `DATA/`，再写一个 `DATA/truth.json`：
```json
{"<文件名前 8 个字符>": {"amount": 15.81, "date": "2026-09-28", "warn": "可选：期望出现在提示里的文字"}}
```
没有这个文件时，相关测试会自动跳过。

### 准确度评测
改识别引擎、预处理或 `core/extract.py` 之前和之后，都用同一批票据跑一次评测（两个系统都能跑）：
```bash
python bench/run.py run --stress --scan --tag 改动前     # 真实票据 + 退化副本（旋转/梯形/模糊/压缩/欠曝/缩小）+ 扫描一致性
python bench/run.py compare ../output/bench/改动前.json ../output/bench/改动后.json --strict
python bench/run.py synth                                 # 没有真实票据时，生成合成票据做冒烟测试
python bench/run.py from-trip <出差文件夹>                 # 用已核对过的出差生成 truth.json，并给当时的识别结果（如 Vision）打分
python bench/run.py from-trip <出差文件夹> --reextract    # 把其中保存的文字行交给当前的 extract 重新提取：不用 Mac 也能检查共用规则有没有让 Vision 的结果退步
```
`truth.json` 里每条还可以写 `"merchant"`。最重要的指标不是命中率，而是**误报高把握**：识别错了、却没有任何提示的票据——这类错误最容易漏过人工核对。`compare --strict` 在出现退步或新增「无提示错误」时返回非零退出码。结果保存在 `output/bench/`（已被 `.gitignore` 忽略）。合成票据只能做冒烟测试，不能代替真实照片。

## 隐私
`DATA/` 和 `output/` 已在 `.gitignore` 中，里面是真实票据（含卡号后四位、商户、地址），不要提交。
