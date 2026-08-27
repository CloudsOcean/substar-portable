# Substar

Substar 是一个本地运行的词级字幕工作台。它把媒体预处理、云端词级听写、语义切分和编辑器组织成可恢复的任务图；翻译、AI 校准与 AI 审阅在编辑器中按需执行。

当前构建：`1.0.5`（Windows x64 正式便携版）

## Beta 功能边界

- 输入 MP4、MOV、MKV、AVI、WEBM、M4V、MP3、WAV、M4A、AAC、FLAC 或 OGG。
- FFmpeg 在本机提取 16 kHz 单声道音频。
- Qwen 云端文件听写返回词级时间、句子和说话人证据。
- DeepSeek 负责语义字幕切分；切分完成即可进入编辑器。
- 编辑器支持词元编辑、Cue/波形/视频联动、翻译、非破坏性繁简投影、AI 校准、AI 审阅、参考文稿、说话人和 SRT 导出。
- 任务状态、事件、重试、取消和产物由 SQLite 运行时统一管理。

本 Beta 不读取旧版项目，也不包含旧版项目导入、旧实验流水线或本地 ASR 引擎。

## 运行

1. 解压完整 ZIP；程序代码与固定运行时必须保持原有相对目录。
2. 双击 `启动_Substar.cmd`；默认打开 `http://127.0.0.1:8769/`。
3. 在“设置”中保存：
   - `ASR_Qwen`：阿里云百炼 Qwen 文件听写密钥；
   - DeepSeek：用于 `Segment_DeepSeek`、翻译、校准与审阅。
4. 回到“切分”页创建项目。

程序只监听本机回环地址。项目、设置和凭据默认保存在安装目录的 `data/`；跨电脑迁移时请复制完整 `data/`，不要单独复制凭据密文或密钥。

## 数据与隐私

媒体会先在本机转换音频，然后上传到用户配置的 Qwen 云端听写服务。字幕文本、提示词和词库可能发送到用户配置的 DeepSeek 兼容接口。Substar 不提供中转服务器。详见 [PRIVACY.md](PRIVACY.md)。

## 开发

```powershell
python -m pip install -r requirements-release.txt
python -m pytest tests -q -p no:cacheprovider
node --test tests/*.test.js
./scripts/build-windows.ps1 -SkipArchive
```

当前完整架构、模块边界、API 调用器和 Worker/Finalizer 数据链见 [docs/architecture/system-map.md](docs/architecture/system-map.md)；机器可读权威为 [docs/architecture/system-map.json](docs/architecture/system-map.json)。便携版物理目录见 [docs/architecture/portable-layout.md](docs/architecture/portable-layout.md)。

## Beta 限制

- 当前预览包使用可审计的 CMD 启动入口，不包含签名安装程序。
- 这是全新项目格式；不会扫描或迁移旧项目。
- 云服务可用性、计费和内容保留规则由对应服务商决定。
- 在正式公开发行前仍需确定项目的最终开源许可证或商业许可证。

问题报告请附：版本号、复现步骤、任务错误码，以及删除密钥/媒体内容后的日志片段。不要提交 API Key。

