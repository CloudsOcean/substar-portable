# Windows 透明便携版目录契约

Substar 便携版继承开发版的生产目录。构建过程只做白名单复制并附加固定运行时，不冻结、重写或隐藏产品模块。

## 解压后

```text
Substar/
├─ 启动_Substar.cmd
├─ 停止_Substar.cmd
├─ 只检测环境.cmd
├─ app.py
├─ launcher.py
├─ substar_core/
├─ web/
├─ prompts/
├─ schemas/
├─ scripts/                    # 仅生产 Worker 入口
├─ assets/
├─ runtime/
│  ├─ python/                  # 固定 Windows x64 Python 与锁定依赖
│  └─ ffmpeg/
│     └─ bin/
│        ├─ ffmpeg.exe
│        └─ ffprobe.exe
├─ docs/
├─ portable_manifest.json
├─ release-verification.json
└─ README.md 等发行文档
```

根目录不得包含 `_internal/`、`tests/`、`.git/`、开发缓存、历史项目或预生成 `data/`。产品代码、网页、Prompt、Schema 和 Worker 必须与构建源逐文件哈希一致。

## 首次运行后

```text
Substar/
└─ data/
   ├─ .substar-workbench/
   │  ├─ settings.json
   │  ├─ credentials.enc
   │  ├─ credentials.key
   │  ├─ runtime.json
   │  └─ runtime.sqlite3
   └─ projects/
```

`credentials.enc` 使用 `credentials.key` 加密。二者随整个 `data/` 复制后可在另一台 Windows 电脑读取；只获得程序 ZIP 不包含任何用户凭据。密钥与密文放在同一便携数据目录的目标是避免绑定 Windows 身份或机器，不等同于防御整个数据目录被一并窃取。

## 启动与 Worker 契约

开发版和透明便携版只使用一种命令形态：

```text
<当前固定 Python> <仓库或包内绝对脚本路径> <参数数组>
```

Launcher 启动 `app.py`；Scheduler 启动明确列入白名单的 Worker 脚本。不允许 `sys.frozen`、`--backend`、`--worker-script` 或 PyInstaller 资源根分支。

## 构建门禁

1. 系统地图和项目测试通过。
2. 固定 Python 中的直接及关键传递依赖版本与 `requirements-release.txt` 完全一致。
3. Launcher smoke import 成功。
4. 语义切分素材契约 smoke 成功，素材路径只出现一次。
5. 生产文件源与包逐文件 SHA256 一致。
6. smoke 产生的 `data/` 必须在压缩前安全删除。
7. `release-verification.json` 记录运行时版本、生产哈希和全部门禁结果。
8. 生成 ZIP 前必须使用透明目录完成一次真实视频端到端验收。
