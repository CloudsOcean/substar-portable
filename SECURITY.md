# Security

## 报告安全问题

请不要在公开 Issue 中提交 API Key、完整日志、媒体、字幕正文或 `data/` 目录。报告中只保留版本号、错误码、最小复现步骤和脱敏日志。

## 安全边界

- HTTP 服务仅监听 `127.0.0.1`。
- API Key 使用 `data/.substar-workbench/credentials.key` 加密为 AES-GCM 信封；Worker 只获得当前任务声明的用途密钥，并在启动后立即从环境变量中移除。
- Worker 通过严格 JSONL 协议通信，任务产物在发布前验证 Schema、大小与 SHA-256。
- 运行时记录 PID、进程创建时间、实例 ID 和安装根，强制停止前必须全部匹配。
- 本 Beta 未签名。只从项目发布页获取 ZIP，并核对同版本的 SHA-256 文件。

便携密钥与密文位于同一个 `data/`，目的是支持整体迁移，不用于抵御整个数据目录被复制。请依靠 Windows 账户权限、磁盘加密和备份保护 `data/`。
