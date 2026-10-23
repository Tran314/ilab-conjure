# 下载 / Releases

当前正式版本：[v0.9.9](https://github.com/Tran314/ilab-conjure/releases/tag/v0.9.9)

## 版本说明

当前版本：`v0.9.9`。这是 Tran314 fork 基于上游 v0.9.8 的整合更新，包含统一图片引用、Nano Banana 2.1、任务与历史库同步修复，以及图片优先工作台和启动故障恢复入口。继续保留 fork 的备份恢复安全修复与独立签名更新渠道。

受影响平台：macOS、Windows 标准版与便携版，以及桌面和手机 WebUI。

必要操作与数据迁移：升级前请等待当前任务结束并退出旧实例；升级后重启应用并刷新页面。无需手动迁移任务、图片或设置。首次从上游发行版切换到本 fork 时，请手动下载完整安装包；已有 fork 使用独立签名更新渠道，Windows 标准 ZIP 仍需手动替换应用文件。

本版详情：

### Fork 工作台与稳定性

- **常用生成操作更集中。** 提示词占满操作列，参数摘要与生成按钮同排；空参考区及低频参数默认折叠，手机保留固定生成栏，历史参数检查仍完整显示。
- **无图任务给出准确状态。** 区分任务失败、加载失败和服务端确认的原图缺失；历史详情与管理面板按需展开。
- **Windows 图片响应不再依赖系统 MIME 注册表。** 即使系统未登记 WebP 类型，缩略图和成功结果仍能正常显示与下载。
- **启动故障可以直接恢复。** 无可用供应商时提供配置入口，目录加载失败时提供手动刷新；健康检查不再错误解锁生成按钮，刷新保留输入，恢复后清除本次检查的过期提示。
- **沿用 fork 的安全与更新修复。** 保留供应商 origin 匹配下的密钥继承、Windows 文件保护、备份恢复默认模型映射，以及 Ed25519 清单签名与安装包 SHA256 校验。
- **更新图片解码与证书依赖。** Pillow 升级至 13.0.0，certifi 升级至 2026.10.19，并更新完整哈希锁定文件，纳入本次发布的依赖安全检查。参见 [Pillow 安全公告](https://github.com/python-pillow/Pillow/security/advisories/GHSA-v9pv-5gc8-w2m6)与 [certifi 证书更新](https://github.com/certifi/python-certifi/commit/662df63)。

### P1 · 重要

#### 新增

- **统一图片引用。** 在提示词中使用 `@` 引用当前参考图或公用图库，引用顺序与提交输入保持一致；提交期间保留引用资源，避免任务准备时被清理。

- **支持 Nano Banana 2.1。** 可在 Gemini 系列中选择新型号，提供 1K／2K／4K 分辨率、最多 14 张参考图、超宽与超长比例及 Google 搜索选项，支持 Gemini 与 OpenAI 兼容图片接口的供应商绑定。

#### 修复

- **实时连接中断后继续同步任务状态。** 自动切换为定时更新并持续尝试恢复连接；补充空闲心跳、失去响应后的连接重建和请求超时处理，避免进度长期停滞。
- **连接恢复后正确清除过期错误。** 修复迟到的启动请求、重连请求及超时结果覆盖健康状态，导致反复显示断线或读取失败提示的问题。
- **队列与任务状态保持一致。** 修复请求返回顺序交错时，已完成任务仍残留在运行中队列的问题。
- **单条异常记录不再中断全局同步。** 任务记录损坏、暂时无法读取或正在删除时，其他任务仍可正常更新；无法读取的记录保留原文件，并在详情中提示异常。

### P2 · 常规

#### 修复

- **“保留并结束”后缩略图正常显示。** 修复多图任务部分失败、保留成功结果后任务卡缩略图消失的问题，兼容透明图片和重新排列后的结果。
- **“修改输入与参数”恢复可用。** 点击后载入对应任务的提示词、参考输入和生成参数，解除参数锁定并进入编辑状态；已有草稿可恢复，原任务与成功图片保留，不会自动开始生成。
- **历史任务输入不再被旧响应覆盖。** 修复加载详情期间发生重试或状态更新时，迟到数据覆盖新状态、恢复错误输入的问题。
- **滚动浏览历史时仍能更新。** 离开列表顶部后，已加载卡片继续原位同步，保留滚动位置、选择和键盘焦点；已删除或不再符合筛选条件的任务及时移出。
- **后台刷新失败不再清空历史列表。** 请求失败或取消时保留已有卡片；成功刷新也不会重置多选、退出手机选择模式，或把正在滚动的用户拉回顶部。
- **已打开的历史详情同步更新。** 任务状态和图片结果更新后，详情及时跟随；读取失败时保留当前内容。
- **历史统计和整理操作不再回退。** 修复旧响应覆盖任务数量、标签及刚完成的收藏操作，以及已移除卡片重新出现的问题。

### P3 · 低影响

#### 修复

- **补齐表单控件的主题样式。** 统一供应商设置中的“追加比例提示”“设为该型号默认供应商”，以及通知、模板、备份恢复和历史标签等控件的深浅主题、选中、禁用与键盘焦点状态。
- **统一文件选择控件样式。** 设置恢复与历史导入使用一致的主题外观和交互反馈。

#### 兼容性/安装/打包/更新

- 更新页面资源缓存版本，确保升级后加载新版界面。

#### 工程与文档

- 合并重复测试与浏览器验收工具，调整持续集成中的测试分工，并补充断线恢复、并发更新、异常记录和历史操作的回归覆盖。
- 修复 Windows 浏览器验收脚本的 esbuild 启动入口，复用已有的跨平台测试工具解析。

## 推荐下载

| 平台 | 推荐给 | 下载 | SHA256 |
| --- | --- | --- | --- |
| macOS Apple Silicon | 新用户，M1/M2/M3/M4 | [iLab-GPT-CONJURE-macos-arm64-0.9.9.dmg](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/iLab-GPT-CONJURE-macos-arm64-0.9.9.dmg) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/iLab-GPT-CONJURE-macos-arm64-0.9.9.dmg.sha256.txt) |
| macOS Intel | 新用户，Intel x64 | [iLab-GPT-CONJURE-macos-x64-0.9.9.dmg](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/iLab-GPT-CONJURE-macos-x64-0.9.9.dmg) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/iLab-GPT-CONJURE-macos-x64-0.9.9.dmg.sha256.txt) |
| Windows x64 | 新用户，Windows 10/11 x64 | [iLab-GPT-CONJURE-windows-x64_0.9.9.zip](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/iLab-GPT-CONJURE-windows-x64_0.9.9.zip) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/iLab-GPT-CONJURE-windows-x64_0.9.9.zip.sha256.txt) |

