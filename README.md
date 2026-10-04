# receipt-claim · 报销助手

出差报销小工具：拍下票据，自动识别金额和日期，逐单人工核对后，一键生成改好名的票据、带公式的 Excel 报销表和 A4 扫描版 PDF。

本地运行，票据不会上传到任何服务器。

## 环境要求
- macOS 12 及以上（文字识别和扫描用的是系统自带的 Vision / Core Image）
- Xcode Command Line Tools（提供 `swiftc`）：`xcode-select --install`
- Python 3.10 及以上

## 使用
双击 `启动报销助手.command`（第一次会自动安装依赖、编译识别工具），浏览器会打开 http://127.0.0.1:8765

1. **新建出差**：一次出差 = 一个文件夹 `output/<出差名>/`，可以有多段日期
2. **报销日期**：每段包含首尾两天，总额度 = 总天数 × 每日额度（默认 £50）
3. **导入票据**：拖入照片（JPG / PNG / HEIC）或填文件夹路径；自动识别金额和日期，重复照片自动排除
4. **逐单核对**：对照原图确认每一单（识别到的金额、日期会在图上框出来）。相关票据（总账单 + AA 收据）勾选后「关联为一单」，按个人实付计算。全部确认后才能导出，确认后再改动会自动变回待核对
5. **扫描版（可选）**：一键转黑白扫描件，并重新识别扫描件，确认金额、日期一致、内容没被裁掉
6. **生成报销包**：
   - `报销票据/`：按 `序号_金额_日期` 命名（序号即日期顺序），关联票为 `-1`、`-2`；未计入和期外的在 `未计入/`
   - `<出差名>_报销表.xlsx`：汇总（含公式）/ 明细（每单一行）/ 票据清单（每张图一行，含识别值和核对时间）
   - `扫描版票据/` 和 `<出差名>_扫描版.pdf`：首页汇总目录，后面按单号排列，按票据实际大小排版
   - `原始票据/`：原图备份（你自己的原图不会被改动）

## 项目结构
```
app/
  server.py          本地 HTTP 服务（标准库），出差数据存于 output/<出差名>/trip.json
  core/extract.py    从识别结果中提取金额、日期、商户，并定位它们在图上的位置
  core/trip.py       期间、关联单、核对状态、文件命名、汇总
  core/scan.py       扫描版一致性检查、A4 排版、PDF
  core/excel.py      Excel（含公式）
  ocr/ocr.swift      Vision 文字识别
  ocr/scan.swift     找边、透视矫正、增强
  static/index.html  界面
  tests/
```

## 开发与测试
```bash
cd app
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
swiftc -O ocr/ocr.swift -o ocr/ocr && swiftc -O ocr/scan.swift -o ocr/scan
.venv/bin/python -m unittest discover -s tests -v
```

用真实票据做回归测试（可选）：把照片放进 `DATA/`，再写一个 `DATA/truth.json`：
```json
{"<文件名前 8 个字符>": {"amount": 15.81, "date": "2026-09-28", "warn": "可选：期望出现在提示里的文字"}}
```
没有这个文件时，相关测试会自动跳过。

## 隐私
`DATA/` 和 `output/` 已在 `.gitignore` 中，里面是真实票据（含卡号后四位、商户、地址），不要提交。
