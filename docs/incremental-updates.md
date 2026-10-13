# 便携版增量更新

GitHub Releases 同时保存完整便携包、签名清单和差量 ZIP。客户端先读取清单，正常升级只下载与当前版本匹配的差量 ZIP，**不会先下载完整包**。相同的 Python、FFmpeg 和资源文件在本机复用；本地会复制成完整暂存目录，仍需要足够的磁盘空间容纳暂存和备份。

这是文件级差量：改动过的文件完整放入差量包，不对单个二进制文件再做字节差分。每个差量包对应一个基线版本，可以为最近多个版本各生成一份。不存在匹配的基线、本机未改变的文件校验失败，或完整包更小时，界面会明确显示“完整更新”和下载大小。点击增量下载后，如果检查发现必须改用完整包，会停止并要求重新检查，不会静默下载完整包。

## 本地生成

使用从旧版完整 ZIP 解压出的干净 `Substar` 文件夹作为基线，使用 `scripts/build-windows.ps1` 生成的新 `dist/Substar` 作为目标。不要使用包含项目、设置、密钥的日常工作目录。先提高目标版本号。

```powershell
.\runtime\python\python.exe scripts/build_update_delta.py `
  --target dist/Substar `
  --baseline build/baseline-v2.2.7/Substar `
  --output dist/update-assets `
  --private-key E:/ReleaseKeys/substar-update.pem `
  --key-id release-2026
```

`--baseline` 可以重复。输出：

- `Substar-update-v版本.json`：Ed25519 签名清单，包括目标文件的 SHA-256、大小和各基线差量包信息。
- `Substar-Windows-x64-v旧版-to-v新版.delta.zip`：仅包含新增和修改文件。删除的文件不进入新的完整暂存目录，安装时随旧组件备份移走。

签名私钥需由发布者保管，不能放在代码、便携包或 Release 附件中。公钥以 `{ "release-2026": "Base64编码的32字节Ed25519公钥" }` 写入 `substar_core/update_keys.json`，随首次支持增量的版本发布。仓库中的空密钥表是有意保留的：未配置可信公钥时拒绝签名清单，不会信任清单自带的公钥。密钥轮换需先通过旧密钥签名的更新分发新公钥，再启用新密钥签名。

## GitHub 发布

本地验证后，把完整 ZIP、其 SHA-256 文件、签名 JSON 和所有 `.delta.zip` 上传到**同一个正式 Release**，标签必须对应清单版本。附件名称必须保持不变。建议先上传到草稿，全部上传成功后再公开。完整 ZIP 仍为首次安装和跨版本恢复提供下载。

当前自动 Release workflow 只负责完整包，尚未接入生产签名密钥；运行上面的脚本并上传生成附件后，增量才会生效。不要公开含增量清单但缺少对应差量附件的 Release，也不要让自动构建覆盖已校验附件。此次本地实现不创建生产私钥、不配置 GitHub Secrets、不发布新版本。

## 客户端与安装

设置 → 应用更新：检查版本、显示增量/完整方式与下载大小、下载并校验、保存设置并重启安装。源码目录禁止自动覆盖。安装沿用独立 PowerShell 更新器：等主进程退出，将旧程序组件移入备份，安装完整暂存目录，通过启动检查后重启；失败恢复备份。`data`、`models` 和其他非程序顶层目录不参与替换。安装前仍需保存其他窗口的编辑。

清单通过内置公钥验签，并绑定仓库和目标版本；清单及差量附件还验证 GitHub 提供的 SHA-256。每个文件在暂存时重新验证，阻止路径穿越、Windows 特殊设备路径、符号链接/目录联接、重复文件和超限解压。网络或校验失败不会改动当前安装。

## 本地验证

```powershell
.\runtime\python\python.exe -m pytest tests/test_update_delta.py tests/test_update_helper_windows.py tests/test_editor_actions_ass_updates.py -q
```

覆盖签名篡改、不受信任密钥、错误版本、路径穿越、基线变化、新增和删除文件、只请求清单与差量附件，以及真实 Windows 更新器的安装和回滚。测试使用临时密钥与临时安装目录，不更新正在使用的程序。
