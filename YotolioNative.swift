import AppKit
import SwiftUI

private let coral = Color(red: 0.84, green: 0.32, blue: 0.23)
private let ink = Color(red: 0.19, green: 0.23, blue: 0.26)
private let cream = Color(red: 0.98, green: 0.97, blue: 0.94)
private let bodyFont = Font.system(size: 16, design: .rounded)
private let detailFont = Font.system(size: 15, design: .rounded)
private let noteFont = Font.system(size: 14, design: .rounded)
private let sectionFont = Font.system(size: 17, weight: .semibold, design: .rounded)

private func confirm(_ title: String, _ detail: String, _ action: String, _ cancel: String) -> Bool {
    let alert = NSAlert()
    alert.messageText = title
    alert.informativeText = detail
    alert.addButton(withTitle: action)
    alert.addButton(withTitle: cancel)
    return alert.runModal() == .alertFirstButtonReturn
}

private func fileNames(_ paths: [String]) -> String {
    paths.map { URL(fileURLWithPath: $0).lastPathComponent }.joined(separator: ", ")
}

struct YotolioNativeView: View {
    @ObservedObject var model: YotolioModel

    var body: some View {
        HStack(spacing: 0) {
            sidebar
                .frame(width: 246)
            Divider()
            VStack(spacing: 0) {
                toolbar
                Divider()
                if model.taskStatusUnknown {
                    HStack(alignment: .top) {
                        messageBox(model.text("Task status unavailable", "无法确认任务状态"),
                                   detail: model.text(
                                    "The app lost track of a task, so its result is unknown. Check the current task and any affected cards on Yoto before trying again.",
                                    "应用无法确认任务的最终状态。重新操作前，请检查当前任务以及 Yoto 中可能受影响的卡片。"),
                                   color: .orange)
                        Button { model.taskStatusUnknown = false } label: {
                            Image(systemName: "xmark")
                        }
                        .buttonStyle(.plain)
                        .help(model.text("Dismiss", "关闭"))
                    }
                    .padding(.horizontal, 24)
                    .padding(.vertical, 10)
                }
                if let migration = model.status?.accountMigration {
                    AccountConfirmationView(model: model, migration: migration)
                        .padding(.horizontal, 24)
                        .padding(.vertical, 10)
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        if model.offline {
                            messageBox(
                                model.text("The local service is unavailable. Reopen Yotolio.",
                                           "本地服务已断开，请重新打开 Yotolio。"),
                                detail: model.errorMessage, color: .red
                            )
                            Button(model.text("Try again", "重试")) { model.refresh() }
                        } else if !model.errorMessage.isEmpty {
                            HStack(alignment: .top) {
                                messageBox(model.text("Could not complete that action", "操作未能完成"),
                                           detail: model.errorMessage, color: .red)
                                Button {
                                    model.errorMessage = ""
                                } label: {
                                    Image(systemName: "xmark")
                                }
                                .buttonStyle(.plain)
                                .help(model.text("Dismiss", "关闭"))
                            }
                        }
                        if !model.notice.isEmpty {
                            HStack {
                                Text(model.notice).foregroundColor(.secondary)
                                Button(model.text("Dismiss", "关闭")) { model.notice = "" }
                                    .buttonStyle(.link)
                            }
                        }
                        if model.status == nil {
                            if !model.offline { ProgressView(model.text("Opening your workspace…", "正在打开工作区…")) }
                        } else if model.showingGuide {
                            GuideView(model: model)
                        } else {
                            stepContent
                            if let job = model.status?.job, showsJob(job) {
                                JobView(model: model, job: job)
                            }
                            stepNavigation
                        }
                    }
                    .frame(maxWidth: 950, alignment: .leading)
                    .padding(.horizontal, 34)
                    .padding(.vertical, 28)
                    .frame(maxWidth: .infinity, alignment: .top)
                }
            }
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .frame(minWidth: 800, minHeight: 600)
        .font(bodyFont)
        .controlSize(.large)
        .foregroundColor(ink)
        .accentColor(coral)
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                if let icon = YotolioBrand.icon {
                    Image(nsImage: icon)
                        .resizable().scaledToFit().frame(width: 46, height: 46)
                        .accessibilityHidden(true)
                } else {
                    Image(systemName: "music.note.list")
                        .font(.system(size: 20, weight: .semibold)).foregroundColor(.white)
                        .frame(width: 46, height: 46).background(coral).cornerRadius(12)
                        .help(model.text("App icon could not be loaded", "无法读取应用图标"))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(YotolioBrand.name)
                        .font(.system(size: 23, weight: .bold, design: .rounded))
                        .lineLimit(1).minimumScaleFactor(0.75)
                    Text(model.text("Little cards, big adventures.", "小小卡片，大大冒险。"))
                        .font(.system(size: 12, design: .rounded)).foregroundColor(.secondary)
                }
            }
            .padding(.bottom, 30)

            ForEach(1...4, id: \.self) { number in
                Button {
                    model.navigate(to: number)
                } label: {
                    HStack(spacing: 10) {
                        Text("\(number)")
                            .font(.system(size: 14, weight: .semibold, design: .rounded))
                            .frame(width: 29, height: 29)
                            .background(model.step == number && !model.showingGuide ? coral : Color.black.opacity(0.06))
                            .foregroundColor(model.step == number && !model.showingGuide ? .white : ink)
                            .clipShape(Circle())
                        Text(stepName(number))
                            .font(.system(size: 15, weight: model.step == number && !model.showingGuide ? .semibold : .regular, design: .rounded))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 9)
                    .background(model.step == number && !model.showingGuide ? Color.white.opacity(0.8) : Color.clear)
                    .cornerRadius(10)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!model.canNavigate(to: number))
            }
            Spacer(minLength: 25)
            VStack(alignment: .leading, spacing: 8) {
                Image(systemName: "lock.shield").foregroundColor(coral)
                Text(model.text("Files stay on your Mac until you choose to upload.",
                                "选择上传前，素材只保存在你的 Mac 上。"))
                    .font(noteFont).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.65))
            .cornerRadius(10)
        }
        .padding(18)
        .background(cream)
    }

    private var toolbar: some View {
        HStack {
            Text(model.showingGuide ? model.text("Setup guide", "配置指南") : stepName(model.step))
                .font(.system(size: 15, weight: .semibold, design: .rounded))
                .foregroundColor(.secondary)
            Spacer()
            Button {
                model.showingGuide.toggle()
            } label: {
                Label(model.showingGuide ? model.text("Back to workspace", "返回工作区")
                      : model.text("Setup guide", "配置指南"), systemImage: "book")
            }
            .buttonStyle(.plain)
            .padding(.trailing, 12)
            Picker(model.text("Language", "语言"), selection: $model.language) {
                Text("English").tag("en")
                Text("简体中文").tag("zh")
            }
            .labelsHidden()
            .frame(width: 128)
        }
        .padding(.horizontal, 28)
        .frame(height: 60)
    }

    private func stepName(_ number: Int) -> String {
        switch number {
        case 1: return model.text("Your collection", "选择素材")
        case 2: return model.text("Connect Yoto", "连接 Yoto")
        case 3: return model.text("Review & upload", "检查与上传")
        default: return model.text("Card results", "卡片结果")
        }
    }

    @ViewBuilder private var stepContent: some View {
        switch model.step {
        case 1: CollectionView(model: model)
        case 2: ConnectView(model: model)
        case 3: ReviewView(model: model)
        default: OverviewView(model: model)
        }
    }

    private var stepNavigation: some View {
        VStack(alignment: .trailing, spacing: 8) {
            if model.step == 3 && !model.busy && model.currentReview?.state == "completed"
                && model.actionableTitles.isEmpty {
                Text(model.selectedTitles.isEmpty
                     ? model.text("Select at least one card to continue.", "请至少选择一张卡片。")
                     : model.text("No cards will be changed. You can view the check results.",
                                  "不会更改任何卡片，可以查看检查结果。"))
                    .font(noteFont).foregroundColor(.secondary)
            }
            HStack {
                if model.step > 1 {
                    Button(model.text("Back", "上一步")) { model.back() }
                }
                Spacer()
                if model.step < 3 {
                    Button(model.text("Continue", "继续")) { model.continueToNextStep() }
                        .buttonStyle(.borderedProminent)
                        .disabled(!model.canContinue)
                } else if model.step == 3 {
                    ReviewPrimaryAction(model: model)
                }
            }
        }
        .padding(.top, 8)
    }

    private func showsJob(_ job: YotolioJob) -> Bool {
        switch job.operation {
        case "scan", "prepare":
            return job.state != "completed" && [1, 4].contains(model.step)
        case "auth": return false
        case "upload", "existing", "recreate": return model.step == 4 && job.active
        case "verify": return model.step == 4
        case "inspect": return model.step == 4 && job.active
        default: return false
        }
    }
}

private struct AccountConfirmationView: View {
    @ObservedObject var model: YotolioModel
    let migration: YotolioAccountMigration

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(model.text("One last step to finish connecting", "完成连接还差最后一步"),
                  systemImage: "exclamationmark.circle.fill")
                .font(sectionFont).foregroundColor(coral)
            Text(model.text(
                "Yoto sign-in succeeded. Earlier progress or image choices were found in “\(migration.name)”, but their account is unknown. Use them only if they belong to this account; a wrong choice may create duplicate cards. Otherwise keep them separate; old records are not deleted.",
                "Yoto 登录已成功。发现“\(migration.name)”中有旧上传进度或图片选择，但无法确认属于哪个账号。只有确定属于当前账号时才使用；选错可能导致卡片重复。不确定就分开保存，旧记录不会删除。"))
                .font(noteFont).fixedSize(horizontal: false, vertical: true)
            if migration.remaining > 1 {
                Text(model.text("There are \(migration.remaining) earlier histories to check.",
                                "还有 \(migration.remaining) 份旧记录需要确认。"))
                    .font(noteFont)
            }
            if model.offline {
                HStack {
                    Text(model.text("The local service is unavailable.", "本地服务已断开。"))
                    Button(model.text("Try again", "重试")) { model.refresh() }
                }
                .font(noteFont).foregroundColor(.red)
            } else if !model.errorMessage.isEmpty {
                Text(model.errorMessage).font(noteFont).foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                Button(model.text("Keep separate", "分开保存")) {
                    model.resolveAccount("different")
                }.buttonStyle(.borderedProminent)
                Button(model.text("Use old progress", "使用旧进度")) {
                    model.resolveAccount("same")
                }
                Button(model.text("Cancel login", "取消登录")) {
                    model.resolveAccount("cancel")
                }
            }
            .controlSize(.regular)
            .disabled(model.pending || model.busy || model.offline)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(coral.opacity(0.09))
        .cornerRadius(10)
    }
}

