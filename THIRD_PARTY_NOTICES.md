# Third-party notices

Substar 的 Python 依赖及其许可证由对应上游项目声明，包括 FastAPI、Starlette、Uvicorn、Pydantic、Requests、orjson、NumPy、python-multipart、cryptography、cffi 与 pycparser。

Windows 包在 runtime/ffmpeg/bin 中提供 FFmpeg 9.0.2 和同一构建的 ffprobe，使用 GPLv3 或更高版本。NVIDIA 编码接口使用 nv-codec-headers n11.1.5.3，CPU H.264 和 HEVC 编码分别使用 x264 和 x265。FFmpeg 的许可文件位于 runtime/ffmpeg，源码归档、构建配置和依赖来源记录位于 runtime/ffmpeg/source。

- https://ffmpeg.org/legal.html
- https://ffmpeg.org/download.html
- https://github.com/FFmpeg/nv-codec-headers
- https://github.com/ffmpeg-builds/FFmpeg-Builds

云端模型及服务不随本软件分发，其条款由阿里云百炼、Qwen、DeepSeek 或用户自行配置的兼容服务提供方决定。

中文参考稿匹配使用 pypinyin 0.55.0（MIT License）；完整许可证随 Python 运行时的 pypinyin 包元数据分发。