标准包数据目录：

- macOS：`~/Library/Application Support/iLab GPT CONJURE/`
- Windows：`%APPDATA%\iLab GPT CONJURE\`

包含更新助手的 macOS 标准 App 会校验 signed `latest.json` 与 DMG SHA256，并在用户确认后自动覆盖、失败回滚和重新启动；`v0.6.1` 及更早的 macOS 标准 App 需要先手动安装当前版本一次，Windows 标准 ZIP 仍手动替换。

## 免安装一键包

| 平台 | 适用设备 | 下载 | SHA256 |
| --- | --- | --- | --- |
| Windows x64 | Windows 10/11 x64 | [ilab-gpt-conjure_windows_portable_x64_0.9.9.zip](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/ilab-gpt-conjure_windows_portable_x64_0.9.9.zip) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/ilab-gpt-conjure_windows_portable_x64_0.9.9.zip.sha256.txt) |
| macOS Apple Silicon | M1/M2/M3/M4 | [ilab-gpt-conjure_macos_portable_arm64_0.9.9.zip](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/ilab-gpt-conjure_macos_portable_arm64_0.9.9.zip) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/ilab-gpt-conjure_macos_portable_arm64_0.9.9.zip.sha256.txt) |
| macOS Intel | Intel x64 | [ilab-gpt-conjure_macos_portable_x64_0.9.9.zip](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/ilab-gpt-conjure_macos_portable_x64_0.9.9.zip) | [sha256](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/ilab-gpt-conjure_macos_portable_x64_0.9.9.zip.sha256.txt) |

portable 自动更新 manifest：

- [latest.json](https://github.com/Tran314/ilab-conjure/releases/download/v0.9.9/latest.json)

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