private func messageBox(_ title: String, detail: String, color: Color) -> some View {
    HStack(alignment: .top, spacing: 9) {
        Image(systemName: "exclamationmark.triangle.fill").foregroundColor(color)
        VStack(alignment: .leading, spacing: 5) {
            Text(title).fontWeight(.semibold)
            if !detail.isEmpty { Text(detail).textSelection(.enabled).font(detailFont) }
        }
        Spacer(minLength: 0)
    }
    .padding(14)
    .frame(maxWidth: .infinity, alignment: .leading)
    .background(color.opacity(0.08))
    .cornerRadius(10)
}

private struct StepHeading: View {
    @ObservedObject var model: YotolioModel
    let number: Int
    let title: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.text("STEP \(number) OF 4", "第 \(number) 步，共 4 步"))
                .font(.system(size: 13, weight: .bold, design: .rounded)).foregroundColor(coral)
            Text(title).font(.system(size: 34, weight: .bold, design: .rounded))
            Text(detail).font(detailFont).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct Panel<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }

    var body: some View {
        VStack(alignment: .leading, spacing: 15) { content }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .controlBackgroundColor))
            .cornerRadius(14)
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.black.opacity(0.07)))
    }
}

private struct CollectionView: View {
    @ObservedObject var model: YotolioModel

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            StepHeading(model: model, number: 1,
                        title: model.text("Let's find your collection.", "先找到你的素材合集。"),
                        detail: model.text("Scan a folder for audio, covers and song icons. This only checks local files; it does not search your Yoto account or upload anything.",
                                           "扫描文件夹中的音频、封面和歌曲图标。此操作只检查本地素材，不搜索 Yoto 账号，也不会上传。"))
            Panel {
                Label(model.text("Collection folder", "素材合集文件夹"), systemImage: "folder")
                    .font(sectionFont)
                HStack {
                    TextField(model.text("Choose a folder, or paste its full path", "选择文件夹，或粘贴完整路径"),
                              text: $model.draft.root)
                        .textFieldStyle(.roundedBorder)
                    Button(model.text("Choose folder", "选择文件夹")) { model.chooseFolder() }
                }
                .disabled(model.unavailable)
                Text(model.text("Names, supported audio tags, image sizes and nearby files help identify matches. Unclear matches are shown for review; originals are not moved.",
                                "应用结合文件名、支持的音频标签、图片尺寸和附近素材进行识别；不明确的匹配会列出，原文件不会移动。"))
                    .font(detailFont).foregroundColor(.secondary)
                Text(model.text("Continue to scan this folder automatically. Nothing is uploaded.",
                                "点击“继续”后会自动扫描此文件夹，不会上传任何内容。"))
                    .font(noteFont).foregroundColor(.secondary)
                if model.recoveryNeedsAttention {
                    Text(model.text("Return to Review & upload and resolve the missing-card confirmation first.",
                                    "请先返回“检查与上传”处理缺失卡片确认。"))
                        .font(noteFont).foregroundColor(coral)
                }
            }
        }
    }

}

private struct MetricsView: View {
    @ObservedObject var model: YotolioModel
    var titles: [String]? = nil

    var body: some View {
        let albums = titles.map { names in
            let selected = Set(names)
            return model.albums.filter { selected.contains($0.title) }
        } ?? model.albums
        HStack(spacing: 12) {
            metric(albums.count, model.text("Cards", "张卡片"))
            metric(albums.reduce(0) { $0 + $1.tracks }, model.text("Audio", "首音频"))
            metric(albums.reduce(0) { $0 + $1.icons }, model.text("Icons", "个图标"))
            metric(albums.filter { $0.coverStatus == "provided" }.count, model.text("Covers", "个封面"))
        }
    }

    private func metric(_ value: Int, _ title: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(value)").font(.system(size: 24, weight: .bold, design: .rounded))
            Text(title).font(noteFont).foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(cream)
        .cornerRadius(10)
    }
}

private struct WhiteCard: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 8).fill(Color.white)
            RoundedRectangle(cornerRadius: 8).stroke(Color.black.opacity(0.13))
            VStack(spacing: 5) {
                Circle().fill(coral.opacity(0.9)).frame(width: 15, height: 15)
                Text("MYO").font(.system(size: 8, weight: .bold, design: .rounded))
                    .foregroundColor(Color.gray)
                RoundedRectangle(cornerRadius: 1).fill(Color.gray.opacity(0.22)).frame(width: 24, height: 2)
            }
        }
        .accessibilityLabel("Make Your Own placeholder")
    }
}

private struct CoverView: View {
    @ObservedObject var model: YotolioModel
    let album: YotolioAlbum
    let index: Int
    var showGenerated = false
    var size: CGFloat = 96

    var body: some View {
        Group {
            if album.coverStatus == "provided" || showGenerated {
                if let url = model.coverURL(index: index) {
                    CoverImageView(model: model, url: url, size: size,
                                   label: album.coverStatus == "provided"
                                   ? model.text("Cover for \(album.title)", "\(album.title) 的封面")
                                   : model.text("Generated placeholder cover", "生成的临时封面"))
                } else {
                    CoverPreviewUnavailable(model: model, size: size,
                                            detail: model.text("Could not prepare the local preview address.",
                                                               "无法准备本地预览地址。"))
                }
            } else {
                WhiteCard()
                    .frame(width: size, height: size)
                    .accessibilityLabel(model.text("White card preview; no cover chosen", "白卡预览；尚未选择封面"))
            }
        }
        .frame(width: size)
    }
}

private struct CoverImageView: View {
    @ObservedObject var model: YotolioModel
    let url: URL
    let size: CGFloat
    let label: String
    @StateObject private var loader = CoverPreviewLoader()

    var body: some View {
        Group {
            switch loader.state {
            case .loading:
                ProgressView().controlSize(.small)
                    .frame(width: size, height: size)
                    .accessibilityLabel(model.text("Loading cover preview", "正在加载封面预览"))
            case .loaded(let image):
                let fitted = fittedCoverSize(image.size, maxWidth: size)
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .frame(width: fitted.width, height: fitted.height)
                    .accessibilityLabel(label)
            case .failed(let failure):
                CoverPreviewUnavailable(model: model, size: size, detail: description(failure)) {
                    loader.load(url)
                }
            }
        }
        .onAppear { loader.load(url) }
        .onChange(of: url) { loader.load($0) }
        .onDisappear { loader.cancel() }
    }

    private func description(_ failure: CoverPreviewFailure) -> String {
        switch failure {
        case .transport(let detail):
            return model.text("Local preview request failed: \(detail)", "本地预览请求失败：\(detail)")
        case .http(let code):
            return model.text("Local preview returned HTTP \(code).", "本地预览返回 HTTP \(code)。")
        case .invalidResponse:
            return model.text("The local preview returned an invalid response.", "本地预览返回了无效响应。")
        case .invalidImage:
            return model.text("The preview image could not be decoded.", "无法解码预览图片。")
        }
    }
}

private struct CoverPreviewUnavailable: View {
    @ObservedObject var model: YotolioModel
    let size: CGFloat
    let detail: String
    var retry: (() -> Void)? = nil

    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: "exclamationmark.triangle")
            Text(model.text("Preview unavailable", "预览不可用"))
                .font(.system(size: 11))
                .multilineTextAlignment(.center)
            if let retry {
                Button(model.text("Retry", "重试"), action: retry)
                    .buttonStyle(.link)
                    .font(.system(size: 12))
            }
        }
        .foregroundColor(.red)
        .frame(width: size, height: size)
        .background(Color.red.opacity(0.06))
        .cornerRadius(8)
        .help(detail + " " + model.text("A preview error does not determine the upload result.",
                                      "预览出错不能说明上传结果。"))
    }
}

private struct ReviewView: View {
    @ObservedObject var model: YotolioModel

