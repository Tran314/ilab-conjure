# 下载 / Releases

当前正式版本：[v0.9.7](https://github.com/Tran314/ilab-conjure/releases/tag/v0.9.7)

## 版本说明

当前版本：`v0.9.7`。这是 Tran314 fork 基于上游 v0.9.6 的修复发行版，包含备份恢复安全、Windows 文件保护和模型默认供应商修复。

受影响平台：Windows x64、macOS Apple Silicon 与 Intel 的标准版和 portable 一键包。

必要操作与数据迁移：升级前退出旧实例。首次从上游发行版切换到本 fork 时，请手动下载完整安装包；后续签名更新使用 Tran314/ilab-conjure 的独立发布渠道。无需手动迁移任务、图片或设置，Windows 标准 ZIP 仍需手动替换应用文件。

本版详情：

- **阻止密钥被继承到其他服务地址。** 恢复不含密钥的配置备份时，仅在供应商 ID 和规范化 origin 均一致的情况下保留当前密钥。
- **修复 Windows 备份导入、导出与回滚。** 使用 Windows 原生文件句柄和受保护的 owner/SYSTEM 权限，拒绝符号链接与 junction 等重解析点，并清理失败的上传文件。
- **完整恢复模型默认供应商。** 增量和替换恢复均依据最终供应商集合补齐默认映射，写入前验证合并后的设置。
- **使用本 fork 的签名更新。** 标准版和 portable 更新地址指向 Tran314/ilab-conjure；更新清单继续验证 Ed25519 签名与安装包 SHA256。

## 推荐下载

| 平台 | 推荐给 | 下载 | SHA256 |
| --- | --- | --- | --- |
| macOS Apple Silicon | 新用户，M1/M2/M3/M4 | [iLab-GPT-CONJURE-macos-arm64-0.9.7.dmg](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/iLab-GPT-CONJURE-macos-arm64-0.9.7.dmg) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/iLab-GPT-CONJURE-macos-arm64-0.9.7.dmg.sha256.txt) |
| macOS Intel | 新用户，Intel x64 | [iLab-GPT-CONJURE-macos-x64-0.9.7.dmg](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/iLab-GPT-CONJURE-macos-x64-0.9.7.dmg) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/iLab-GPT-CONJURE-macos-x64-0.9.7.dmg.sha256.txt) |
| Windows x64 | 新用户，Windows 10/11 x64 | [iLab-GPT-CONJURE-windows-x64_0.9.7.zip](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/iLab-GPT-CONJURE-windows-x64_0.9.7.zip) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/iLab-GPT-CONJURE-windows-x64_0.9.7.zip.sha256.txt) |

标准包数据目录：

- macOS：`~/Library/Application Support/iLab GPT CONJURE/`
- Windows：`%APPDATA%\iLab GPT CONJURE\`

包含更新助手的 macOS 标准 App 会校验 signed `latest.json` 与 DMG SHA256，并在用户确认后自动覆盖、失败回滚和重新启动；`v0.6.1` 及更早的 macOS 标准 App 需要先手动安装当前版本一次，Windows 标准 ZIP 仍手动替换。

## 免安装一键包

| 平台 | 适用设备 | 下载 | SHA256 |
| --- | --- | --- | --- |
| Windows x64 | Windows 10/11 x64 | [ilab-gpt-conjure_windows_portable_x64_0.9.7.zip](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/ilab-gpt-conjure_windows_portable_x64_0.9.7.zip) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/ilab-gpt-conjure_windows_portable_x64_0.9.7.zip.sha256.txt) |
| macOS Apple Silicon | M1/M2/M3/M4 | [ilab-gpt-conjure_macos_portable_arm64_0.9.7.zip](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/ilab-gpt-conjure_macos_portable_arm64_0.9.7.zip) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/ilab-gpt-conjure_macos_portable_arm64_0.9.7.zip.sha256.txt) |
| macOS Intel | Intel x64 | [ilab-gpt-conjure_macos_portable_x64_0.9.7.zip](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/ilab-gpt-conjure_macos_portable_x64_0.9.7.zip) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/ilab-gpt-conjure_macos_portable_x64_0.9.7.zip.sha256.txt) |

portable 自动更新 manifest：

- [latest.json](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.7/latest.json)

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
