# Yotolio

把自己的音频、封面和歌曲图标整理并批量上传到 Yoto Make Your Own 卡片。程序会先扫描本地素材，
检查 Yoto 中是否已有同名卡片，再由你确认上传；只上传你拥有或有权使用的素材。

## 开始使用

1. 在 Mac 上安装 [Python 3.11 或更新版本](https://www.python.org/downloads/)。运行程序不需要额外安装
   Python 包、Node.js 或输入命令。
2. 打开文件夹中的 **`Yotolio.app`**；也可以双击 `Start Yoto.command`。
   **不要单独移动 App**：它需要与当前文件夹中的 Python 文件和 `web` 文件夹放在一起。
3. 界面右上角可以切换中文和英文。选择素材文件夹，点击继续，程序会扫描音频、封面和图标。
4. 首次连接时，按程序内截图到 [Yoto Developer Dashboard](https://dashboard.yoto.dev/) 创建
   **Public Client**，填写程序显示的回调地址并批准所需权限，再复制 Client ID 回程序。
   登录和授权在 Yoto 网站完成，不要向程序输入密码、Client Secret 或令牌。
5. 在**检查并上传**中查看每张卡、已有卡片和数量变化，选择要上传的卡片并确认。
   上传期间保持程序运行。完成后在**卡片结果**查看本次结果和需要补充的素材。

已有同名卡片默认跳过；只有明确选择**替换**才会覆盖其音频、封面和图标。数量相同不代表文件内容相同，
请检查预览和歌曲匹配。封面按原比例完整显示；预览失败可以重试，预览报错不等于上传失败。
如果创建结果不确定或任务中断，请先查看 Yoto 中的卡片，再按界面提示处理，不要删除记录来绕过提示。

关闭窗口不等于退出：可以从 Dock 重新打开窗口；完成或暂停任务后，用应用菜单或界面中的退出按钮退出。
macOS App 为本地签名、未经过 Apple 公证；如果系统阻止打开，仅在信任此来源时使用系统提供的打开流程。
Windows 可双击 `Start Yoto.bat` 使用浏览器界面，但尚未在真实 Windows 机器上验证。

## 文件与数据

- `Yotolio.app`：Mac 图形界面；须与下列文件保存在同一文件夹。
- `yoto_*.py`、`web/`：扫描、连接、上传和界面所需的程序文件。
- `Yotolio*.swift`、`build_macos_app.sh`、`tests/`：Mac 构建源码与测试。
- 隐藏的 `.yoto-myo/`：本机登录信息、素材选择和上传进度。**不要删除或分享这个文件夹**；
  改名不会清除这些记录。
- [GUI_GUIDE.md](GUI_GUIDE.md)：英文详细操作说明；程序内也有中英文配置指导。
- [LESSONS.md](LESSONS.md)：历史 API 实测与技术说明。

旧版曾成功登录并上传新卡；当前改名版本及已有卡片替换尚未重新用真实账号验证。
程序不会修改或移动原始素材；选择上传后，所选内容会发送到 Yoto。

## Git 与隐私

源码中有 Yoto 的公开 API 地址和调用逻辑，但不需要 Yoto 密码或 Client Secret。
Client ID 是 Public Client 的标识，不是密码；登录后获得的访问令牌则能操作账号，必须保密。
程序默认将 Client ID、令牌、素材路径和上传进度保存在本机的 `.yoto-myo/` 中。

`.gitignore` 排除了该目录以及常见的本机配置、令牌和进度文件，正常 `git add` 不会添加未跟踪的
被忽略文件。忽略规则不会移除已经跟踪或提交过的内容，也不能阻止强制添加，或保护复制到其他文件中的凭据。
如果用 `--work-dir` 自定义工作目录，应把整个目录加入忽略规则。
提交或推送前，检查 Git 暂存区及其变更；不要提交私有目录、令牌，或包含个人资料的日志和截图。
如果令牌已经公开，应先撤销或使其失效并重新登录；仅删除文件不能从 Git 历史中移除它。

---

## Detailed documentation (English)

The app's native macOS interface and browser interface are available in English and Simplified Chinese.
The interface and built-in guide change language together; some technical errors remain in English.
Keep `Yotolio.app` next to the project files. A source-only download needs a built app bundle.
Python-equipped Linux users can run `python3 yoto_ui.py`; there is no packaged no-CLI Linux launcher.
See [GUI_GUIDE.md](GUI_GUIDE.md) for the complete walkthrough.

## What the UI handles

| Step | What you do | What the app does |
| --- | --- | --- |
| Your collection | Choose a folder and click Continue | Automatically scan nested files using extensions, supported tags, names and image sizes without AI |
| Connect Yoto | Create your own Public Client in Yoto's dashboard and sign in yourself | Show bilingual instructions, Dashboard screenshots and a labeled Client ID copy illustration, plus a bounded PKCE S256 login |
| Review & upload | Compare counts, select cards, choose any replacements, and confirm the upload | Automatically check Yoto, show actual check progress and card counts, then create or replace the reviewed cards |
| Card results | Review this batch, online status and missing assets | Check the available cards in the batch on Yoto, show per-song evidence and local previews |

Yoto's dashboard configuration is still done by you on Yoto's website; the app guides but does not bypass it.
Physical MYO card linking, NFC writing, and downloading commercial cards are outside scope.
Only upload material you own or have permission to use.

## Finding your materials

Select a single album folder or a collection. The app searches nested files and considers file extensions,
names, image dimensions, nearby artwork, and supported MP3/MP4 album and track tags; no numbered layout or
`Covers` folder is required. These are local heuristics, not AI or guaranteed identification. Other audio formats
are discovered by extension but their embedded tags are not currently read. Album numbering and supported
`Yoto -` / `youtube版 -` prefixes are removed. Check the detected grouping and order before uploading.
Clicking **Continue** in Your collection starts the local scan automatically; it does not search your Yoto account
or upload anything. The scan shows indeterminate progress and opens Connect Yoto or Review when it succeeds. You can rescan
from Review or the final overview. Failed or cancelled scans are not treated as complete.
If multiple cards would have the same cleaned title, scanning stops: rename the card folders, or adjust
album tags when grouping by tags, before scanning again. This prevents accidentally merging different cards.

When a cover is missing or ambiguous, Review shows an original white MYO-style preview; new cards actually receive a
locally generated, stable colorful PNG. Missing or ambiguous song icons are omitted rather than guessed. The final
**Card results** lists gaps and unmatched images and offers image selection for covers and individual songs.
Cover previews retain the complete image and its proportions, with their height bounded for very tall images.
On macOS, a failed preview is retried automatically; if it still fails, use **Retry** and hover over the message
for details. A preview failure alone does not establish whether an upload succeeded or failed.
Review flags cards without audio or with more than 100 tracks. A mixed tagged folder
with unassigned tracks needs album tags or separate folders before those tracks can be uploaded. Original files
are never renamed, moved or copied into a new cover folder.

After adding or selecting images, rescan. If you choose **Replace** for an existing card, all of its audio,
cover and icons are replaced with the reviewed local sources, not just missing artwork. An unmatched icon is
removed and a missing cover becomes a generated placeholder. Selected paths are stored privately for the
chosen collection and Yoto account.
Yoto's response to a new card with omitted icons has not been validated against a live account; if Yoto rejects it,
choose the missing icons and retry.

## Authorization and private progress

Create a **Public Client** at <https://dashboard.yoto.dev/>. Register the callback shown by the app
(default `http://127.0.0.1:8788/callback`) and select `user:content:manage`. In the supplied Dashboard example,
this also selects `user:content:view` and `user:icons:manage`; ensure view and manage are approved.
Yotolio requests all three scopes; for icon-specific errors, confirm `user:icons:manage` for the same Client ID.
The optional icon check requires Yoto to confirm icon access before connecting.
On the Dashboard's **My Applications** page, use the copy icon next to your app's Client ID, then paste it into
Yotolio. The fourth setup illustration is a labeled example, not a screenshot of your application.
Login opens automatically when the local callback is ready; if it does not, use **Open Yoto login** in the app.
Connection progress and failures appear directly below the connection button. Account-management actions are
inside the collapsed **Account options** section. If a browser callback fails, it remains a failure response but
asks you to return to the app for details instead of showing a generic error page.

If the authorization response omits `scope` entirely, the app uses the scopes requested in that login, as allowed
by [OAuth 2.0](https://www.rfc-editor.org/rfc/rfc6749#section-5.1) when they are unchanged. Explicitly invalid or
insufficient permissions still fail. If Yoto returns only the documented parent `user:content:manage`, the app
checks the missing required child permissions through read-only Yoto endpoints before accepting them. Yoto's
UserInfo endpoint separately verifies account identity. If it is unavailable or rejects the token, the app can
instead verify a signed identity token from this login, or a signed Yoto API access token when no identity
token was supplied. It checks Yoto's public signing keys, issuer, intended audience, expiry and client binding;
identity tokens also require the login nonce. Unverified token contents are never used as an identity, and
failed checks never replace existing login or progress.
Reading the icon list does not prove that a later icon upload will succeed; Yoto may also revoke access later.

Authorization is time-limited. No refresh token or password is required. Tokens are never sent to the frontend.
The server listens only on `127.0.0.1`; requests that change state require this UI's session header.
Credentials, source paths, cache and original-card snapshots stay in the ignored **`.yoto-myo`** folder.
The app identifies the signed-in Yoto account using a verified Yoto identity and uses a private hash
to select that account's saved login, upload history and image choices automatically; no workspace name is needed.
Choose **Switch Yoto account** to request account selection. If Yoto still opens the previous account, sign out
on Yoto's website and try again. Confirm the account on Yoto before uploading.

If older saved progress cannot be matched to an account, a prominent confirmation remains visible in the app
after Yoto sign-in. Restore only if you are sure it belongs to that account; otherwise keep it separate or cancel.
You can go back to inspect earlier steps, but cannot continue or upload until the question is resolved.
It leaves old files intact
and refuses to merge multiple verified histories. Restoring the wrong history can cause duplicate-card decisions.
Different collections on the same account share upload history; review warnings for cards with the same title.
Do not delete history to bypass a warning or share the private folder or raw API responses.
The identity fallback and account-selection flow have not yet been verified with a live Yoto account.

If the app loses track of a task, it displays an unknown-outcome warning and does not automatically recheck or
retry it. Check the current task and any affected cards on Yoto before deciding whether to try again.

The identity fallback uses macOS's system OpenSSL; on Windows or Linux it requires an `openssl` executable
on `PATH`. UserInfo does not require OpenSSL. If neither route can be verified, connection fails safely and
keeps the existing login. Diagnostic logs record only the UserInfo failure category or HTTP status, not
tokens, account subjects or Client IDs.

On macOS, if Python has no configured trust store, the app uses the system certificate bundle when available;
HTTPS certificate verification remains enabled, and explicitly configured certificate paths are not replaced.
If verification still fails, use the official Python installer's **Install Certificates.command**.

Review automatically checks matching titles online and shows audio, icon and cover counts as **Yoto → Local**.
The check can take several minutes; the progress indicator counts cards actually checked and no upload happens yet.
An equal count does not prove that the files are the same; unavailable counts are not shown as zero.
Existing cards default to **Skip** and are unchecked. Choosing **Replace** selects the card, while choosing
**Skip** clears its selection; selecting an existing card also chooses its displayed action. **Select all new**
never selects existing cards for replacement. Upload from the same page and confirm the listed actions before the app uses Yoto's
[create-or-update content API](https://yoto.dev/api/content/createorupdatecontent/)
with the existing card ID. Replacement changes audio, cover, icons and the app's shuffle sequence; it preserves
other card metadata where possible. A private pre-replacement snapshot is saved under `.yoto-myo`, but there is
no automatic restore UI. Interrupted creations are checked against saved evidence, not overwritten. If a
replacement's response is uncertain, checking it never sends another replacement. If Yoto still shows the
old card, the action remains unresolved: wait and check again, and keep the saved progress. There is no
force-retry control for an unconfirmed replacement.
Before writing, the app rechecks the source, card and its library membership. This check is not atomic with the
Yoto POST; do not edit the same card concurrently. Duplicate names, unreadable details or changed evidence stop
the affected action instead of being treated as an absent card. Live replacement has not been verified.
Creation POSTs are not blindly retried. If a previously verified card with a saved ID and no
unfinished cover change is absent from the current library, Review treats it as a new card.
The app checks both its title and old ID again before creating it and retains the old record.
An old ID may still be readable after removal, so that lookup alone does not mean the card
remains in the library. If the saved ID is still listed under another name, Review stops and
asks you to check it. Resume with the same account and source; do not remove saved state to
bypass an uncertain creation. If an earlier creation or cover change is unfinished, the Review & upload
page explains the conflict. Check the same Yoto account for any copy, including renamed
copies, and wait for recent changes to appear. Only when you are sure it is absent, confirm
**Re-create this card**. The app rechecks the title and any known old ID, retains the old
record and retries that card only. Check Yoto again, select the remaining cards and confirm another upload to continue.
If the earlier creation was interrupted without a card ID, or another client changes the
library concurrently, delayed duplicates are still possible. End-to-end recreation has not
been verified against a live Yoto account.

**Card results** automatically performs a read-only Yoto check for available sources after a successful batch;
before any upload its read-only view reuses the current review for the selected cards. After a pause or failure,
the app waits for an explicit recheck rather than replacing the task status automatically. To continue, check Yoto
again in Review & upload, select the remaining cards and confirm another upload. Results show processed, skipped and unfinished outcomes
separately from whether a card exists. Missing sources cannot be checked, and a failed check can be retried.
Artwork is a local preview, not a downloaded image of the Yoto card. Audio, icon and cover matches require
saved upload evidence; matching a song title alone does not prove its audio source. No missing or failed result
is presented as verified. The batch and check report last only for the current app session and are cleared
when switching accounts or changing collections; upload-resume evidence persists privately for the account.

## CLI (optional)

The original reusable tools remain available for scripting:

```bash
python3 yoto_upload.py --root "/path/to/collection" --plan
python3 yoto_authorize.py --client-id "YOUR_PUBLIC_CLIENT_ID" --work-dir .yoto-myo
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --album "Example Songs" --album "Another Card"
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --create-only
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --update-existing
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --inspect-only --album "Example Songs"
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --existing-only
python3 yoto_upload.py --root "/path/to/collection" --work-dir .yoto-myo --verify-only
```

The CLI still uses an explicitly supplied work directory and does not perform the UI's automatic account
assignment. Avoid using it to replace credentials in an app-managed account folder.
Use the same work directory throughout. `--prepare-covers` remains an optional legacy copying command, not part of
the UI workflow. The legacy `--existing-only` fills missing icons without changing covers; `--update-existing`
can also replace this app's confirmed placeholders. `--verify-only` remains a strict audit of all eligible
scanned cards and may fail if evidence is missing; the UI's batch inspection is different. Both are read-only.
To replace expired authorization, add `--replace` to the authorization command.
The UI starts these workers without invoking a shell.

## Development checks

```bash
python3 -m unittest discover -s tests -v
node --test tests/ui.test.mjs
xcrun swiftc -parse-as-library YotolioModel.swift tests/native_test.swift -o "$TMPDIR/yotolio-native-model-test" -framework AppKit && "$TMPDIR/yotolio-native-model-test"
```

The Swift check is macOS-only and uses a mocked local-service protocol. Tests use synthetic files and mocked Yoto operations. They do not authenticate, install dependencies,
modify the live collection, or upload to a real account. Node.js is needed only for frontend development tests.
There is no frontend build step or runtime dependency installation.
Windows and actual desktop/browser interaction must be checked on those platforms separately.

On macOS, `./build_macos_app.sh` rebuilds and signs the existing `Yotolio.app` bundle.
Quit it before rebuilding; the script refuses to overwrite a bundle with Finder customization.
The launcher bundle must remain next to the project files.

## Files and operational lessons

- `Yotolio.swift`, `YotolioModel.swift`, `YotolioNative.swift`: native macOS app and bilingual UI.
- `yoto_ui.py`, `web/`: shared local backend and browser interface.
- `yoto_accounts.py`, `yoto_discovery.py`, `yoto_upload.py`, `yoto_authorize.py`: account identification,
  discovery, upload and login workers.
- Desktop launchers and `GUI_GUIDE.md`: no-CLI onboarding.
- [LESSONS.md](LESSONS.md): historical API/authentication details from the successful 31-card / 452-track operation.

The historical result is evidence of that completed operation, not a guarantee of future API behavior.
No audio, covers, account data, signed upload URLs, or credentials are distributed in this repository.