    var body: some View {
        let submitted = model.presentedUpload
        let processing = submitted?.requested.count ?? model.actionableTitles.count
        let notIncluded = submitted != nil ? (submitted?.reviewSkipped?.count ?? 0)
            : model.currentReview?.state == "completed" ? model.selectedTitles.count - processing : 0
        VStack(alignment: .leading, spacing: 22) {
            StepHeading(model: model, number: 3,
                        title: model.text("Review and upload your cards.", "检查并上传你的卡片。"),
                        detail: model.text("We check each title on Yoto automatically. Choose your cards, then upload here. Existing cards are skipped unless you choose to replace them.",
                                           "应用会自动检查 Yoto 中的同名卡片。选择后即可在这里上传；已有卡默认跳过，只有你选择覆盖才会替换。"))
            if model.preparingReview {
                Panel {
                    HStack {
                        ProgressView().controlSize(.small)
                        Text(model.text("Preparing to check Yoto…", "正在准备检查 Yoto…"))
                            .font(sectionFont)
                    }
                    Text(model.text("This can take several minutes. No upload is happening; keep Yotolio open.",
                                    "检查可能需要几分钟，期间不会上传。请保持 Yotolio 打开。"))
                        .font(noteFont).foregroundColor(.secondary)
                }
            } else if let job = reviewJob {
                JobView(model: model, job: job)
            }
            MetricsView(model: model)
            Panel {
                HStack {
                    Text(submitted == nil
                         ? model.text("\(model.selectedTitles.count) selected", "已选择 \(model.selectedTitles.count) 张")
                         : model.text("\(processing) submitted", "已提交 \(processing) 张"))
                        .font(sectionFont)
                    Spacer()
                    ReviewPrimaryAction(model: model)
                }
                if submitted == nil && model.currentReview?.state == "completed" {
                    Text(model.selectedTitles.isEmpty
                         ? model.text("Select at least one card to continue.", "请至少选择一张卡片。")
                         : model.text("\(processing) to process", "将处理 \(processing) 张"))
                        .font(noteFont).foregroundColor(.secondary)
                }
                if notIncluded > 0 {
                    Text(model.text("Some selected cards (\(notIncluded)) are not included in this task. Check their status below.",
                                    "有 \(notIncluded) 张已选卡片未列入本次任务，请查看下方卡片状态。"))
                        .font(noteFont).foregroundColor(coral)
                }
                HStack {
                    Button(model.text("Select all new", "全选新卡")) { model.selectAllNewCards() }
                        .disabled(model.unavailable || model.selectableNewTitles.isEmpty)
                    Button(model.text("Clear", "清空")) {
                        model.selected.removeAll()
                        model.overwrites.removeAll()
                    }
                        .disabled(model.unavailable || submitted != nil || model.currentReview?.state != "completed"
                                  || model.selected.isEmpty)
                }
                HStack {
                    Button(model.text("Rescan", "重新扫描")) { model.runJob("scan") }
                        .disabled(model.unavailable || model.recoveryNeedsAttention || model.taskStatusUnknown)
                    Button(model.text("Check Yoto again", "重新检查 Yoto")) {
                        model.inspectReviewIfNeeded(force: true)
                    }
                    .disabled(model.unavailable || model.recoveryNeedsAttention || model.taskStatusUnknown
                              || !model.hasCurrentConnection)
                    Spacer()
                }
                if let review = model.currentReview, review.state == "failed", reviewJob?.id != review.id {
                    messageBox(model.text("Yoto check failed", "Yoto 检查失败"),
                               detail: review.error ?? model.text("Check again.", "请重试。"), color: .red)
                } else if model.currentReview == nil && submitted == nil && !model.busy && !model.pending {
                    Text(model.text("Check Yoto again to compare these cards before uploading.",
                                    "上传前请重新检查 Yoto，比较这些卡片。"))
                        .font(noteFont).foregroundColor(.secondary)
                }
                if model.albums.isEmpty {
                    Text(model.busy
                         ? model.text("Scanning your collection…", "正在扫描素材合集…")
                         : model.text("Scan a collection to see its cards.", "扫描素材后，卡片会显示在这里。"))
                        .foregroundColor(.secondary)
                } else {
                    LazyVStack(spacing: 0) {
                        ForEach(Array(model.albums.enumerated()), id: \.element.title) { index, album in
                            reviewRow(album, index: index)
                            .padding(.vertical, 10)
                            if index < model.albums.count - 1 { Divider() }
                        }
                    }
                }
                Text(model.text("A white MYO-style card is a preview only. New cards without a cover receive a colorful generated placeholder.",
                                "白色 MYO 风格卡片仅用于预览。新卡缺少封面时，实际上传的是彩色生成的临时封面。"))
                    .font(noteFont).foregroundColor(.secondary)
                Text(model.text("Yoto → Local compares existing and local source quantities, not file contents or the final uploaded cover. Replacement removes unmatched icons; a missing cover becomes a placeholder.",
                                "Yoto → 本地比较的是现有素材与本地素材的数量，不代表文件内容相同，也不是最终上传封面的数量。覆盖会移除未匹配的图标；缺封面时使用临时封面。"))
                    .font(noteFont).foregroundColor(.secondary)
                Text(model.text("Upload only files you own or may use. Do not edit the same card on Yoto during a task.",
                                "请仅上传你拥有或获准使用的素材；运行时不要在 Yoto 同时编辑同一张卡。"))
                    .font(noteFont).foregroundColor(.secondary)
            }
        }
    }

    private var reviewJob: YotolioJob? {
        guard let job = model.status?.job else { return nil }
        switch job.operation {
        case "scan", "prepare": return job.state == "completed" ? nil : job
        case "upload", "existing", "recreate": return job
        case "inspect":
            return job.active || (job.id == model.currentReview?.id && ["failed", "stopped"].contains(job.state))
                ? job : nil
        default: return nil
        }
    }

    @ViewBuilder private func reviewRow(_ album: YotolioAlbum, index: Int) -> some View {
        if #available(macOS 13.0, *) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .top, spacing: 18) {
                    reviewIdentity(album, index: index).frame(width: 350, alignment: .leading)
                    reviewDetails(album).frame(minWidth: 300, maxWidth: .infinity, alignment: .leading)
                }
                .frame(minWidth: 700, alignment: .leading)
                compactReviewRow(album, index: index)
            }
        } else {
            compactReviewRow(album, index: index)
        }
    }

    private func compactReviewRow(_ album: YotolioAlbum, index: Int) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            reviewIdentity(album, index: index)
            reviewDetails(album).padding(.leading, 32)
        }
    }

    private func reviewIdentity(_ album: YotolioAlbum, index: Int) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Toggle("", isOn: Binding(
                get: { model.presentedUpload?.requested.contains(album.title) ?? model.selected.contains(album.title) },
                set: { model.setSelected(album.title, selected: $0) }
            ))
            .toggleStyle(.checkbox)
            .labelsHidden()
            .disabled(model.unavailable || album.audioIssue != nil || model.currentReview?.state != "completed")
            .accessibilityLabel(model.text("Select \(album.title)", "选择 \(album.title)"))
            .accessibilityHint(model.text("Selecting an existing card chooses its displayed action; confirmation is required before uploading.",
                                          "选择已有卡片会同时选择显示的处理方式；上传前还需确认。"))
            CoverView(model: model, album: album, index: index)
            VStack(alignment: .leading, spacing: 6) {
                Text(album.title).fontWeight(.semibold).lineLimit(3)
                Text(model.text("Local: audio \(album.tracks) · icons \(album.icons) · cover \(album.coverStatus == "provided" ? 1 : 0)",
                                "本地：音频 \(album.tracks) · 图标 \(album.icons) · 封面 \(album.coverStatus == "provided" ? 1 : 0)"))
                    .font(noteFont).foregroundColor(.secondary)
                Text(albumState(album))
                    .font(noteFont).foregroundColor(albumNeedsAttention(album) ? coral : .green)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func reviewDetails(_ album: YotolioAlbum) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let upload = model.presentedUpload {
                Text(uploadStatus(album.title, upload: upload))
                    .font(noteFont).foregroundColor(.secondary)
            } else if let result = model.currentReview?.results?.first(where: { $0.title == album.title }) {
                reviewStatus(result, album: album)
            } else {
                Text(model.busy || model.preparingReview
                     ? model.text("Waiting for Yoto result", "等待 Yoto 检查结果")
                     : model.text("Yoto: not checked", "Yoto：尚未检查"))
                    .font(noteFont).foregroundColor(.secondary)
            }
        }
    }

    private func uploadStatus(_ title: String, upload: YotolioReport) -> String {
        if upload.completed.contains(title) { return model.text("Completed in this batch", "本批次已完成") }
        if upload.skipped.contains(where: { $0.title == title }) { return model.text("Skipped in this batch", "本批次已跳过") }
        if upload.requested.contains(title) {
            return ["running", "stopping"].contains(upload.state)
                ? model.text("Included in this batch", "已列入本批次")
                : model.text("Not completed in this batch", "本批次未完成")
        }
        if upload.reviewSkipped?.contains(title) == true {
            return model.text("Selected, but not included", "已选择，但未列入任务")
        }
        return model.text("Not in this batch", "未列入本批次")
    }

    private func countComparison(_ album: YotolioAlbum, audio: Int, icons: Int, covers: Int) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(model.text("Yoto → Local", "Yoto → 本地"))
                .font(noteFont).foregroundColor(.secondary)
            Text(model.text("Audio \(audio) → \(album.tracks) · Icons \(icons) → \(album.icons) · Cover \(covers) → \(album.coverStatus == "provided" ? 1 : 0)",
                            "音频 \(audio) → \(album.tracks) · 图标 \(icons) → \(album.icons) · 封面 \(covers) → \(album.coverStatus == "provided" ? 1 : 0)"))
                .font(detailFont)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private func reviewStatus(_ result: YotolioCardResult, album: YotolioAlbum) -> some View {
        if let error = result.error {
            let explanation = error == "PREVIOUS_CARD_RENAMED"
                ? model.text("A previously uploaded card is still on Yoto under another name. Check it before creating another.",
                             "之前上传的卡片仍在 Yoto 中，但名称已改变。请检查后再创建。")
                : error
            Text(model.text("Yoto check: \(explanation)", "Yoto 检查：\(explanation)"))
                .font(noteFont).foregroundColor(coral)
        } else if result.presence == "duplicate" {
            Text(model.text("Multiple Yoto cards share this name. Rename or resolve them on Yoto first.",
                            "Yoto 有多张同名卡，请先在 Yoto 中处理。"))
                .font(noteFont).foregroundColor(coral)
        } else if result.presence == "missing" {
            Text(result.pendingOverwrite == true
                 ? model.text("A previous overwrite is unresolved and the card is missing. Check Yoto first.",
                              "上次覆盖尚未确认，而卡片已找不到；请先检查 Yoto。")
                 : result.pendingCreate == true
                 ? model.text("Previous creation needs confirmation", "上次创建需要确认")
                 : model.currentReview?.state != "completed"
                 ? model.text("Not on Yoto", "Yoto 中不存在")
                 : model.selected.contains(album.title)
                 ? model.text("Not on Yoto · will create", "Yoto 中不存在 · 将创建")
                 : model.text("Not on Yoto · not selected", "Yoto 中不存在 · 未选择"))
                .font(noteFont).foregroundColor(result.pendingOverwrite == true || result.pendingCreate == true ? coral : .green)
            countComparison(album, audio: 0, icons: 0, covers: 0)
            if result.pendingCreate == true {
                Text(model.text("An earlier creation was interrupted; its absence must be confirmed before creating again.",
                                "上次创建中断；重新创建前必须确认卡片确实不存在。"))
                    .font(noteFont).foregroundColor(coral)
            }
        } else if result.presence == "present" {
            if let tracks = result.remoteTracks, let icons = result.remoteIcons, let covers = result.remoteCovers {
                countComparison(album, audio: tracks, icons: icons, covers: covers)
            } else {
                Text(model.text("Yoto counts unavailable", "无法读取 Yoto 素材数量"))
                    .font(noteFont).foregroundColor(coral)
            }
            Picker(model.text("Existing card", "已有卡片"), selection: Binding(
                get: { model.overwrites.contains(album.title) ? "replace" : "skip" },
                set: { model.setExistingChoice(album.title, replace: $0 == "replace") }
            )) {
                Text(model.text("Skip", "跳过")).tag("skip")
                Text(result.pendingCreate == true
                     ? model.text("Check previous creation", "核对上次创建")
                     : result.pendingOverwrite == true
                     ? model.text("Check previous replacement", "核对上次覆盖")
                     : model.text("Replace", "覆盖")).tag("replace")
            }
            .pickerStyle(.segmented)
            .disabled(model.unavailable || model.currentReview?.state != "completed" || album.audioIssue != nil)
            if result.pendingOverwrite == true {
                Text(model.text("The previous replacement may still take effect. Check its result; no replacement will be sent again.",
                                "上次覆盖仍可能生效。请核对结果；应用不会再次发送覆盖。"))
                    .font(noteFont).foregroundColor(coral)
            }
        }
    }

    private func albumNeedsAttention(_ album: YotolioAlbum) -> Bool {
        album.audioIssue != nil || album.coverStatus != "provided" || album.icons < album.tracks
            || !album.unmatchedIcons.isEmpty
    }

    private func albumState(_ album: YotolioAlbum) -> String {
        if album.audioIssue != nil { return model.text("Needs attention", "需要处理") }
        if album.coverStatus != "provided" || album.icons < album.tracks || !album.unmatchedIcons.isEmpty {
            return model.text("Missing or unclear materials", "缺少或不明确的素材")
        }
        return model.text("All materials found in scan", "扫描时素材齐全")
    }
}

