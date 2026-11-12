# Yotolio

[简体中文](#简体中文) | [English](#english)

## 简体中文

Yotolio 帮你整理并批量上传有权使用的音频、封面和歌曲图标到 Yoto Make Your Own（MYO）卡片。
程序会先扫描素材并检查 Yoto 中已有的卡片，再由你确认上传；不会修改或移动原始文件。

### 使用要求

- 目前仅支持 macOS 12 或更高版本；需要 Python 3.11 或更高版本、Yoto 账号和网络连接。
- 无需额外安装 Python 包或 Node.js。

### 开始使用

1. 在本页面点击 **Code → Download ZIP**，解压整个项目文件夹。
2. 安装 [Python 3.11 或更高版本](https://www.python.org/downloads/)，然后双击 `Yotolio.app`
   或 `Start Yotolio.command`。请将 App 与项目中的 Python 文件及 `web/` 文件夹保存在一起。
3. 在 App 中选择存放音频和图片的文件夹，等待扫描。
4. 首次连接时，按 App 内的说明在 [Yoto Developer Dashboard](https://dashboard.yoto.dev/)
   创建 Public Client，填写显示的回调地址并选择所需权限；将 Client ID 复制回 App，
   然后在 Yoto 网站完成登录和授权。
5. 在“检查并上传”中确认卡片和上传选项，上传期间保持 App 运行。完成后可在“卡片结果”查看结果和缺失素材。

### 注意事项

- 已有同名卡片默认跳过；只有选择并确认“替换”才会覆盖音频、封面和图标，缺失素材可能被移除或替换为占位图。
  数量相同不代表内容相同；替换功能尚未通过真实 Yoto 账号验证。
- 上传中断或结果不确定时，先在 Yoto 中检查卡片并保留本机记录，避免重复创建。
- 实体 MYO 卡的关联需要在 Yoto 中另行完成。
- 登录令牌以明文保存在本机 `.yoto-myo/` 中，素材路径和上传进度也保存在此；
  该目录默认被 Git 忽略，请勿分享或随意删除。
- `Yotolio.app` 为本地签名，未经 Apple 公证。如 macOS 阻止打开，只在信任此下载来源时按系统提示允许运行。

### 问题反馈

可以通过 [GitHub Issues](https://github.com/Cathyice315/Yotolio/issues) 反馈问题，请勿提交令牌、登录信息或私人素材。

---

## English

Yotolio helps you organize and batch-upload audio, covers, and song icons you have permission to use
to Yoto Make Your Own (MYO) cards. It scans your files and checks for existing cards on Yoto before
you confirm an upload; it does not modify or move your original files.

### Requirements

- Currently supports macOS 12 or later only; requires Python 3.11 or later, a Yoto account, and an internet connection.
- No additional Python packages or Node.js are required.

### Getting started

1. On this page, click **Code → Download ZIP** and extract the entire project folder.
2. Install [Python 3.11 or later](https://www.python.org/downloads/), then double-click `Yotolio.app`
   or `Start Yotolio.command`. Keep the app beside the project's Python files and `web/` folder.
3. In the app, select the folder containing your audio and images and wait for the scan.
4. For your first connection, follow the app's instructions to create a Public Client in the
   [Yoto Developer Dashboard](https://dashboard.yoto.dev/), register the displayed callback address,
   and select the required permissions. Copy the Client ID back into the app, then sign in and
   authorize access on Yoto's website.
5. In **Review & upload**, confirm the cards and upload choices. Keep the app running during upload.
   When finished, check **Card results** for the outcome and any missing assets.

### Important notes

- Existing cards with matching titles are skipped by default. Only choosing and confirming **Replace**
  overwrites their audio, cover, and icons; missing assets may be removed or replaced with placeholders.
  Equal counts do not mean equal contents; replacement has not been verified with a live Yoto account.
- If an upload is interrupted or its outcome is uncertain, check the cards on Yoto first and keep
  the local records to avoid creating duplicates.
- Linking a physical MYO card must be done separately in Yoto.
- Login tokens are stored in plaintext locally in `.yoto-myo/`, along with source paths and upload
  progress. Git ignores this folder by default; do not share or casually delete it.
- `Yotolio.app` is locally signed and has not been notarized by Apple. If macOS blocks it, follow
  the system's instructions to open it only if you trust the download source.

### Feedback

Report problems through [GitHub Issues](https://github.com/Cathyice315/Yotolio/issues). Do not
include tokens, login information, or private materials.
