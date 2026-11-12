# Yotolio: the no-command-line guide

The interface and every built-in guide step are available in **English and Simplified Chinese**.
English is the default. Change the language in the top-right menu at any time; your choice is remembered for this app.
The renamed app retains its existing language choice. If it has none, it inherits the choice from `Yotolio Review and Upload.app`,
`Yotolio Review Fix.app`, `Yotolio Login Fix.app`, or `Yotolio Update.app`, in that order,
without needing those older app bundles.
No account data is stored in browser local storage.

**macOS build status:** `Yotolio.app` includes the combined review and upload flow and full-aspect cover previews.
A previous version successfully signed in and uploaded new cards; live replacement and the renamed build have not been retested.

## Before you begin

1. Download this repository from GitHub using **Code → Download ZIP**, then extract it.
2. Install **Python 3.11 or newer** with the graphical installer from [python.org](https://www.python.org/downloads/).
   No Python packages, Node.js, or terminal commands are required to run this app.
   The app never asks for a password or client secret; sign in directly with Yoto when prompted.
3. Keep the entire project together. The launcher needs `yoto_ui.py`, `yoto_accounts.py`, `yoto_discovery.py`,
   `yoto_tls.py`, both worker scripts, and the `web` folder beside it.
4. On **macOS**, open `Yotolio.app` if present; source-only downloads need their own build.
   `Start Yoto.command` opens the same app.
   Finish or pause work and quit older Yotolio apps first; they share the saved login and progress.
   The `.app` is locally signed, not a notarized installer. If macOS blocks an unverified download, follow
   the system's explicit **Open Anyway** flow only if you trust this source; do not disable platform protections.
5. On **Windows**, double-click **Start Yoto.bat**. Install Python's launcher when offered by the Python installer.
   A console may stay open in the background; you do not need to type into it.
6. The interface is native on macOS and opens in your browser on Windows.
   Yoto login and other external links open in your browser on both platforms.
   The local service binds only to `127.0.0.1`, using an available port.
   Keep the app running during uploads. Close it with **Quit app** after tasks finish.
   On macOS, closing the window leaves the app running; click its Dock icon to reopen it.
   If an older local service is detected, the app offers to restart it only after confirming that no task is running.

If Python is missing, the launcher offers the official Python download page.
On macOS, if a python.org installation later reports certificate verification errors, use the Python installer's
supplied **Install Certificates.command** in its Applications folder; do not disable HTTPS certificate verification.

### macOS app icon

`Yotolio.app` includes its icon for Finder, the Dock, and the app sidebar; no manual setup is needed.

## 1. Your collection

Choose a folder containing your audio and images using **Choose folder**.
If the native picker is unavailable, paste the full path into the field.
Use Finder or File Explorer's copy-path feature; there is no need to type a command.

Click **Continue** to scan automatically. This checks only local files; it does not search your Yoto account or
upload anything. The progress indicator runs while scanning, without guessing a percentage. Review opens when
the scan succeeds when Yoto is connected; otherwise Connect Yoto opens first. If scanning fails or is
cancelled, fix the problem and try again.
The app searches nested files and uses local rules based on audio extensions,
image sizes and names, track numbers, and supported MP3/MP4 album and track tags. It does not use AI.
Numbered images such as `1.png` and `Icon 01.png` can be matched,
but numbering and a particular folder structure are not required. Other audio formats are discovered by extension;
their embedded tags are not currently read. Album numbering and supported transport prefixes are removed.
Originals are never renamed, moved or copied into a cover folder.

If different cards would have the same cleaned title, the scan stops to prevent mixing them.
Rename the card folders, or adjust album tags when grouping by tags, then scan again.

If a cover or icon is missing or ambiguous, the scan continues and lists it in the final overview.
Review shows a white MYO-style preview for a missing cover, but new cards without a clear cover actually receive
a locally generated colorful placeholder when uploaded. Missing icons are left out.
The app does not guess among ambiguous images. Cards need 1–100 tracks and safe grouping.
For unassigned tracks in a mixed tagged folder, add album tags or separate the files before rescanning.

There is no workspace name to enter. After you sign in, Yotolio identifies the Yoto account and automatically
selects its separate local login, upload history and image choices. Different collections for the same account
share upload history, so review warnings carefully when they contain cards with the same title.

## 2. Connect Yoto

This is the one manual configuration needed on Yoto's website. Click **How do I set this up? View screenshots**
in the app for Dashboard screenshots and instructions in your selected language. Steps 1–3 use screenshots;
step 4 is a labeled illustration of where to copy your Client ID. Yoto's website is in English and its
appearance may change. The callback shown in the screenshot is an example; use the address in your app.

1. Click **Open Yoto Developer Dashboard** and sign in.
2. Create an application and choose **Public Client**.
3. Copy the callback address displayed in the UI and add it to the app's allowed callback URLs.
   The default is `http://127.0.0.1:8788/callback`. Save it exactly, including the port and path.
4. In the app's scopes/permissions settings, select **`user:content:manage`**.
   In the supplied screenshot, this also includes `user:content:view` and `user:icons:manage` automatically.
   Ensure content view, content manage, and icon manage are approved in your Dashboard.
   The login explicitly requests those scopes and `openid`; it does not request `offline_access`.
5. On **My Applications**, click the copy icon next to your application's **Client ID**, then paste it into
   Yotolio. Never paste a client secret, password, or token.
6. Click **Connect with Yoto**. The app opens the login when its local callback server is ready.
   If it does not open, click **Open Yoto login** in the app.
7. Sign in to the intended Yoto account yourself and approve access.
8. Return to the app. Connection progress or any failure appears below the connection button.
   If a confirmation appears at the top, answer it to finish connecting. When successful, the app detects the
   saved authorization and shows its expiry.

To change accounts, expand **Account options** and choose **Switch Yoto account**
(or **Use a different Yoto account** when no login is saved).
Yotolio requests Yoto's account chooser, but if the old account opens again, sign out on Yoto's website first.
Confirm the intended account on Yoto before uploading.

If an older saved history cannot be identified, Yotolio asks whether it belongs to the account you just signed
into. The question stays visible even if you go back or open the guide. Restore it only if you are sure;
selecting the wrong history can lead to duplicate-card decisions. Choose **Keep separate** or **Cancel login**
if unsure. You cannot continue or upload until you answer. Old records are not deleted or silently merged. Multiple
verified histories for one account cannot currently be merged automatically. The identity fallback and account-selection
flow have not yet been verified with a live Yoto account.

If task status becomes unknown, Yotolio warns you rather than automatically retrying or checking online.
Check the current task and any affected cards on Yoto before deciding whether to repeat an action.

If the callback port is occupied, choose another in the UI and update the registered callback in Yoto to match.
If icons are denied, confirm `user:icons:manage` for the same Client ID. You can enable **Require icon access
when connecting** to check it before connecting again; icon access is already requested automatically.
Changing scopes does not upgrade an old token; always reconnect.
The displayed authorization status checks saved scope/expiry, not whether Yoto has subsequently revoked the token.
When Yoto omits `scope` entirely from an authorization response, Yotolio uses the permissions requested in that
login. If only the documented parent `user:content:manage` is returned, missing required child permissions are
checked through read-only Yoto endpoints before acceptance. An icon-list read does not prove that a later upload
will succeed. The current build's sign-in behavior has not been independently retested.
If Yoto's UserInfo service cannot verify the account, Yotolio can securely verify a signed token from the same
login using Yoto's signing keys. This fallback uses the system OpenSSL on macOS, or an `openssl` executable on
`PATH` on Windows and Linux. If verification is unavailable or fails, the existing login and progress are retained.

## 3. Review and upload your cards

The app automatically looks for each title on Yoto and displays audio, icon and cover counts as
**Yoto → Local**. Numbers compare quantities only; equal counts do not prove equal files, and unavailable
counts are not shown as zero. The check can take several minutes; the progress display counts completed
card checks. Nothing is uploaded while checking.
Check the detected grouping and listen to the audio separately if needed. Images are local previews, not
downloaded pictures of the remote card. Covers retain the full image and its proportions; very tall images are
scaled to fit a height limit. On macOS, a failed preview is retried automatically. If it remains unavailable,
use **Retry** and hover over the message for details; the preview error alone does not determine the upload result.

Select one or more usable cards, or use **Select all new** / **Clear**. Matching existing cards default to
**Skip** and are unchecked. Choosing **Replace** selects the card; choosing **Skip** unchecks it. Checking an
existing card also selects its displayed action. **Select all new** never authorizes an existing-card replacement.
Choose **Replace** only if you intend to replace that card's audio, cover and icons with the local sources.
Missing icons will be removed; a missing cover becomes a generated placeholder. If an earlier creation was
interrupted, choose **Check previous creation** to reconcile its saved evidence without overwriting it.
If an earlier replacement is unresolved, choose **Check previous replacement** to compare its saved evidence;
this never sends another replacement.
Duplicate titles, unreadable card details and unresolved earlier actions are shown rather than guessed.
If none of the selected cards needs processing, choose **View results without uploading**. The app shows
the current check without sending an upload or presenting an older batch as the current result.

### Start upload and monitor progress

The upload processes selected new cards and existing cards explicitly chosen for replacement or reconciliation.
Cards left on **Skip** are not sent for processing. Adjust the selections on this page as needed, then click
**Upload selected cards** and confirm the listed account actions.
Each new card includes a clean title, cover or placeholder,
all discovered audio, shuffle each play, and available matching icons at both chapter and track levels.
Cards with no audio, ambiguous grouping, or more than 100 tracks cannot be selected in Review.
Source files stay on your computer until these authorized uploads send their bytes to Yoto.
Only use material you own or have permission to upload.

The progress display comes from real worker output. Replacement uses the existing card ID and preserves
other card metadata where possible, but replaces the audio, cover, icons and app shuffle sequence.
The app saves a private snapshot before replacement; there is no automatic restore UI.
It checks the source, card and library membership again before writing, but the check is not atomic with the
Yoto request. Avoid editing the same card on Yoto while this tool is running. Live replacement has not been
verified. If a replacement response is uncertain, saved evidence is used to reconcile it without submitting it
again. If the previous version is still shown, the original request may yet take effect: wait and check again.
Keep the saved progress; there is no force-retry control for an unconfirmed replacement.

Use **Pause after this card** to request a pause at the next completed card boundary.
The current upload/transcode may take several minutes to finish. Do not force-close the application while waiting.
After a pause or failure, use the same account and source, click **Check Yoto again**, select the remaining cards,
and confirm another upload. The app does not automatically replace the interrupted task with a check.
Never delete state to “fix” an uncertain creation:
inspect the cards on Yoto first to avoid duplicate playlists.
Closing a Mac window or browser tab does not stop the local server; use the app's pause/quit controls.

### If you deleted a previously uploaded card

If the earlier creation was verified, has a saved card ID and no unfinished cover change, Review treats
the card as new when neither its title nor its old ID appears in the current Yoto library. It checks the
library again immediately before creating the card and retains the previous record. An old ID may still
be readable after removal, so that lookup alone does not mean the card remains in the library. If the old
ID is still listed under another name, Review asks you to check it instead.

An unfinished creation or cover change still requires confirmation because it might take effect later.
On the Review & upload page, open Yoto and check the **same account** for any copy, including a renamed copy.
Wait and refresh if a recent change may not have appeared yet. Only if you are sure no copy exists, choose
**Re-create this card** and confirm. The app rechecks the title and any known previous card ID, keeps the
previous record and retries just that card.
Return to Review, check the cards again and start another upload to continue the rest of the batch.
Do not continue if uncertain: an earlier creation without a saved ID could appear later, and concurrent
Yoto changes can still cause duplicates. End-to-end recreation has not been verified against a live Yoto account.

## 4. Card results

The overview shows the latest batch's processed, skipped and unfinished cards. After a successful upload,
it automatically checks the available cards from that batch on Yoto using read-only requests; before an
upload, its read-only view reuses the current review for the selected cards. After a pause or failure, a
check requires an explicit action. A card's presence on Yoto does not mean this batch uploaded it. If a source
is no longer in the scan, it remains listed but cannot be checked. Failed or paused checks can be retried
with **Check again** / **Check Yoto again**.

Each card shows local cover and icon previews, audio/icon counts, and the latest online presence result.
Expand a card to inspect each song's audio and icon status. These images are **local previews**, not
downloaded pictures of the remote card. A source match needs saved upload evidence; matching names alone
do not prove an audio file matches. A card created elsewhere or checked from a different account may
therefore be present but unverified. Network and card-detail errors are reported rather than treated as
missing cards.

The page also shows missing or ambiguous artwork, unmatched images and pending updates. Select **Choose cover**
or expand a card to **Choose icon**; each choice triggers a new scan. You can also add files and use **Rescan**.
To use them on Yoto, review the card again and explicitly choose **Replace**. This replaces all local-content
parts described above, not just the missing artwork.
If Yoto rejects a new card with an omitted icon, choose the missing icon and retry; this API behavior has not
been validated against a live account.

Batch outcomes and online check results last for the current app session only and are cleared when you
switch accounts or change collections. Upload-resume evidence is saved separately for each account and
remains available after restarting. The CLI's optional
`--verify-only` is a stricter whole-collection audit; it is not the batch overview.

Click **Open my Yoto cards** to see your library.
Linking a playlist to a physical MYO card is a separate action in Yoto, not part of this tool.

## Privacy, stopping, and recovery

Credentials, cache, source paths, and card snapshots are stored only under the ignored `.yoto-myo` directory.
The app stores a hash of the Yoto account identifier there, not the identifier itself.
Keep that folder private. It must not be uploaded to GitHub or shared with support.
**Remove saved login** deletes only the local authorization file, not your cards or Yoto-side app authorization.
The login is time-limited; reconnect to the same account when it expires.

Interface guidance and troubleshooting switch with your selected language; some technical errors remain in English.
If an action fails, the UI shows the error. It does not claim partial work is complete.
Do not send tokens, signed URLs, or raw private state when reporting a problem.

macOS launch errors are recorded in `.yoto-myo/ui-launch.log`, which can be opened in a text editor.
Windows launch errors remain visible in its console.
macOS is the development platform; Windows launcher and locking branches are provided, but need a real Windows check.

## 中文使用

双击启动后，在右上角选择 **简体中文**，界面、分步配置和内置问题解答都会切换为中文。
按“选择素材 → 连接 Yoto → 检查与上传 → 卡片结果”完成配置即可，不需要输入命令行。
已有同名卡片默认跳过；只有明确选择并确认覆盖时，才会使用本地音频、封面和图标替换。
数量相同不代表文件相同；请勿同时在 Yoto 编辑同一张卡。之前的版本已成功登录并上传新卡；本次版本和覆盖操作尚未重新验证。
应用会按登录的 Yoto 账号自动分开保存登录、上传进度和图片选择，无需手动填写工作区。
切换账号时点击“切换 Yoto 账号”；如果网站仍打开旧账号，请先在 Yoto 网站退出登录。
如无法确认旧进度属于哪个账号，只有确定属于当前账号时才选择恢复；不确定时选择分开保存或取消。
同一账号下不同素材合集共用上传进度，遇到同名卡片警告时请先核对。
暂停或失败后，请重新检查 Yoto，再选择剩余卡片并确认上传。
部分技术错误会保留英文，方便准确定位问题。