private struct ConnectView: View {
    @ObservedObject var model: YotolioModel
    @State private var showingSetupGuide = false

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            StepHeading(model: model, number: 2,
                        title: model.text("Connect securely.", "安全地连接 Yoto。"),
                        detail: model.text("Create your own developer application once, then sign in directly on Yoto. Never enter your password here.",
                                           "首次创建自己的开发者应用，然后在 Yoto 网站自行登录；不要在这里填写密码。"))
            Panel {
                instruction("1", model.text("Create your Yoto application", "创建 Yoto 开发者应用"),
                            model.text("In the Dashboard:", "在开发者网站完成以下操作："))
                VStack(alignment: .leading, spacing: 6) {
                    Text(model.text("• Name the application and choose Public Client.",
                                    "• 命名应用，并选择 Public Client。"))
                    Text(model.text("• Add the callback address shown below.",
                                    "• 添加下方显示的回调地址。"))
                    Text(model.text("• Select user:content:manage. In the example, Yoto also selects view and icon permissions.",
                                    "• 勾选 user:content:manage。示例中 Yoto 会同时选择查看和图标权限。"))
                    Text(model.text("• In My Applications, click the copy icon next to your app's Client ID.",
                                    "• 在 My Applications 中，点击应用 Client ID 旁边的复制图标。"))
                }
                .font(detailFont)
                Text(model.text("Allowed callback URL", "允许的回调 URL")).fontWeight(.medium)
                HStack {
                    Text("http://127.0.0.1:\(model.portText)/callback")
                        .font(.system(size: 15, design: .monospaced))
                        .textSelection(.enabled)
                    Button(model.text("Copy", "复制")) { model.copyCallback() }
                }
                DisclosureGroup(model.text("If the callback port is in use", "如果回调端口被占用")) {
                    HStack {
                        Text(model.text("Callback port", "回调端口"))
                        TextField("8788", text: $model.portText).textFieldStyle(.roundedBorder).frame(width: 90)
                    }
                    .disabled(model.unavailable)
                    Text(model.text("Change the port here and register the updated address in the Dashboard.",
                                    "修改这里的端口，并在开发者网站注册更新后的地址。"))
                        .font(noteFont).foregroundColor(.secondary)
                }
                HStack {
                    Button(model.text("Open Yoto Developer Dashboard", "打开 Yoto 开发者网站")) {
                        model.open("https://dashboard.yoto.dev/")
                    }
                    Button(model.text("How do I set this up? View screenshots", "具体怎么设置？查看截图教程")) {
                        showingSetupGuide = true
                    }
                    .foregroundColor(coral)
                }
            }
            Panel {
                instruction("2", model.text("Connect with your public client ID", "使用 Public Client ID 连接"),
                            model.text("This is not a client secret, password or access token.",
                                       "它不是 Client Secret、密码或 Access Token。"))
                TextField(model.text("Your public client ID", "你的 Public Client ID"),
                          text: $model.draft.client_id)
                    .textFieldStyle(.roundedBorder).disabled(model.unavailable)
                HStack {
                    Button(model.status?.connection.saved == true
                           ? model.text("Connect again", "重新连接") : model.text("Connect with Yoto", "连接 Yoto")) {
                        model.runJob("auth")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.unavailable || model.draft.client_id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    connectionLabel
                    Spacer()
                }
                Text(model.text("Yotolio automatically keeps each Yoto account's login and upload progress separate.",
                                "Yotolio 会根据登录的 Yoto 账号自动分开保存登录信息和上传进度。"))
                    .font(noteFont).foregroundColor(.secondary)
                if let error = model.status?.connection.error {
                    messageBox(model.text("Saved login needs attention", "本地登录需要处理"),
                               detail: error, color: .red)
                }
            }
            if model.preparingConnection && !model.accountConfirmationPending {
                HStack {
                    ProgressView().controlSize(.small)
                    Text(model.text("Preparing your connection…", "正在准备连接…"))
                }
                .font(detailFont)
            } else if let job = model.status?.job, job.operation == "auth", !model.accountConfirmationPending {
                JobView(model: model, job: job)
            }
            Panel {
                DisclosureGroup(model.text("Account options", "账号管理")) {
                    VStack(alignment: .leading, spacing: 10) {
                        Button(model.status?.connection.saved == true
                               ? model.text("Switch Yoto account", "切换 Yoto 账号")
                               : model.text("Use a different Yoto account", "使用其他 Yoto 账号")) {
                            model.runJob("auth", switchAccount: true)
                        }
                        .disabled(model.unavailable || model.draft.client_id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        Text(model.text("If Yoto opens the previous account, sign out on Yoto and try again.",
                                        "如果 Yoto 仍打开原账号，请先在 Yoto 网站退出登录，再重试。"))
                            .font(noteFont).foregroundColor(.secondary)
                        if model.status?.connection.saved == true {
                            Button(model.text("Remove saved login", "移除本地登录")) {
                                if confirm(model.text("Remove saved login?", "移除本地登录？"),
                                           model.text("This removes the local token only. Cards and upload progress remain; Yoto access is not revoked.",
                                                      "只删除本地凭据，卡片和上传进度会保留；不会撤销 Yoto 网站中的授权。"),
                                           model.text("Remove login", "移除登录"), model.text("Cancel", "取消")) {
                                    model.disconnect()
                                }
                            }
                            .disabled(model.unavailable)
                        }
                        if let expiry = model.status?.connection.expiresAt {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(model.text("Saved authorization expires:", "本地授权到期时间："))
                                HStack(spacing: 4) {
                                    Text(Date(timeIntervalSince1970: expiry), style: .date)
                                    Text(Date(timeIntervalSince1970: expiry), style: .time)
                                    Text(model.text("(not an online check)", "（非在线检查）"))
                                }
                            }
                            .font(noteFont).foregroundColor(.secondary)
                        }
                    }
                    .padding(.top, 8)
                }
                DisclosureGroup(model.text("If icon permissions are denied", "如果图标权限被拒绝")) {
                    Toggle(model.text("Require icon access when connecting", "连接时检查图标权限"),
                           isOn: $model.draft.icons_scope)
                        .toggleStyle(.checkbox).disabled(model.unavailable)
                    Text(model.text("Yotolio requests icon access automatically. When Yoto reports only the parent permission, this option checks the icon list; uploading is confirmed separately.",
                                    "Yotolio 会自动申请图标权限。如果 Yoto 只返回上级权限，此选项会检查能否读取图标列表；上传是否成功仍需单独确认。"))
                        .font(noteFont).foregroundColor(.secondary)
                }
            }
        }
        .sheet(isPresented: $showingSetupGuide) {
            SetupDiagramView(model: model)
        }
    }

    private func instruction(_ number: String, _ title: String, _ detail: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text(number).font(.system(size: 14, weight: .bold, design: .rounded)).foregroundColor(.white)
                .frame(width: 24, height: 24).background(coral).clipShape(Circle())
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(sectionFont)
                Text(detail).font(detailFont).foregroundColor(.secondary)
            }
        }
    }

    private var connectionLabel: some View {
        let connection = model.status?.connection
        let connecting = model.preparingConnection
            || (model.status?.job?.operation == "auth" && model.status?.job?.active == true)
        let current = model.hasCurrentConnection && !connecting
        let label = model.accountConfirmationPending ? model.text("Confirmation needed", "需要确认旧记录")
            : connecting ? model.text("Connecting…", "正在连接…")
            : current ? model.text("Login saved", "已保存登录")
            : connection?.saved == true ? model.text("Reconnect required", "需要重新连接")
            : model.text("Not connected", "尚未连接")
        return Label(label, systemImage: current ? "checkmark.circle.fill" : "circle")
            .font(noteFont).foregroundColor(current ? .green : .secondary)
    }
}

