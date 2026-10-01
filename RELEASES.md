# 下载 / Releases

当前正式版本：[v0.9.4](https://github.com/kadevin/ilab-conjure/releases/tag/v0.9.4)

## 版本说明

当前版本：`v0.9.4`。本版改善任务停止、并发任务状态与工作台布局，新增供应商模型查询和可选的 Fake-IP 图片 DNS 兼容。建议使用中转站、多图并发或紧凑工作台的用户升级。

受影响平台：macOS 与 Windows 的标准版和 portable 一键包，以及桌面和手机 WebUI。

必要操作与数据迁移：升级前退出旧实例，升级后重启应用并刷新已打开的页面。无需迁移本地任务、图片或设置。Fake-IP 图片 DNS 兼容默认关闭，仅在遇到相应图片链接解析失败时按需启用；Windows 标准 ZIP 仍需手动替换应用文件。

本版详情：

### P1 · 重要

#### 新增

- **可选的 Fake-IP 图片 DNS 兼容。** 网络设置中可以为返回 Fake-IP 地址的图片下载域名单独查询真实地址，无需修改整个局域网的 DNS 模式。启用后按需向 Cloudflare DNS 查询图片域名，继续校验目标地址和重定向，不向该 DNS 服务发送提示词或 API Key。

#### 修复

- **停止任务及时中断本地请求。** 修复点击停止后仍等待图片请求或重试结束的问题；现在主动中断进行中的请求和同任务的并发图片请求，保留已生成结果，连接清理完成后释放并发位并继续后续任务。修复停止恰好发生在任务完成时状态滞留的问题。

### P2 · 常规

#### 新增

- **供应商设置支持“获取可用模型”。** 使用当前填写的连接参数查询 OpenAI-compatible 或 Gemini 模型列表，选择返回的模型 ID 填入绑定。无需先保存供应商；查询本身不会保存设置，地址、密钥或协议变化后旧查询结果会失效。

#### 修复

- **并发多图任务保持正确的逐图状态。** 修复删除已完成任务后，其他多图任务显示成单图生成的问题；活动任务从队列状态恢复完整的图片数量和进度。
- **删除旧任务保留浏览位置。** 修复删除或归档左侧非最新任务后滚动条跳到顶部的问题，保留当前展开分组、已加载任务和可见位置。
- **进入历史库保留未提交输入。** 修复从生成页进入历史库时出现离开网站警告的问题；同标签页跳转前临时保留提示词、参考图片和文件，返回后恢复。从历史库复用任务或添加参考输入时，原输入仍可从“恢复草稿”找回。
- **纯文生图不再刷新最近上传。** 修复没有提交参考图时点击生成仍触发最近上传加载动画和多余请求的问题；实际提交参考图时仍会更新列表。
- **工作台高度与底部操作恢复稳定。** 修复桌面布局被多余最小高度撑开、提示词底部按钮超出显示范围的问题，保留窄屏内容的正常滚动。
- **预设与自定义尺寸保持输出设置等高。** 修复切换到自定义尺寸时下方组件被向下挤压的问题，并在窗口尺寸或语言变化后重新适配。普通参数输入不再反复测量布局。
- **紧凑模式自定义尺寸不再重叠。** 宽高与比例输入分区排布，移除多余分隔线，避免控件互相覆盖。

### P3 · 低影响

#### 变更与优化

- **新设置补齐越南语文案。** 模型查询按钮、查询反馈和 Fake-IP DNS 说明与主语言保持一致。
- **提示词区域重新整理。** 宽屏保留编辑框右侧的大尺寸生成按钮，字数紧邻标题，恢复草稿入口保留；本次生成参数移到右上角，长内容提供完整悬停提示。
- **底部操作按钮统一。** 提示词的清空、查找恢复与参考输入一致的按钮样式，两块面板底部按钮高度统一，保留管理模板库的主题颜色。
- **减少重复说明。** 移除预览结果下方重复的“所选任务”摘要；直接使用图像模型的说明改为较轻的辅助文字，避免挤压输出设置标题。

#### 兼容性/安装/打包/更新

- **离线应用缓存与本版资源同步。** 更新主页面、历史库和样式的缓存清单，避免刷新或离线使用时混用旧界面资源。

#### 工程与文档

- 补充模型查询、DNS 地址安全、任务取消、并发状态、滚动位置、提交请求、历史库草稿恢复和布局测量的回归验证；网络兼容选项纳入设置备份与恢复。修正模拟供应商测试图片，使其通过与真实生成结果相同的严格校验。

#### 已知问题

- 停止任务会中断本地请求；供应商仍可能继续已经接收的生成并计费，取决于其取消支持。
- Fake-IP 图片 DNS 兼容需要能访问 Cloudflare DNS；供应商未提供有效图片内容或链接时，该选项无法修复返回数据。
- 大尺寸 GIF 动图仍使用原始动态预览，连续导入时可能出现短暂停顿。

## 推荐下载

| 平台 | 推荐给 | 下载 | SHA256 |
| --- | --- | --- | --- |
| macOS Apple Silicon | 新用户，M1/M2/M3/M4 | [iLab-GPT-CONJURE-macos-arm64-0.9.4.dmg](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/iLab-GPT-CONJURE-macos-arm64-0.9.4.dmg) | [sha256](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/iLab-GPT-CONJURE-macos-arm64-0.9.4.dmg.sha256.txt) |
| macOS Intel | 新用户，Intel x64 | [iLab-GPT-CONJURE-macos-x64-0.9.4.dmg](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/iLab-GPT-CONJURE-macos-x64-0.9.4.dmg) | [sha256](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/iLab-GPT-CONJURE-macos-x64-0.9.4.dmg.sha256.txt) |
| Windows x64 | 新用户，Windows 10/11 x64 | [iLab-GPT-CONJURE-windows-x64_0.9.4.zip](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/iLab-GPT-CONJURE-windows-x64_0.9.4.zip) | [sha256](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/iLab-GPT-CONJURE-windows-x64_0.9.4.zip.sha256.txt) |

标准包数据目录：

- macOS：`~/Library/Application Support/iLab GPT CONJURE/`
- Windows：`%APPDATA%\iLab GPT CONJURE\`

包含更新助手的 macOS 标准 App 会校验 signed `latest.json` 与 DMG SHA256，并在用户确认后自动覆盖、失败回滚和重新启动；`v0.6.1` 及更早的 macOS 标准 App 需要先手动安装当前版本一次，Windows 标准 ZIP 仍手动替换。

## 免安装一键包

| 平台 | 适用设备 | 下载 | SHA256 |
| --- | --- | --- | --- |
| Windows x64 | Windows 10/11 x64 | [ilab-gpt-conjure_windows_portable_x64_0.9.4.zip](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/ilab-gpt-conjure_windows_portable_x64_0.9.4.zip) | [sha256](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/ilab-gpt-conjure_windows_portable_x64_0.9.4.zip.sha256.txt) |
| macOS Apple Silicon | M1/M2/M3/M4 | [ilab-gpt-conjure_macos_portable_arm64_0.9.4.zip](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/ilab-gpt-conjure_macos_portable_arm64_0.9.4.zip) | [sha256](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/ilab-gpt-conjure_macos_portable_arm64_0.9.4.zip.sha256.txt) |
| macOS Intel | Intel x64 | [ilab-gpt-conjure_macos_portable_x64_0.9.4.zip](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/ilab-gpt-conjure_macos_portable_x64_0.9.4.zip) | [sha256](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/ilab-gpt-conjure_macos_portable_x64_0.9.4.zip.sha256.txt) |

portable 自动更新 manifest：

- [latest.json](https://github.com/kadevin/ilab-conjure/releases/download/v0.9.4/latest.json)

使用方式：

1. 下载对应平台的 zip。
2. 解压到普通用户目录，不要放在系统保护目录。
3. Windows 双击 `Start iLab GPT CONJURE.exe`；macOS 双击
   `Start iLab GPT CONJURE.app`。旧的 `Start WebUI Portable.bat` /
   `Start WebUI Portable.command` 仍保留，用于终端调试。
4. 如果浏览器没有自动打开，访问 `http://127.0.0.1:8787/`。

一键包启动器不会后台自动访问 GitHub。更新已经解压的一键包时，可在托盘 / 菜单栏
菜单选择检查更新，并在发现新版本后确认 `安装更新`；也可以退出启动器后手动运行
Windows 的 `Update WebUI Portable.bat` 或 macOS 的 `Update WebUI Portable.command`。
更新脚本会读取带签名的 `latest.json`
manifest，先用启动器内置公钥校验 Ed25519 签名，再下载当前平台对应的最新
GitHub Release 资产，执行前显示所选资产和 manifest SHA256，校验下载 zip 的
SHA256，只替换一键包目录内由程序管理的文件，保留本地 `data/`，并把被替换文件备份到 `.backup/`。

macOS 标准 DMG 和 portable zip 都暂未使用 Apple Developer ID 签名，也未 notarize。如果 macOS
拦截启动，可以右键或 Control-click App，选择 Open，并在系统安全提示中再次确认。
portable zip 也可以对解压目录执行：

```bash
xattr -dr com.apple.quarantine /path/to/ilab-gpt-conjure_macos_portable_arm64
# 或：
xattr -dr com.apple.quarantine /path/to/ilab-gpt-conjure_macos_portable_x64
```

一键包内的 `data/` 目录会保存本地设置、公用图库、输入图、输出图、任务数据库和日志。
不要把这些本地数据、API key 或 OAuth 文件提交到 Git。
