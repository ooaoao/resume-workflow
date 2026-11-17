# AI 简历工作流

一个在自己的电脑上用的简历工具。它把岗位原文、个人资料、简历版本和交付记录放在独立的数据目录里，让你或你选用的 AI 助手可以查岗位、接着改稿、生成单页 A4 PDF，并在关闭后继续工作。

- **筛岗位**：导入手头的岗位原文，保存有据可查的资格判断和选择。
- **做简历**：按任务保存稿件、依据和版本，使用本地浏览器排版并生成 PDF。
- **接着做**：重新打开早先的任务和记录，按需要创建保存点或备份。

示例完全使用虚构资料，不需要模型账号、浏览器登录或招聘网站。工具本身不调用在线模型，也不会替你投递；使用你选的 AI 客户端时，请按该客户端的数据设置处理真实资料。

[查看服务运营示例](examples/outputs/服务运营_虚构样例.pdf) · [查看数据质量示例](examples/outputs/数据质量_虚构样例.pdf)

## 在 Windows 上试用

需要 Python 3.11 或更新版本。生成 A4 和 PDF 时还需要 Node.js 22+，以及已安装的 Chrome 或 Microsoft Edge。将源码解压到一个普通目录，在该目录中打开 PowerShell：

```powershell
python -m venv .venv
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $py -m pip install -c .\constraints-validated.txt .
& $py -B -m resume_workflow --help
```

以下命令运行一份虚构案例。用尚未存在的演练目录；想跑另一份时，将两处 `operations` 改为 `data`。

```powershell
$lab = Join-Path (Split-Path -Parent $PWD.Path) 'resume-demo'
$fonts = Join-Path $PWD.Path 'examples/resources/LXGWWenKaiGBLite'
& $py -B examples/synthetic_demo.py --case operations --fonts $fonts --run-root (Join-Path $lab 'operations')
```

结果写在演练目录的 `result.json`。用其中的 `pdf` 路径打开生成的 PDF。找不到浏览器时，可以给示例脚本加上 `--chrome` 和本机浏览器程序的路径。

## 使用自己的资料

在程序目录之外准备单独的数据目录。真实简历、照片、姓名联系方式和岗位记录留在那里；不要放进源码目录或 GitHub。

```powershell
$dataRoot = Join-Path (Split-Path -Parent $PWD.Path) 'resume-data'
New-Item -ItemType Directory -Path $dataRoot -ErrorAction Stop | Out-Null
& $py -B -m resume_workflow --data-root $dataRoot workspace init
& $py -B -m resume_workflow workspace open
```

用 `material init 背景 --file <UTF-8 文件路径>` 接收背景，兴趣和偏好同理。需要 AI 协助时，让它先读 [AGENTS.md](AGENTS.md)，然后打开已配置的工作区。迁入旧资料只选择你打算使用的业务文件，浏览器配置和缓存留在原处。迁入和完整备份都不能用来给资料脱敏。

- [日常工作流](docs/日常工作流.md)：导入岗位、筛选、改稿、打印和恢复。
- [网申可选接入](docs/网申可选接入.md)：本地记录如何接到自行配置的浏览器流程。
- [第三方资源](docs/第三方资源.md)：字体、依赖与各自的许可。

## 许可证与反馈

本项目的原创代码和虚构示例采用 [GNU AGPL v3](LICENSE)。简历排版参考并改编自 [Kami](https://github.com/tw93/Kami)，保留 Kami 的[原始版权和 MIT 许可证](LICENSE-KAMI.txt)。示例字体采用 [SIL Open Font License 1.1](examples/resources/LXGWWenKaiGBLite/OFL.txt)，其他依赖仍依各自许可。项目许可证不要求公开你用工具处理的个人资料。

欢迎说明问题或建议改进。复现时请使用 `examples/` 中的虚构数据，或提供人工构造的最小示例；别附真实简历、账号或带私有路径的日志。