private struct SetupDiagramView: View {
    @ObservedObject var model: YotolioModel
    @Environment(\.dismiss) private var dismiss

    private func screenshot(_ name: String) -> NSImage? {
        let url = Bundle.main.bundleURL.deletingLastPathComponent()
            .appendingPathComponent("web/setup-\(name).png")
        return NSImage(contentsOf: url)
    }

    private var examples: [(name: String, title: String, detail: String)] {
        [
            ("create", model.text("1. Name the application and choose Public Client", "1. 命名应用并选择 Public Client"),
             model.text("Give the application a name. Select Public Client, not Confidential Client.",
                        "为应用填写名称，选择 Public Client，不要选择 Confidential Client。")),
            ("callback", model.text("2. Add the callback", "2. 添加回调地址"),
             model.text("Paste the exact callback shown in Yotolio into Allowed Callback URLs. Your port may differ from this example.",
                        "将 Yotolio 显示的完整回调地址粘贴到 Allowed Callback URLs。你的端口可能与截图不同。")),
            ("scopes", model.text("3. Select permissions and create", "3. 选择权限并创建"),
             model.text("Select user:content:manage. In this example, Yoto includes user:content:view and user:icons:manage automatically. Ensure view and manage are approved; read the terms before agreeing.",
                        "勾选 user:content:manage。此截图中 Yoto 自动包含 user:content:view 和 user:icons:manage。请确保 view 和 manage 已获批准，并在同意前阅读条款。")),
        ]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(model.text("Create your Yoto application", "创建 Yoto 开发者应用"))
                    .font(.system(size: 22, weight: .bold, design: .rounded))
                Spacer()
                Button(model.text("Close", "关闭")) { dismiss() }
            }
            Text(model.text("Steps 1–3 show real Yoto Dashboard screenshots; step 4 is an illustration. Yoto's website may change; always copy the current callback shown in Yotolio.",
                            "步骤 1–3 是 Yoto 开发者网站真实截图；步骤 4 为示意图。网站界面可能变化；请始终复制 Yotolio 当前显示的回调地址。"))
                .font(detailFont).foregroundColor(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    ForEach(examples, id: \.name) { example in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(example.title).font(sectionFont)
                            Text(example.detail).font(detailFont).foregroundColor(.secondary)
                            if let image = screenshot(example.name) {
                                Image(nsImage: image).resizable().scaledToFit()
                                    .accessibilityLabel(example.title)
                            } else {
                                messageBox(model.text("The setup screenshot could not be opened.", "无法打开配置截图。"),
                                           detail: model.text("Keep the web folder next to the app.", "请将 web 文件夹与应用放在一起。"),
                                           color: .red)
                            }
                        }
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        Text(model.text("4. Copy your public client ID", "4. 复制 Public Client ID")).font(sectionFont)
                        Text(model.text("In My Applications, click the copy icon next to your app's Client ID, then paste it into Yotolio. Never paste a client secret or password.",
                                        "在 My Applications 中点击应用 Client ID 旁边的复制图标，再粘贴到 Yotolio。不要填写 Client Secret 或密码。"))
                            .font(detailFont).foregroundColor(.secondary)
                        clientIDExample
                    }
                }
            }
        }
        .padding(24)
        .frame(minWidth: 680, idealWidth: 820, minHeight: 540, idealHeight: 760)
    }

    private var clientIDExample: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(model.text("Illustration only — your Dashboard may look different",
                            "示意图 — 你的开发者网站界面可能不同"))
                .font(noteFont).foregroundColor(.secondary)
            Text("My Applications").font(sectionFont)
            Divider()
            HStack(alignment: .center, spacing: 16) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(model.text("Example application", "示例应用")).fontWeight(.semibold)
                    Text("Client ID").font(noteFont).foregroundColor(.secondary)
                    Text("example-client-id").font(.system(size: 14, design: .monospaced))
                }
                Spacer()
                Image(systemName: "doc.on.doc")
                    .font(.system(size: 19, weight: .semibold)).foregroundColor(coral)
                    .padding(10).overlay(RoundedRectangle(cornerRadius: 8).stroke(coral))
                    .accessibilityLabel(model.text("Copy icon", "复制图标"))
            }
            Text(model.text("Click the copy icon next to your own Client ID.",
                            "点击你自己的 Client ID 旁边的复制图标。"))
                .font(noteFont).foregroundColor(.secondary)
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(cream).cornerRadius(10)
    }
}

private struct ReviewPrimaryAction: View {
    @ObservedObject var model: YotolioModel

    var body: some View {
        if !model.selectedTitles.isEmpty && model.currentReview?.state == "completed"
            && model.actionableTitles.isEmpty {
            Button(model.text("View results without uploading", "查看结果，不上传")) {
                model.continueToNextStep()
            }
            .buttonStyle(.borderedProminent)
            .disabled(!model.canContinue)
        } else {
            UploadButton(model: model)
        }
    }
}

private struct UploadButton: View {
    @ObservedObject var model: YotolioModel

    var body: some View {
        Button(model.text("Upload selected cards", "上传所选卡片")) {
            let titles = model.actionableTitles
            let existing = titles.filter { model.overwrites.contains($0) }
            let reconcilingCreates = existing.filter { title in
                model.currentReview?.results?.first { $0.title == title }?.pendingCreate == true
            }
            let reconcilingOverwrites = existing.filter { title in
                model.currentReview?.results?.first { $0.title == title }?.pendingOverwrite == true
            }
            let replacing = existing.filter {
                !reconcilingCreates.contains($0) && !reconcilingOverwrites.contains($0)
            }
            let creating = titles.filter { !existing.contains($0) }
            var parts: [String] = []
            if !creating.isEmpty {
                parts.append(model.text("Create these new cards:\n\(creating.joined(separator: "\n"))",
                                        "创建以下新卡片：\n\(creating.joined(separator: "\n"))"))
            }
            if !replacing.isEmpty {
                parts.append(model.text("Replace these existing cards?\n\(replacing.joined(separator: "\n"))\n\nReplacement uses local audio, cover and icons; unmatched icons are removed and a missing cover becomes a placeholder. Avoid concurrent edits on Yoto.",
                                        "确认覆盖以下已有卡片？\n\(replacing.joined(separator: "\n"))\n\n覆盖会使用本地音频、封面和图标；未匹配的图标会移除，缺封面时使用临时封面。请勿同时在 Yoto 编辑这些卡片。"))
            }
            if !reconcilingCreates.isEmpty {
                parts.append(model.text("Check these interrupted creations against saved evidence? They will not be replaced:\n\(reconcilingCreates.joined(separator: "\n"))",
                                        "根据保存的证据核对以下中断的创建吗？这些卡片不会被覆盖：\n\(reconcilingCreates.joined(separator: "\n"))"))
            }
            if !reconcilingOverwrites.isEmpty {
                parts.append(model.text("Check these previous replacements against saved evidence? No replacement will be sent again:\n\(reconcilingOverwrites.joined(separator: "\n"))",
                                        "根据保存的证据核对以下上次覆盖吗？不会再次发送覆盖：\n\(reconcilingOverwrites.joined(separator: "\n"))"))
            }
            let detail = parts.joined(separator: "\n\n")
            if confirm(model.text("Start this Yoto account task?", "确认在 Yoto 账号执行此操作？"),
                       detail, model.text("Start", "开始"), model.text("Cancel", "取消")) {
                model.runJob("upload", titles: titles, uploadConfirmed: true)
            }
        }
        .buttonStyle(.borderedProminent).disabled(!model.canUpload)
    }
}

private struct JobView: View {
    @ObservedObject var model: YotolioModel
    let job: YotolioJob

