# Yoto MYO Upload

把本地专辑批量上传到自己的 [Yoto Make Your Own](https://my.yotoplay.com/cards)：
**去掉专辑序号、上传封面、上传全部音频、启用随机播放、添加每首歌的 icon**。
只处理自己拥有或获准上传的素材。本仓库不包含音频、图片、账号数据或登录凭据。

Python **3.11+**，仅标准库；当前支持 macOS/Linux（使用 `fcntl` 防止同一工作目录并发上传）。
不需要安装依赖，不需要把密码交给程序，也不需要启动自动化浏览器。

## 1. 准备文件与封面

可以有任意数量的专辑，专辑也可以放在合集下面；每张卡最多 100 首。
音频与 icon 的数字编号必须严格一一对应，从 1 连续到 N，程序按自然数顺序排序。
每张专辑直接放音频；也支持已带 `cover` 图片或已准备好封面的专辑把音频放在子目录中。
合集本身不要同时放音频和其他独立专辑。不要使用同名专辑文件夹。

```text
collection/
  01 - Example Songs/
    cover.jpg
    01 - First Song.mp3
    02 - Second Song.m4a
    icons/
      01.png
      Icon 02.png
  Another Collection/
    02 - More Songs/
      cover.png
      01 - Another Song.mp3
      01.png
```

```bash
python3 yoto_upload.py --root "/path/to/collection" --prepare-covers
python3 yoto_upload.py --root "/path/to/collection" --plan
```

`--prepare-covers` 将封面复制到 `collection/Covers/<原专辑文件夹名>.<扩展名>`，
通过 SHA-256 检查新副本，**不修改原文件**。优先选文件名含 `cover` 的 JPG/PNG；
没有时仅接受唯一一张未编号的顶层图片。已有副本不会覆盖。
如果候选不唯一，程序会停止，请手动把选中的图片放入 `Covers`，按原专辑文件夹名命名。
`--plan` 不调用 API、不创建账号、不上传，也不需要登录。

专辑标题仅去掉开头的数字序号以及原素材的 `Yoto -` / `youtube版 -` 前缀；
保留原来的大小写、拼写以及歌曲完整文件名。不能可靠选择的素材会报错，不会猜测或静默跳过。

## 2. 创建 Yoto Public Client 并登录

在 [Yoto Developer Dashboard](https://dashboard.yoto.dev/) 创建 **Public Client**。
配置完全相同的 callback：

```text
http://127.0.0.1:8788/callback
```

给应用预先批准 `user:content:view` 和 `user:content:manage`。
登录请求还包含 `openid`；**不要照抄其他人的 client ID，也不需要 client secret**。

```bash
python3 yoto_authorize.py --client-id "YOUR_PUBLIC_CLIENT_ID" --work-dir .yoto-myo
```

打开终端打印的短地址 `http://127.0.0.1:8788/start`，自行完成 Yoto 登录和授权。
成功页面会显示本次实际授予的 scopes。凭据保存在 `.yoto-myo/authorization.json`，
新文件权限为 `0600`，程序不会打印 token。回调服务器只监听本机，最多运行 15 分钟。
端口占用时，可使用 `--port 8789`，但必须同时更改 Dashboard 中注册的 callback。
凭据到期后重复登录，加 `--replace`；不会自动刷新，不请求 `offline_access`。

**每个 Yoto 账号和素材集合使用单独的 `--work-dir`，所有命令使用同一个目录。**
它包含账号范围内的缓存、私有快照和断点状态，不能共享、提交或公开。

## 3. 上传、补 icons 与独立核验

```bash
# 默认按五步要求上传所有专辑；已有同名卡不会重复创建。
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo

# 先用一张卡确认效果；名称使用 --plan 输出的清理后标题。
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --album "Example Songs"

# 只补齐现有卡缺失的 icons，不创建新卡。
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --existing-only

# 真正只读：GET 核验，不上传、不补 icons、不更新断点状态。
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --verify-only
```

上传和转码并发数为 3；歌曲 icon 上传并发数为 3。每张卡创建后重新 GET，
核对标题、封面、音频顺序与引用、歌曲数量、随机播放配置、chapter 与 track 两处 icon。
`--verify-only` 还重新计算本地源文件哈希并比对上传缓存；需要保留同一个私有工作目录。
从网站导入的既有卡无法反推原始音频哈希，核验会明确标记 `BASELINE_ONLY`：
这类卡的音频和封面只与导入时的远端基线比较，不声称证明其来自本地源文件。

**已有卡策略：** 必须有相同清理后标题、歌曲数量、文件名/顺序、封面和已启用的随机播放。
只补空 icon，不替换不同的非空 icon，也不自动重传既有音频或修改设置。
先保存私有 `before-icons` 快照，更新前重读检查并发变化，更新后严格比较所有非 icon 内容。
如果旧卡没有启用随机播放，先在网站上打开它再运行；程序不会暗中重写其他设置。

**断点续传：** 正常中断后，用原命令和同一工作目录重新运行即可，已完成的音频和图像使用哈希缓存。
创建 POST 不盲目重试；若程序在创建期间退出，先重新运行，让它按远端同名卡对账。
如果远端找不到卡，但本地状态仍为 `pending_create` / `created` / `verified`，
程序会停止防止重复。先在网站确认创建结果并备份私有状态，再人工处理，不要直接删除状态重跑。
发现重复标题、已有卡 ID 改变、未知素材映射或并发修改时都会停止。
中断可能留下已上传但未绑定到卡的媒体，不会自动删除远端媒体。

## 4. 测试与安全边界

```bash
python3 -m unittest discover -s tests -v
```

测试使用合成素材和模拟 API，不登录、不调用真实 Yoto、不启动浏览器、不修改个人素材。
`.gitignore` 排除了常用凭据、缓存、快照和音频，但它不是秘密扫描器：
**不要把任意工作目录、源素材、真实 API 响应、登录 URL 参数或签名上传 URL 放进 Git。**
操作完成后可删除工作目录内的 `authorization.json`，保留私有断点文件供后续核验；
再次调用 API 时需要重新授权。

## 参考与经验

[LESSONS.md](LESSONS.md) 记录认证、图标、API 字段、易错点和本次实际结果。
这些是特定时间的一次实测经验，不代表 Yoto 永久保证 API 或 scopes 行为。