    var body: some View {
        Panel {
            HStack {
                if job.active { ProgressView().controlSize(.small) }
                Text(stateTitle).font(sectionFont)
                Spacer()
                if job.active && ["scan", "prepare", "auth", "upload", "existing", "verify", "inspect", "recreate"].contains(job.operation) {
                    Button(["scan", "prepare"].contains(job.operation) || job.phase == "account-scan" ? model.text("Cancel scan", "取消扫描")
                           : job.operation == "auth" ? model.text("Cancel connection", "取消连接")
                           : model.text("Pause after this card", "完成当前卡片后暂停")) {
                        if ["scan", "prepare"].contains(job.operation) || job.phase == "account-scan" {
                            model.stopJob()
                            return
                        }
                        let detail = job.operation == "auth"
                            ? model.text("Cancel the connection attempt?", "取消本次连接？")
                            : job.operation == "inspect"
                            ? model.text("The current card will finish checking first. No upload is happening.",
                                         "会先完成当前卡片的检查，期间不会上传。")
                            : ["upload", "existing"].contains(job.operation)
                            ? model.text("The current card will finish first. To continue, check Yoto again, select the remaining cards and confirm another upload.",
                                         "当前卡片会先完成。继续时请重新检查 Yoto，选择剩余卡片并确认上传。")
                            : model.text("The current card will finish first. Progress stays saved.",
                                         "当前卡片会先完成，进度会保留。")
                        if confirm(model.text("Stop this task?", "停止此任务？"), detail,
                                   model.text("Stop", "停止"), model.text("Keep running", "继续运行")) {
                            model.stopJob()
                        }
                    }
                    .disabled(model.pending || job.state == "stopping")
                }
            }
            if !job.currentAlbum.isEmpty {
                Text(job.operation == "inspect"
                     ? model.text("Last checked: \(job.currentAlbum)", "最近检查完成：\(job.currentAlbum)")
                     : job.currentAlbum)
                    .fontWeight(.semibold)
            }
            if job.showsPhase && (job.operation != "inspect" || job.active) {
                Text(phaseTitle).font(noteFont).foregroundColor(.secondary)
            }
            if (["scan", "prepare"].contains(job.operation) || job.phase == "account-scan") && job.active {
                ProgressView().progressViewStyle(.linear)
                Text(model.text("Finding audio and images. The scan time depends on your files.",
                                "正在查找音频和图片，扫描时间取决于素材数量。"))
                    .font(noteFont).foregroundColor(.secondary)
            }
            if ["upload", "existing", "verify", "inspect", "recreate"].contains(job.operation) {
                if job.operation == "inspect" && job.total > 0 {
                    ProgressView(value: Double(job.completed.count), total: Double(job.total))
                } else if job.operation == "inspect" {
                    ProgressView().progressViewStyle(.linear)
                } else {
                    ProgressView(value: job.progress)
                }
                Text(job.operation == "inspect"
                     ? model.text("\(job.completed.count) of \(job.total) cards checked",
                                  "已检查 \(job.completed.count) / \(job.total) 张卡片")
                     : model.text("\(job.completed.count) completed · \(job.skipped.count) skipped · \(job.total) selected",
                                  "已完成 \(job.completed.count) 张 · 已跳过 \(job.skipped.count) 张 · 共选 \(job.total) 张"))
                    .font(noteFont).foregroundColor(.secondary)
                if job.active && job.operation == "inspect" {
                    Text(model.text("Checking Yoto can take several minutes. No upload is happening; keep Yotolio open.",
                                    "检查 Yoto 可能需要几分钟，期间不会上传。请保持 Yotolio 打开。"))
                        .font(noteFont).foregroundColor(.secondary)
                }
                if job.audioTotal > 0 && job.active {
                    Text(model.text("Current card: \(job.audioDone) of \(job.audioTotal) audio files processed",
                                    "当前卡片已处理 \(job.audioDone) / \(job.audioTotal) 首音频"))
                        .font(noteFont).foregroundColor(.secondary)
                }
                if !job.skipped.isEmpty {
                    Text(model.text("\(job.skipped.count) cards skipped:",
                                    "已跳过 \(job.skipped.count) 张卡片："))
                        .font(noteFont).foregroundColor(coral)
                    ForEach(Array(job.skipped.enumerated()), id: \.offset) { _, item in
                        Text("\(item.title): \(skipReason(item.reason))")
                            .font(noteFont).foregroundColor(coral)
                    }
                }
                if job.active && job.operation != "inspect" {
                    Text(model.text("Pausing waits for the current card; transcoding may take several minutes.",
                                    "暂停会等待当前卡片完成；转码可能需要数分钟。"))
                        .font(noteFont).foregroundColor(.secondary)
                }
            }
            if job.operation == "auth", job.phase == "auth", let login = job.loginUrl, job.state == "running" {
                Button(model.text("Open Yoto login", "打开 Yoto 登录页")) { model.open(login, login: true) }
                    .buttonStyle(.borderedProminent)
                Text(model.text("Yoto should open automatically. If it did not, use this button; then sign in and return here.",
                                "Yoto 登录页应会自动打开；如果没有，请点击此按钮。登录授权后返回应用。"))
                    .font(noteFont).foregroundColor(.secondary)
            }
            if let recovery = job.recovery, job.state == "failed", recovery.type == "missing_prior_card" {
                recoveryPanel(recovery)
            } else if job.error != nil {
                messageBox(model.text("Task failed", "任务失败"), detail: model.jobError(job), color: .red)
            }
            if job.operation == "recreate" && job.state == "completed" {
                Text(model.text("This task covers only this card. Return to Review and check the remaining cards before uploading again.",
                                "这次只处理这一张卡。请返回“检查卡片”，核对其余卡片后再上传。"))
                    .font(noteFont).foregroundColor(.secondary)
            }
            if model.step == 3 && ["upload", "existing"].contains(job.operation)
                && ["stopped", "failed"].contains(job.state) && job.recovery == nil {
                Text(model.text("To continue, check Yoto again, select the remaining cards and confirm another upload. Saved progress is used for reconciliation.",
                                "继续处理时，请先重新检查 Yoto，再选择剩余卡片并确认上传；应用会用已保存的进度核对。"))
                    .font(noteFont).foregroundColor(.secondary)
            }
        }
    }

    private func recoveryPanel(_ recovery: YotolioRecovery) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(model.text("A previously recorded card was not found", "未找到之前记录的卡片")).font(sectionFont)
            Text(model.text("Yotolio recorded a previous creation for “\(recovery.title)”, but cannot find it now. It stopped instead of creating a possible duplicate.",
                            "Yotolio 记录过“\(recovery.title)”的创建，但现在找不到它。为避免可能重复，已停止创建。"))
            if recovery.priorStatus == "pending_create" {
                Text(model.text("The earlier creation did not finish, so a card could still appear later.",
                                "上次创建没有完成，卡片之后仍可能出现。"))
                    .fontWeight(.semibold)
            }
            Text(model.text("Open Yoto and check the same account. Confirm that no copy, including a renamed copy, remains. If a recent change may not have appeared yet, wait and refresh. Do not continue if you are unsure.",
                            "请打开 Yoto，检查同一个账号，确认没有这张卡的任何副本，包括改名后的卡。如果最近的更改可能尚未显示，请等待并刷新。不确定时请不要继续。"))
            HStack {
                Button(model.text("Open my Yoto cards", "打开我的 Yoto 卡片")) {
                    model.open("https://my.yotoplay.com/cards")
                }
                Button(model.text("Re-create this card", "重新创建这张卡")) {
                    let detail = model.text(
                        "I checked the same Yoto account and confirmed that no copy or renamed copy of “\(recovery.title)” remains. Create this card again? This retries only this card; afterwards, check Yoto again before selecting the remaining cards. Concurrent or delayed Yoto changes can still cause duplicates.",
                        "我已检查同一个 Yoto 账号，确认“\(recovery.title)”及其改名后的副本都不存在。现在重新创建吗？这次只处理这一张；之后请重新检查 Yoto，再选择其余卡片。并发操作或延迟生效的更改仍可能导致重复。")
                    if confirm(model.text("Re-create this card?", "重新创建这张卡？"), detail,
                               model.text("Re-create", "重新创建"), model.text("Cancel", "取消")) {
                        model.runJob("recreate", titles: [recovery.title], recoveryJobID: job.id, recoveryConfirmed: true)
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!model.canRecover(recovery.title))
            }
        }
        .font(detailFont).padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.08)).cornerRadius(10)
    }

    private var stateTitle: String {
        switch job.state {
        case "running": return ["scan", "prepare"].contains(job.operation)
            ? model.text("Scanning your files…", "正在扫描素材…")
            : job.operation == "inspect" ? model.text("Checking Yoto…", "正在检查 Yoto…")
            : model.text("Working", "正在处理")
        case "stopping": return model.text("Finishing the current task…", "正在完成当前任务…")
        case "completed": return model.text("Task completed", "操作已完成")
        case "stopped": return model.text("Paused", "已暂停")
        default: return model.text("Something needs your attention", "有一处需要你检查")
        }
    }

    private var phaseTitle: String {
        switch job.phase {
        case "prepare", "scan": return model.text("Checking local files", "正在检查本地素材")
        case "prepared": return model.text("Local collection checks completed", "本地素材检查已完成")
        case "auth": return model.text("Waiting for Yoto login", "等待完成 Yoto 登录")
        case "account-identifying": return model.text("Checking your Yoto account", "正在识别 Yoto 账号")
        case "account-scan": return model.text("Checking files for this Yoto account", "正在检查此 Yoto 账号的本地素材")
        case "account-confirmation": return model.text("Confirm the earlier saved progress above", "请在上方确认以前保存的进度")
        case "connected": return model.text("Authorization saved", "授权已保存")
        case "audio": return model.text("Uploading or transcoding audio", "正在上传或转码音频")
        case "cover": return model.text("Cover uploaded", "封面已上传")
        case "icons": return model.text("Song icons uploaded", "歌曲图标已上传")
        case "saving": return model.text("Saving the card", "正在保存卡片")
        case "verified": return model.text("Card checked", "当前卡片已核验")
        case "verify": return model.text("Reading and checking live cards", "正在读取并核验线上卡片")
        case "inspect", "inspected": return model.text("Reading card details from Yoto", "正在读取 Yoto 卡片信息")
        case "recreate": return model.text("Rechecking the missing card", "正在重新检查缺失卡片")
        default: return model.text("Reading existing cards", "正在读取已有卡片")
        }
    }

    private func skipReason(_ reason: String) -> String {
        switch reason {
        case "not_existing": return model.text("not found on Yoto", "Yoto 上不存在")
        case "already_existing": return model.text("already on Yoto; left unchanged", "Yoto 上已存在，未修改")
        case "missing": return model.text("no audio", "没有音频")
        case "too_many": return model.text("more than 100 tracks", "超过 100 首")
        case "untagged_in_mixed_folder": return model.text("audio grouping is unclear", "音频分组不明确")
        default: return reason
        }
    }
}

private struct OverviewView: View {
    @ObservedObject var model: YotolioModel

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            StepHeading(model: model, number: 4,
                        title: model.text("Your card results.", "你的卡片结果。"),
                        detail: model.text("Check whether each card is on Yoto and review its audio and song icons.",
                                           "查看卡片是否存在于 Yoto，并逐首检查音频与歌曲图标。"))
            Panel {
                if model.viewingReadOnlyResults {
                    Text(model.text("No cards were submitted · \(model.overviewTitles.count) selected",
                                    "未提交任何卡片 · 共选 \(model.overviewTitles.count) 张"))
                        .font(sectionFont)
                    if model.currentInspection == nil {
                        messageBox(model.text("The check is no longer current", "检查结果已失效"),
                                   detail: model.text("Return to Review and check Yoto again.",
                                                      "请返回“检查卡片”并重新检查 Yoto。"), color: .red)
                    }
                } else if let upload = model.overviewUpload {
                    Text(model.text("Latest batch: \(upload.completed.count) processed · \(upload.skipped.count) skipped · \(upload.unfinishedCount) unfinished · \(upload.requested.count) submitted",
                                    "最近一批：已处理 \(upload.completed.count) 张 · 已跳过 \(upload.skipped.count) 张 · 未完成 \(upload.unfinishedCount) 张 · 已提交 \(upload.requested.count) 张"))
                        .font(sectionFont)
                    if let notSubmitted = upload.reviewSkipped, !notSubmitted.isEmpty {
                        Text(model.text("Some selected cards (\(notSubmitted.count)) were not submitted. Check their status below.",
                                        "有 \(notSubmitted.count) 张已选卡片未列入任务，请查看下方卡片状态。"))
                            .font(noteFont).foregroundColor(coral)
                    }
                    if upload.state == "running" || upload.state == "stopping" {
                        Text(model.text("Upload is still running. The online check will start after it completes successfully.",
                                        "上传仍在进行，成功完成后会自动检查线上卡片。"))
                            .font(detailFont).foregroundColor(.secondary)
                    } else if upload.state == "failed", upload.error != nil, !model.recoveryNeedsAttention {
                        messageBox(model.text("The batch did not finish", "此批次未完成"),
                                   detail: model.reportError(upload), color: .red)
                    } else if upload.state == "stopped" {
                        Text(model.text("The batch was paused; unprocessed cards are not counted as uploaded.",
                                        "此批次已暂停，尚未处理的卡片不算上传成功。"))
                            .font(detailFont).foregroundColor(.secondary)
                    }
                } else {
                    Text(model.text("No upload in this session. Showing the cards currently selected for upload.",
                                    "本次会话尚未上传；下方显示当前选中的卡片。"))
                        .font(sectionFont)
                }
                Text(model.text("This overview is session-only and clears when reconnecting or changing collections. Saved upload progress remains.",
                                "此总览仅保留在本次会话中，重新连接或更换素材合集时会清除；已保存的上传进度仍会保留。"))
                    .font(noteFont).foregroundColor(.secondary)
                if model.recoveryNeedsAttention {
                    messageBox(model.text("A previous card needs confirmation first", "请先确认之前的卡片"),
                               detail: model.text("Return to Review & upload and resolve the missing-card warning before checking results.",
                                                  "请返回“检查与上传”处理缺失卡片提示，然后再检查结果。"), color: .red)
                    Button(model.text("Return to review & upload", "返回检查与上传")) { model.navigate(to: 3) }
                } else if !model.hasCurrentConnection {
                    Text(model.text("Connect Yoto to check whether these cards exist online.",
                                    "连接 Yoto 后才能检查这些卡片是否在线上存在。"))
                        .font(detailFont).foregroundColor(.secondary)
                } else if let inspection = model.currentInspection, inspection.state == "failed", let error = inspection.error {
                    messageBox(model.text("The online check did not finish", "线上检查未完成"), detail: error, color: .red)
                }
                HStack {
                    Button(model.text("Check Yoto again", "重新检查 Yoto")) {
                        model.recheckOverview()
                    }
                    .disabled(model.unavailable || model.taskStatusUnknown || !model.canNavigate(to: 3)
                              || model.recoveryNeedsAttention || model.inspectableTitles.isEmpty)
                    Button(model.text("Open my Yoto cards", "打开我的 Yoto 卡片")) {
                        model.open("https://my.yotoplay.com/cards")
                    }
                }
                Text(model.text("The check is read-only. Source matches require saved upload evidence; a matching name alone is not proof.",
                                "此检查只读取数据。只有保存了上传证据才能核对源文件；名称相同不等于文件已匹配。"))
                    .font(noteFont).foregroundColor(.secondary)
            }
            Panel {
                HStack {
                    Text(model.text("Cards in this batch", "本批卡片")).font(sectionFont)
                    Spacer()
                    Button(model.text("Rescan", "重新扫描")) { model.runJob("scan") }
                        .disabled(model.unavailable || model.draft.root.isEmpty || model.recoveryNeedsAttention
                                  || model.taskStatusUnknown)
                }
                Text(model.text("Images shown are local previews. Online labels describe the latest read-only check.",
                                "图片显示本地预览；线上状态来自最近一次只读检查。"))
                    .font(noteFont).foregroundColor(.secondary)
                if let error = model.status?.assetStatusError {
                    messageBox(model.text("Saved upload evidence needs attention", "已保存的上传证据需要处理"),
                               detail: error, color: .red)
                }
                if model.overviewTitles.isEmpty {
                    Text(model.text("Select cards in Review to see them here.", "请在“检查卡片”中选择卡片。"))
                        .foregroundColor(.secondary)
                } else {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(model.overviewTitles.enumerated()), id: \.offset) { position, title in
                            if let index = model.albums.firstIndex(where: { $0.title == title }) {
                                OverviewAlbumView(model: model, album: model.albums[index], index: index,
                                                  result: model.currentInspection?.results?.first { $0.title == title })
                            } else {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(title).fontWeight(.semibold)
                                    Text(model.text("This source is no longer in the latest scan; it was not checked.",
                                                    "最近一次扫描已找不到此素材，未检查这张卡。"))
                                        .font(noteFont).foregroundColor(coral)
                                }
                                .padding(.vertical, 14)
                            }
                            if position < model.overviewTitles.count - 1 { Divider() }
                        }
                    }
                }
            }
        }
    }
}

private struct OverviewAlbumView: View {
    @ObservedObject var model: YotolioModel
    let album: YotolioAlbum
    let index: Int
    let result: YotolioCardResult?

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            CoverView(model: model, album: album, index: index, showGenerated: result?.cover == "placeholder", size: 104)
            VStack(alignment: .leading, spacing: 7) {
                Text(album.title).fontWeight(.semibold)
                Text(uploadOutcome).font(noteFont).foregroundColor(.secondary)
                Text(presence).font(detailFont).fontWeight(.medium)
                    .foregroundColor(result?.presence == "present" && result?.error == nil ? .green : coral)
                Text(model.text("Local preview · \(album.tracks) audio · \(album.icons) icons",
                                "本地预览 · \(album.tracks) 首音频 · \(album.icons) 个图标"))
                    .font(noteFont).foregroundColor(.secondary)
                if let result {
                    Text(coverResult(result.cover)).font(noteFont).foregroundColor(.secondary)
                    if let count = result.remoteTracks {
                        Text(model.text("Yoto tracks: \(count)", "Yoto 中的音频：\(count) 首"))
                            .font(noteFont).foregroundColor(.secondary)
                    }
                    if let error = result.error {
                        messageBox(model.text("Yoto card details could not be checked", "无法检查 Yoto 卡片详情"),
                                   detail: error, color: .red)
                    }
                }
                if let issue = album.audioIssue {
                    issueText(audioIssue(issue))
                }
                if album.coverStatus != "provided" {
                    issueText(coverIssue)
                    if !album.coverCandidates.isEmpty {
                        issueText(model.text("Possible covers: \(fileNames(album.coverCandidates))",
                                             "可能的封面：\(fileNames(album.coverCandidates))"))
                    }
                }
                if !album.unmatchedIcons.isEmpty {
                    issueText(model.text("Unmatched images (possible icons): \(fileNames(album.unmatchedIcons))",
                                         "未匹配的图片（可能是图标）：\(fileNames(album.unmatchedIcons))"))
                }
                if album.coverNeedsUpload == true {
                    issueText(model.text("A real cover is ready. To use it, rescan and choose Replace in Review.",
                                         "真实封面已就绪；如需使用，请重新扫描并在检查页选择覆盖。"))
                }
                if let count = album.iconsNeedUpload, count > 0 {
                    issueText(model.text("\(count) newly available icons are ready to upload.",
                                         "\(count) 个新找到的图标等待补传。"))
                }
                if album.pendingCoverUpdate == true {
                    issueText(model.text("A previous cover update needs reconciliation; resume Upload with this Yoto account.",
                                         "之前的封面更新需要继续核对，请使用此 Yoto 账号恢复上传。"))
                }
                Button(model.text("Choose cover", "选择封面")) {
                    model.chooseAsset(album: album, kind: "cover")
                }
                .disabled(model.unavailable || model.recoveryNeedsAttention)
                if !album.trackDetails.isEmpty || !(result?.tracks.isEmpty ?? true) {
                    let missing = album.trackDetails.filter { !$0.hasIcon }.count
                    DisclosureGroup(missing > 0
                                    ? model.text("\(missing) songs need icons", "\(missing) 首歌曲需要图标")
                                    : model.text("Review song icons", "查看歌曲图标")) {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(Array(album.trackDetails.enumerated()), id: \.offset) { trackIndex, track in
                                HStack(alignment: .top) {
                                    iconPreview(track, trackIndex: trackIndex)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(track.name).fontWeight(.medium)
                                        Text(trackIssue(track)).font(noteFont).foregroundColor(.secondary)
                                        if let remote = result?.tracks.indices.contains(trackIndex) == true
                                            ? result?.tracks[trackIndex] : nil {
                                            Text(model.text("Audio: \(audioResult(remote.audio)) · Icon: \(iconResult(remote.icon))",
                                                            "音频：\(audioResult(remote.audio)) · 图标：\(iconResult(remote.icon))"))
                                                .font(noteFont).foregroundColor(.secondary)
                                            if let title = remote.remoteTitle, title != track.name {
                                                Text(model.text("Yoto song: \(title)", "Yoto 歌曲：\(title)"))
                                                    .font(noteFont).foregroundColor(coral)
                                            }
                                        } else {
                                            Text(model.text("Online audio and icon not checked", "线上音频和图标尚未检查"))
                                                .font(noteFont).foregroundColor(.secondary)
                                        }
                                        if !track.candidates.isEmpty {
                                            Text(model.text("Possible images: \(fileNames(track.candidates))",
                                                            "可能的图片：\(fileNames(track.candidates))"))
                                                .font(noteFont).foregroundColor(.secondary)
                                        }
                                    }
                                    Spacer()
                                    Button(model.text("Choose icon", "选择图标")) {
                                        model.chooseAsset(album: album, kind: "icon", track: trackIndex)
                                    }
                                    .disabled(model.unavailable || model.recoveryNeedsAttention)
                                }
                            }
                            if let result, result.tracks.count > album.trackDetails.count {
                                ForEach(Array(result.tracks.dropFirst(album.trackDetails.count).enumerated()), id: \.offset) { _, track in
                                    Text(model.text("Extra Yoto song: \(track.remoteTitle ?? track.name)",
                                                    "Yoto 中多出的歌曲：\(track.remoteTitle ?? track.name)"))
                                        .font(noteFont).foregroundColor(coral)
                                }
                            }
                        }
                        .padding(.top, 8)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 14)
    }

    private var uploadOutcome: String {
        if model.viewingReadOnlyResults {
            return model.text("Not submitted for processing", "未提交处理")
        }
        guard let upload = model.overviewUpload else {
            return model.text("Not uploaded in this session", "本次会话尚未上传")
        }
        if upload.completed.contains(album.title) { return model.text("Batch: processed", "本批：已处理") }
        if upload.reviewSkipped?.contains(album.title) == true {
            return model.text("Batch: not sent for processing", "本批：未提交处理")
        }
        if let skipped = upload.skipped.first(where: { $0.title == album.title }) {
            let reason = skipped.reason == "already_existing"
                ? model.text("already on Yoto", "Yoto 上已存在")
                : skipped.reason == "not_existing" ? model.text("not on Yoto", "Yoto 上不存在") : skipped.reason
            return model.text("Batch: skipped — \(reason)", "本批：已跳过 — \(reason)")
        }
        return model.text("Batch: not completed", "本批：尚未完成")
    }

    private var presence: String {
        guard let result else {
            return model.currentInspection?.state == "running"
                ? model.text("Checking Yoto…", "正在检查 Yoto…")
                : model.text("Yoto presence not checked", "尚未检查 Yoto 是否存在")
        }
        switch result.presence {
        case "present": return model.text("Found in Yoto library", "Yoto 卡库中存在")
        case "missing": return model.text("Not found in Yoto library", "Yoto 卡库中未找到")
        default: return model.text("Multiple cards have this title", "Yoto 中有多张同名卡片")
        }
    }

    private func coverResult(_ value: String) -> String {
        switch value {
        case "matched": return model.text("Cover matches saved upload evidence", "封面与保存的上传证据一致")
        case "placeholder": return model.text("Yoto has this generated placeholder cover", "Yoto 使用此生成的临时封面")
        case "different": return model.text("Yoto cover differs from this local preview", "Yoto 封面与此本地预览不同")
        case "missing": return model.text("No Yoto cover found", "Yoto 卡片没有封面")
        default: return model.text("Yoto cover match not verified", "尚无法核实 Yoto 封面是否匹配")
        }
    }

    private func audioResult(_ value: String) -> String {
        switch value {
        case "matched": return model.text("source matched", "源文件已匹配")
        case "different": return model.text("different", "不一致")
        case "missing": return model.text("missing on Yoto", "Yoto 中缺失")
        case "name_only": return model.text("name only; source unverified", "仅名称相同，源文件未核实")
        case "name_mismatch": return model.text("name differs; source unverified", "名称不同，源文件未核实")
        default: return model.text("unverified", "未核实")
        }
    }

    private func iconResult(_ value: String) -> String {
        switch value {
        case "matched": return model.text("matched", "已匹配")
        case "missing": return model.text("missing on Yoto", "Yoto 中缺失")
        case "different": return model.text("different", "不一致")
        case "partial": return model.text("missing on chapter or track", "章节或歌曲图标缺失")
        case "remote_only": return model.text("on Yoto; no local match", "Yoto 有图标，本地未匹配")
        case "song_mismatch": return model.text("song order/name differs", "歌曲顺序或名称不一致")
        default: return model.text("unverified", "未核实")
        }
    }

    @ViewBuilder private func iconPreview(_ track: YotolioTrack, trackIndex: Int) -> some View {
        if track.hasIcon, track.icon != nil {
            AsyncImage(url: model.iconURL(index: index, track: trackIndex)) { phase in
                switch phase {
                case .success(let image): image.resizable().interpolation(.none).scaledToFit()
                case .failure: Image(systemName: "exclamationmark.triangle").foregroundColor(.red)
                default: ProgressView().controlSize(.small)
                }
            }
            .frame(width: 38, height: 38)
            .accessibilityLabel(model.text("Local icon preview", "本地图标预览"))
        } else {
            Image(systemName: "square.dashed").foregroundColor(.secondary).frame(width: 38, height: 38)
                .accessibilityLabel(model.text("No local icon", "没有本地图标"))
        }
    }

    private func issueText(_ value: String) -> some View {
        Text(value).font(noteFont).foregroundColor(coral)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var coverIssue: String {
        switch album.coverStatus {
        case "ambiguous": return model.text("Several possible covers; choose one. New cards use a placeholder.",
                                            "找到多个可能的封面，请选择；新卡会先使用临时封面。")
        case "selected_missing": return model.text("The selected cover is no longer available.",
                                                   "之前选择的封面已不可用。")
        default: return model.text("No cover found. New cards use a colorful placeholder.",
                                   "没有找到封面；新卡会使用彩色临时封面。")
        }
    }

    private func audioIssue(_ issue: String) -> String {
        switch issue {
        case "too_many": return model.text("More than 100 tracks; split this card before uploading.",
                                          "超过 100 首，请拆分后再上传。")
        case "untagged_in_mixed_folder":
            return model.text("Some audio cannot be grouped safely. Add album tags or separate the files.",
                              "部分音频无法安全归组，请添加专辑标签或分开文件。")
        default: return model.text("No audio found; add audio and rescan.", "没有找到音频，请添加后重新扫描。")
        }
    }

    private func trackIssue(_ track: YotolioTrack) -> String {
        if track.hasIcon { return model.text("Icon found locally", "已找到本地图标") }
        switch track.issue {
        case "ambiguous": return model.text("More than one matching icon", "找到多个可能的图标")
        case "selected_missing": return model.text("Selected icon is no longer available", "之前选择的图标已不可用")
        default: return model.text("No matching icon found", "没有找到匹配图标")
        }
    }
}

private struct GuideView: View {
    @ObservedObject var model: YotolioModel

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            VStack(alignment: .leading, spacing: 8) {
                Text(model.text("SETUP GUIDE", "配置指南"))
                    .font(.system(size: 13, weight: .bold, design: .rounded)).foregroundColor(coral)
                Text(model.text("From folder to first listen.", "从一个文件夹，到第一次收听。"))
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                Text(model.text("No commands or AI services are required. Follow the four steps; upload progress is kept separately for each Yoto account.",
                                "无需命令行或 AI 服务。按以下四步配置，上传进度会按 Yoto 账号分别保存。"))
                    .foregroundColor(.secondary)
            }
            Panel {
                guideSection(model.text("Before you begin", "开始之前"),
                             model.text("Install Python 3.11 or newer using the official graphical installer. Keep the app beside the project files. The app is locally signed, not notarized. On Windows, use Start Yoto.bat.",
                                        "使用官方图形安装程序安装 Python 3.11 或更新版本。将应用与项目文件放在一起。应用为本地签名，未经公证。Windows 用户请使用 Start Yoto.bat。"))
                Button(model.text("Get Python", "下载 Python")) { model.open("https://www.python.org/downloads/") }
                guideSection(model.text("1. Find your files", "1. 查找素材"),
                             model.text("Choose a folder and select Continue to scan. The scan checks local files only and never moves them.",
                                        "选择文件夹并点击“继续”扫描。扫描只检查本地素材，不会移动原文件。"))
                guideSection(model.text("2. Connect Yoto", "2. 连接 Yoto"),
                             model.text("Open the screenshot guide in Connect Yoto. Create a Public Client, register the callback and select user:content:manage. Enter your client ID, connect, then sign in on Yoto. Never share your password or client secret.",
                                        "在“连接 Yoto”中打开截图教程。创建 Public Client，注册回调地址并勾选 user:content:manage。填写客户端 ID、连接后在 Yoto 登录。不要分享密码或 Client Secret。"))
                guideSection(model.text("3. Review and upload", "3. 检查与上传"),
                             model.text("Compare local and Yoto counts, choose cards and confirm upload here. Existing cards are skipped unless you choose Replace. A missing cover receives a generated placeholder; unmatched icons are omitted. After pausing or a failure, check Yoto again, select the remaining cards and confirm another upload.",
                                        "比较本地与 Yoto 的素材数量，在此选择并确认上传。已有卡默认跳过，除非你选择覆盖；缺封面会生成临时封面，未匹配图标不会上传。暂停或失败后，请重新检查 Yoto，选择剩余卡片并确认上传。"))
                guideSection(model.text("4. Review upload results", "4. 查看上传结果"),
                             model.text("After a successful upload, the overview checks the latest batch on Yoto and shows local previews, missing assets and source-match evidence. To add materials later, rescan and choose whether to replace the card. Linking a physical MYO card is a separate Yoto step.",
                                        "上传成功后，结果页会检查最近一批 Yoto 卡片，显示本地预览、缺失素材和源文件匹配证据。之后补充素材时，请重新扫描并选择是否覆盖；实体卡需在 Yoto 单独绑定。"))
                guideSection(model.text("Troubleshooting and privacy", "问题排查与隐私"),
                             model.text("For a callback error, use the same address and port in Yotolio and the Dashboard, then reconnect. For icon permission errors, approve user:icons:manage and request it when reconnecting. Sign in to the same Yoto account to resume. If an unfinished earlier creation is missing, inspect Yoto before confirming Re-create this card. An uncertain earlier creation could still appear later; never delete progress to bypass the warning. Credentials remain in the private .yoto-myo folder.",
                                        "回调出错时，请确保 Yotolio 与开发者网站的地址和端口一致，然后重新连接。图标权限出错时，请先批准 user:icons:manage，并在重连时勾选该权限。登录同一个 Yoto 账号即可恢复。若上次创建未完成且卡片缺失，请先检查 Yoto，再确认重新创建。如果上次创建结果不确定，卡片之后仍可能出现；不要删除进度绕过提示。凭据保存在私有 .yoto-myo 文件夹中。"))
            }
        }
    }

    private func guideSection(_ title: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(sectionFont)
            Text(detail).font(detailFont).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 4)
    }
}
