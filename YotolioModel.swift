import AppKit
import Combine
import Foundation
import UniformTypeIdentifiers

struct YotolioConfig: Codable {
    var workspace = "default"
    var root = ""
    var client_id = ""
    var oauth_port = 8788
    var icons_scope = false
}

struct YotolioConnection: Decodable {
    let saved: Bool
    let usable: Bool
    let expiresAt: Double?
    let scopes: [String]
    let error: String?
}

struct YotolioAccountMigration: Decodable {
    let id: String
    let name: String
    let remaining: Int
}

struct YotolioTrack: Decodable {
    let name: String
    let source: String
    let hasIcon: Bool
    let issue: String?
    let icon: String?
    let candidates: [String]
}

struct YotolioAlbum: Decodable, Identifiable {
    let title: String
    let tracks: Int
    let icons: Int
    let cover: String?
    let coverStatus: String
    let coverCandidates: [String]
    let unmatchedIcons: [String]
    let source: String
    let audioIssue: String?
    let trackDetails: [YotolioTrack]
    let hasEvidence: Bool?
    let uploadedPlaceholder: Bool?
    let pendingCoverUpdate: Bool?
    let coverNeedsUpload: Bool?
    let iconsNeedUpload: Int?

    var id: String { title }
}

struct YotolioSkipped: Decodable {
    let title: String
    let reason: String
}

struct YotolioResultTrack: Decodable {
    let name: String
    let remoteTitle: String?
    let audio: String
    let icon: String
}

struct YotolioCardResult: Decodable {
    let title: String
    let presence: String
    let cover: String
    let remoteTracks: Int?
    let remoteIcons: Int?
    let remoteCovers: Int?
    let pendingCreate: Bool?
    let pendingOverwrite: Bool?
    let tracks: [YotolioResultTrack]
    let error: String?
}

struct YotolioReport: Decodable {
    let id: String
    let operation: String
    let state: String
    let requested: [String]
    let completed: [String]
    let skipped: [YotolioSkipped]
    let reviewSkipped: [String]?
    let error: String?
    let errorCode: String?
    let results: [YotolioCardResult]?
    let sourceUploadId: String?
    let scanRevision: Int?

    var unfinishedCount: Int {
        let finished = Set(completed).union(skipped.map(\.title))
        return requested.filter { !finished.contains($0) }.count
    }
}

struct YotolioRecovery: Decodable {
    let type: String
    let title: String
    let priorStatus: String
}

struct YotolioJob: Decodable {
    let id: String
    let operation: String
    let state: String
    let logs: [String]
    let error: String?
    let errorCode: String?
    let recovery: YotolioRecovery?
    let loginUrl: String?
    let phase: String
    let completed: [String]
    let skipped: [YotolioSkipped]
    let currentAlbum: String
    let audioDone: Int
    let audioTotal: Int
    let total: Int

    var active: Bool { state == "running" || state == "stopping" }
    var showsPhase: Bool { operation != "auth" || active || state == "completed" }
    var progress: Double {
        guard total > 0 else { return 0 }
        let fraction = max(0, min(1, Double(completed.count + skipped.count) / Double(total)))
        return state == "completed" ? fraction : min(0.99, fraction)
    }
}

struct YotolioStatus: Decodable {
    let config: YotolioConfig
    let connection: YotolioConnection
    let job: YotolioJob?
    let albums: [YotolioAlbum]
    let assetStatusError: String?
    let lastUpload: YotolioReport?
    let inspection: YotolioReport?
    let reviewInspection: YotolioReport?
    let scanRevision: Int?
    let accountMigration: YotolioAccountMigration?
}

private struct Bootstrap: Decodable { let csrf: String }
private struct EmptyResponse: Decodable {}
private struct AssetResponse: Decodable { let chosen: Bool }
private struct JobStartResponse: Decodable { let job: YotolioJob }
private struct ServerError: Decodable { let error: String?; let code: String? }

private struct RequestError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

enum CoverPreviewFailure: Equatable {
    case transport(String)
    case http(Int)
    case invalidResponse
    case invalidImage
}

enum CoverPreviewState {
    case loading
    case loaded(NSImage)
    case failed(CoverPreviewFailure)
}

func fittedCoverSize(_ source: NSSize, maxWidth: CGFloat) -> NSSize {
    guard source.width.isFinite, source.height.isFinite, maxWidth.isFinite,
          source.width > 0, source.height > 0, maxWidth > 0 else { return .zero }
    let scale = min(maxWidth / source.width, 2 * maxWidth / source.height)
    return NSSize(width: source.width * scale, height: source.height * scale)
}

final class CoverPreviewLoader: ObservableObject {
    @Published private(set) var state: CoverPreviewState = .loading

    private let session: URLSession
    private let retryDelays: [TimeInterval]
    private var task: URLSessionDataTask?
    private var pendingRetry: DispatchWorkItem?
    private var generation = 0

    init(session: URLSession = .shared, retryDelays: [TimeInterval] = [0.4, 1.0]) {
        self.session = session
        self.retryDelays = retryDelays
    }

    func load(_ url: URL) {
        cancel()
        state = .loading
        request(url, attempt: 0, generation: generation)
    }

    func cancel() {
        generation += 1
        task?.cancel()
        task = nil
        pendingRetry?.cancel()
        pendingRetry = nil
    }

    private func request(_ url: URL, attempt: Int, generation: Int) {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5)
        request.httpMethod = "GET"
        task = session.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self, self.generation == generation else { return }
                self.task = nil
                if let error {
                    self.retryOrFail(.transport(error.localizedDescription), url: url,
                                     attempt: attempt, generation: generation)
                    return
                }
                guard let response = response as? HTTPURLResponse else {
                    self.retryOrFail(.invalidResponse, url: url, attempt: attempt, generation: generation)
                    return
                }
                guard (200...299).contains(response.statusCode) else {
                    self.retryOrFail(.http(response.statusCode), url: url, attempt: attempt, generation: generation)
                    return
                }
                guard let data, let image = NSImage(data: data), image.isValid,
                      image.size.width.isFinite, image.size.height.isFinite,
                      image.size.width > 0, image.size.height > 0 else {
                    self.retryOrFail(.invalidImage, url: url, attempt: attempt, generation: generation)
                    return
                }
                self.state = .loaded(image)
            }
        }
        task?.resume()
    }

    private func retryOrFail(_ failure: CoverPreviewFailure, url: URL, attempt: Int, generation: Int) {
        guard attempt < retryDelays.count else {
            state = .failed(failure)
            return
        }
        let retry = DispatchWorkItem { [weak self] in
            guard let self, self.generation == generation else { return }
            self.pendingRetry = nil
            self.request(url, attempt: attempt + 1, generation: generation)
        }
        pendingRetry = retry
        DispatchQueue.main.asyncAfter(deadline: .now() + retryDelays[attempt], execute: retry)
    }

    deinit {
        task?.cancel()
        pendingRetry?.cancel()
    }
}

final class YotolioModel: ObservableObject {
    @Published var language: String {
        didSet { UserDefaults.standard.set(language, forKey: "yotolio-language") }
    }
    @Published private(set) var step = 1
    @Published var showingGuide = false
    @Published var draft = YotolioConfig()
    @Published var portText = "8788"
    @Published var status: YotolioStatus?
    @Published var selected = Set<String>()
    @Published var overwrites = Set<String>()
    @Published var pending = false
    @Published var offline = false
    @Published var errorMessage = ""
    @Published var notice = ""
    @Published var taskStatusUnknown = false

    private let serviceURL: URL
    private let session: URLSession
    private let openURL: (URL) -> Bool
    private var csrf = ""
    private var timer: Timer?
    private var refreshing = false
    private var refreshAgain = false
    private var statusRequestSequence = 0
    private var initialized = false
    private var offlineError = ""
    private var collection = ""
    private var seen = Set<String>()
    private var awaitingJobID: String?
    private var awaitingOperation: String?
    private var awaitingAfterStatusSequence: Int?
    private var startingOperation: String?
    private var reviewAfterScanID: String?
    private var openLoginAfterStartID: String?
    private var reviewAfterLoginID: String?
    private var resultsAfterUploadID: String?
    private var automaticInspectionKey: String?
    private var automaticReviewKey: String?
    private var readOnlyReviewID: String?
    private var readOnlyTitles: [String]?

    static func initialLanguage(saved: String?, previous: String?) -> String {
        (saved ?? previous) == "zh" ? "zh" : "en"
    }

    static func previousLanguage(_ read: (String) -> String?) -> String? {
        for domain in ["local.yotomyo.yotolio.reviewandupload", "local.yotomyo.yotolio.reviewfix",
                       "local.yotomyo.yotolio.loginfix", "local.yotomyo.yotolio.update"] {
            if let language = read(domain) { return language }
        }
        return nil
    }

    init(serviceURL: URL, session: URLSession = .shared,
         openURL: @escaping (URL) -> Bool = { NSWorkspace.shared.open($0) }) {
        self.serviceURL = serviceURL
        self.session = session
        self.openURL = openURL
        let defaults = UserDefaults.standard
        let previous = Self.previousLanguage {
            defaults.persistentDomain(forName: $0)?["yotolio-language"] as? String
        }
        self.language = Self.initialLanguage(saved: defaults.string(forKey: "yotolio-language"), previous: previous)
    }

    deinit { timer?.invalidate() }

    func text(_ english: String, _ chinese: String) -> String {
        language == "zh" ? chinese : english
    }

    func jobError(_ job: YotolioJob) -> String {
        accountError(job.errorCode, fallback: job.error ?? "")
    }

    func reportError(_ report: YotolioReport) -> String {
        accountError(report.errorCode, fallback: report.error ?? "")
    }

    private func accountError(_ code: String?, fallback: String) -> String {
        switch code {
        case "ACCOUNT_IDENTITY_UNAVAILABLE":
            return text("Could not verify the Yoto account. Your existing login and progress were not changed. Check your connection and try again.",
                        "无法验证 Yoto 账号，原有登录和进度未改变。请检查网络后重试。")
        case "ACCOUNT_IDENTITY_REJECTED":
            return text("Yoto rejected this login for account verification. Sign out of Yoto in your browser and connect again. Your existing login and progress were not changed.",
                        "Yoto 拒绝用这次登录验证账号。请在浏览器中退出 Yoto 后重新连接。原有登录和进度未改变。")
        case "ACCOUNT_IDENTITY_SERVICE_UNAVAILABLE":
            return text("Yoto's account verification service could not be reached or returned an unreadable response. Check your connection and try again. Your existing login and progress were not changed.",
                        "无法连接 Yoto 账号验证服务，或服务返回的信息无法识别。请检查网络后重试。原有登录和进度未改变。")
        case "ACCOUNT_IDENTITY_KEYS_UNAVAILABLE":
            return text("Yoto's account verification keys could not be obtained. Check your connection and try again. Your existing login and progress were not changed.",
                        "无法获取 Yoto 的账号验证密钥。请检查网络后重试。原有登录和进度未改变。")
        case "ACCOUNT_IDENTITY_VERIFIER_UNAVAILABLE":
            return text("The OpenSSL account verification tool could not run. Check your system installation and reopen Yotolio. Your existing login and progress were not changed.",
                        "无法运行 OpenSSL 账号验证工具。请检查系统安装后重新打开 Yotolio。原有登录和进度未改变。")
        case "ACCOUNT_IDENTITY_TOKEN_INVALID":
            return text("The login identity did not pass security verification. Check your computer's date and time, then connect again. Your existing login and progress were not changed.",
                        "登录账号未通过安全验证。请检查电脑日期和时间后重新连接。原有登录和进度未改变。")
        case "ACCOUNT_AUTHORIZATION_UNUSABLE":
            return text("Login could not be saved. Check the client ID, permissions and expiry; existing data was not changed.",
                        "无法保存登录。请检查 Client ID、权限和有效期；原数据未改变。")
        case "ACCOUNT_AUTH_RESPONSE_INVALID":
            return text("Yoto returned an unreadable login response. Try connecting again. If it persists, check your connection and Yoto's service. Your existing login and progress were not changed.",
                        "Yoto 返回的登录信息无法识别。请重新连接；如果持续出现，请检查网络和 Yoto 服务。原有登录和进度未改变。")
        case "ACCOUNT_AUTH_TOKEN_INVALID":
            return text("Yoto did not return a usable login token. Try connecting again; if it persists, check your public client ID. Your existing login and progress were not changed.",
                        "Yoto 返回的登录凭据无效。请重新连接；如果持续出现，请检查 Public Client ID。原有登录和进度未改变。")
        case "ACCOUNT_AUTH_EXPIRY_INVALID":
            return text("The login had no usable expiry, or expired before it could be saved. Try connecting again. Your existing login and progress were not changed.",
                        "登录信息的有效期不可用，或在保存前已过期。请重新连接。原有登录和进度未改变。")
        case "ACCOUNT_AUTH_SCOPES_INSUFFICIENT":
            return text("Yoto did not confirm the required permissions. In the Dashboard, check user:content:manage and user:icons:manage for the application matching your Client ID, then connect again. Your existing login and progress were not changed.",
                        "Yoto 未确认所需权限。请检查开发者网站中与当前 Client ID 对应的应用是否已批准 user:content:manage 和 user:icons:manage，然后重新连接。原有登录和进度未改变。")
        case "ACCOUNT_AUTH_CONTENT_VIEW_DENIED":
            return text("Yoto refused to check your cards with this login. Confirm user:content:view is enabled for the same Client ID, then connect again. Your existing login and progress were not changed.",
                        "Yoto 拒绝使用这次登录查看卡片。请确认当前 Client ID 对应的应用已批准 user:content:view，然后重新连接。原有登录和进度未改变。")
        case "ACCOUNT_AUTH_ICON_ACCESS_DENIED":
            return text("Yoto refused to check your custom icons with this login. Confirm user:icons:manage is enabled for the same Client ID, then connect again. Your existing login and progress were not changed.",
                        "Yoto 拒绝使用这次登录查看自定义图标。请确认当前 Client ID 对应的应用已批准 user:icons:manage，然后重新连接。原有登录和进度未改变。")
        case "ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE":
            return text("Yotolio could not check the required Yoto access because the service or connection was unavailable. Try connecting again. Your existing login and progress were not changed.",
                        "无法验证 Yoto 权限，可能是网络或 Yoto 服务暂时不可用。请重新连接。原有登录和进度未改变。")
        case "ACCOUNT_HISTORY_CHANGED":
            return text("The earlier progress changed during confirmation. Nothing was replaced; connect again.",
                        "确认期间旧进度发生了变化，原记录未被覆盖；请重新连接。")
        case "ACCOUNT_MULTIPLE_HISTORIES":
            return text("Multiple older histories belong to this account and cannot be safely merged automatically. All are preserved.",
                        "此账号有多份旧进度，无法安全地自动合并；所有记录均已保留。")
        case "ACCOUNT_LOGIN_CHANGED":
            return text("The saved login was changed outside Yotolio and cannot be safely replaced. Existing progress was not changed.",
                        "本地登录已在 Yotolio 外更改，无法安全替换；原有进度未改变。")
        case "ACCOUNT_HISTORY_MISSING":
            return text("This account's saved history folder is missing. Restore it before connecting.",
                        "此账号的已保存进度文件夹缺失，请恢复文件夹后再连接。")
        case "ACCOUNT_RECORDS_UNREADABLE":
            return text("Saved account records cannot be read. Restore them before connecting; nothing was changed.",
                        "无法读取已保存的账号记录，请恢复后再连接；原数据未改变。")
        case "ACCOUNT_CHANGED":
            return text("The Yoto account changed in another window. Reopen this window and try again.",
                        "Yoto 账号已在另一个窗口更改。请重新打开此窗口后重试。")
        case "CONFIG_CHANGED":
            return text("Settings changed in another window. Quit the app and reopen it.",
                        "其他窗口已更改设置。请退出应用后重新打开。")
        case "ACCOUNT_CONFIRMATION_REQUIRED":
            return text("Confirm or cancel the earlier saved progress before continuing.",
                        "请先确认或取消以前保存的进度。")
        case "ACCOUNT_CONFIRMATION_EXPIRED":
            return text("That confirmation is no longer current. Review the current question or connect again.",
                        "该确认已过期。请查看当前问题，或重新连接。")
        case "INVALID_REVIEW":
            return text("The account or review changed. Check the cards again before uploading.",
                        "账号或检查结果已变化，请重新检查卡片后再上传。")
        case "RECOVERY_REQUIRED":
            return text("Return to Review & upload and resolve the missing-card confirmation first.",
                        "请先返回“检查与上传”处理缺失卡片确认。")
        case "RECREATE_CONFIRMATION_REQUIRED":
            return text("The card or failed task changed. Check Yoto and Review again before taking another action.",
                        "卡片或失败任务已变化。请先检查 Yoto 并重新检查卡片，再进行其他操作。")
        case "OVERWRITE_UNCONFIRMED":
            return text("The previous replacement is not confirmed and may still take effect. No new replacement was sent. Check the card on Yoto, wait, then check again; keep your saved progress.",
                        "上次覆盖尚未确认，之后仍可能生效。本次没有再次发送覆盖。请在 Yoto 检查这张卡，等待后再核对；请保留已保存的进度。")
        case "OVERWRITE_CHANGED":
            return text("The card changed while the previous replacement was unconfirmed and may still take effect. No new replacement was sent. Check the card on Yoto and keep your saved progress; do not replace it again.",
                        "上次覆盖尚未确认，之后仍可能生效，而 Yoto 中的卡片已发生其他变化。本次没有再次发送覆盖。请在 Yoto 检查卡片并保留已保存的进度，不要再次覆盖。")
        default: return fallback
        }
    }

    var busy: Bool { status?.job?.active == true || awaitingJobID != nil }
    var accountConfirmationPending: Bool { status?.accountMigration != nil }
    var preparingConnection: Bool {
        (startingOperation == "auth" || awaitingOperation == "auth")
            && !(status?.job?.operation == "auth" && status?.job?.active == true)
    }
    var preparingReview: Bool {
        (startingOperation == "inspect" || awaitingOperation == "inspect")
            && !(status?.job?.operation == "inspect" && status?.job?.active == true
                 && status?.job?.id == status?.reviewInspection?.id)
    }
    var unavailable: Bool { pending || busy || offline || status == nil || accountConfirmationPending }
    var albums: [YotolioAlbum] { status?.albums ?? [] }
    var uploadable: [YotolioAlbum] { albums.filter { $0.audioIssue == nil } }
    var selectedAlbums: [YotolioAlbum] { uploadable.filter { selected.contains($0.title) } }
    var selectedTitles: [String] { selectedAlbums.map(\.title) }
    var currentReview: YotolioReport? {
        guard let report = status?.reviewInspection,
              report.scanRevision == status?.scanRevision,
              Set(report.requested) == Set(albums.map(\.title)) else { return nil }
        return report
    }
    var presentedUpload: YotolioReport? {
        guard currentReview == nil, let job = status?.job, let upload = status?.lastUpload,
              job.id == upload.id, ["upload", "existing", "recreate"].contains(job.operation) else { return nil }
        return upload
    }
    var actionableTitles: [String] {
        guard let review = currentReview, review.state == "completed" else { return [] }
        let results = Dictionary(uniqueKeysWithValues: (review.results ?? []).map { ($0.title, $0) })
        return selectedTitles.filter { title in
            guard let result = results[title], result.error == nil else { return false }
            if result.presence == "missing" { return result.pendingOverwrite != true }
            return result.presence == "present" && overwrites.contains(title)
        }
    }
    var selectableNewTitles: [String] {
        guard let review = currentReview, review.state == "completed" else { return [] }
        let newTitles = Set((review.results ?? []).filter {
            $0.presence == "missing" && $0.error == nil && $0.pendingOverwrite != true
        }.map(\.title))
        return uploadable.map(\.title).filter { newTitles.contains($0) }
    }

    func setSelected(_ title: String, selected isSelected: Bool) {
        guard !unavailable, currentReview?.state == "completed",
              uploadable.contains(where: { $0.title == title }) else { return }
        if isSelected {
            selected.insert(title)
            if let result = currentReview?.results?.first(where: { $0.title == title }),
               result.presence == "present", result.error == nil {
                overwrites.insert(title)
            }
        } else {
            selected.remove(title)
            overwrites.remove(title)
        }
    }

    func setExistingChoice(_ title: String, replace: Bool) {
        guard let result = currentReview?.results?.first(where: { $0.title == title }),
              result.presence == "present", result.error == nil else { return }
        setSelected(title, selected: replace)
    }

    func selectAllNewCards() {
        guard !unavailable else { return }
        selected.formUnion(selectableNewTitles)
    }

    private func clearExistingChoices() {
        selected.subtract(overwrites)
        overwrites.removeAll()
    }

    var viewingReadOnlyResults: Bool { readOnlyTitles != nil }
    var overviewUpload: YotolioReport? { viewingReadOnlyResults ? nil : status?.lastUpload }
    var overviewTitles: [String] {
        if let readOnlyTitles { return readOnlyTitles }
        guard let upload = overviewUpload else { return selectedTitles }
        return upload.requested + (upload.reviewSkipped ?? [])
    }
    var inspectableTitles: [String] {
        let available = Set(albums.map(\.title))
        return overviewTitles.filter { available.contains($0) }
    }
    var recoveryNeedsAttention: Bool {
        status?.job?.operation == "upload" && status?.job?.state == "failed"
            && status?.job?.recovery?.type == "missing_prior_card"
    }
    private var waitingAfterUpload: Bool {
        guard let job = status?.job, !job.active else { return false }
        return (["upload", "existing"].contains(job.operation) && ["failed", "stopped"].contains(job.state))
            || job.operation == "recreate"
    }
    var currentInspection: YotolioReport? {
        if viewingReadOnlyResults {
            guard let review = currentReview, review.id == readOnlyReviewID,
                  review.state == "completed", hasCurrentScan, hasCurrentConnection else { return nil }
            return review
        }
        guard let report = status?.inspection, Set(report.requested) == Set(inspectableTitles),
              report.sourceUploadId == overviewUpload?.id else { return nil }
        return report
    }
    private var hasCurrentScan: Bool {
        guard let status, !status.albums.isEmpty else { return false }
        let currentRoot = draft.root.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !currentRoot.isEmpty, draft.workspace == status.config.workspace else { return false }
        let draftURL = URL(fileURLWithPath: (currentRoot as NSString).expandingTildeInPath)
            .standardizedFileURL.resolvingSymlinksInPath()
        let savedURL = URL(fileURLWithPath: status.config.root).standardizedFileURL.resolvingSymlinksInPath()
        let unfinishedScan = ["scan", "prepare"].contains(awaitingOperation ?? "")
            || (["scan", "prepare"].contains(status.job?.operation ?? "") && status.job?.state != "completed")
            || (status.job?.operation == "auth" && status.job?.phase == "account-scan"
                && status.job?.state != "completed")
        return draftURL == savedURL && !unfinishedScan
    }

    var hasCurrentConnection: Bool {
        guard let status else { return false }
        return status.connection.usable && !accountConfirmationPending
            && draft.client_id.trimmingCharacters(in: .whitespacesAndNewlines) == status.config.client_id
            && draft.icons_scope == status.config.icons_scope
    }

    func canNavigate(to number: Int) -> Bool {
        guard (1...4).contains(number) else { return false }
        if accountConfirmationPending { return number == 1 || number == 2 }
        if number == 1 { return true }
        guard hasCurrentScan else { return false }
        if number == 2 { return true }
        guard hasCurrentConnection else { return false }
        if number == 3 { return true }
        return viewingReadOnlyResults || status?.lastUpload != nil
    }

    var canContinue: Bool {
        guard !pending, !offline, status != nil, !accountConfirmationPending else { return false }
        switch step {
        case 1: return !busy && !recoveryNeedsAttention
            && !draft.root.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case 2: return !busy && canNavigate(to: 3)
        case 3: return !busy && !taskStatusUnknown && hasCurrentScan && hasCurrentConnection
            && !selectedTitles.isEmpty && currentReview?.state == "completed" && actionableTitles.isEmpty
        default: return false
        }
    }

    var canUpload: Bool {
        !unavailable && !taskStatusUnknown && hasCurrentScan && hasCurrentConnection && !actionableTitles.isEmpty
    }

    var canResolveRecovery: Bool {
        !unavailable && !taskStatusUnknown && hasCurrentScan && hasCurrentConnection
    }

    func canRecover(_ title: String) -> Bool {
        canResolveRecovery && uploadable.contains { $0.title == title }
    }
    var canVerify: Bool {
        !unavailable && hasCurrentScan && hasCurrentConnection && !uploadable.isEmpty
    }

    func navigate(to number: Int) {
        guard canNavigate(to: number) else { return }
        if number != 4 {
            readOnlyReviewID = nil
            readOnlyTitles = nil
        }
        step = number
        showingGuide = false
        if number == 3 { inspectReviewIfNeeded() }
        if number == 4 { inspectOverviewIfNeeded() }
    }

    func continueToNextStep() {
        guard canContinue else { return }
        if step == 1 {
            runJob("scan", reviewWhenComplete: true)
        } else if step == 3 && actionableTitles.isEmpty, let review = currentReview {
            readOnlyReviewID = review.id
            readOnlyTitles = selectedTitles
            step = 4
        } else {
            navigate(to: step + 1)
        }
    }

    func back() {
        guard step > 1 else { return }
        if step == 4 && viewingReadOnlyResults && canNavigate(to: 3) {
            navigate(to: 3)
            return
        }
        if let destination = (1..<step).reversed().first(where: { canNavigate(to: $0) }) {
            navigate(to: destination)
        }
    }

    func start() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 1.2, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func refresh() {
        if refreshing {
            refreshAgain = true
            return
        }
        refreshing = true
        if csrf.isEmpty {
            request("api/bootstrap") { [weak self] (result: Result<Bootstrap, Error>) in
                guard let self else { return }
                switch result {
                case .success(let value):
                    self.csrf = value.csrf
                    self.readStatus()
                case .failure(let error):
                    self.refreshFailed(error)
                }
            }
        } else {
            readStatus()
        }
    }

    private func readStatus() {
        statusRequestSequence += 1
        let sequence = statusRequestSequence
        request("api/status") { [weak self] (result: Result<YotolioStatus, Error>) in
            guard let self else { return }
            switch result {
            case .success(let value):
                if self.offline && self.errorMessage == self.offlineError {
                    self.errorMessage = ""
                }
                self.offline = false
                self.apply(value, statusSequence: sequence)
                self.finishRefresh()
            case .failure(let error):
                self.refreshFailed(error)
            }
        }
    }

    private func refreshFailed(_ error: Error) {
        offline = true
        errorMessage = error.localizedDescription
        offlineError = errorMessage
        finishRefresh()
    }

    private func finishRefresh() {
        refreshing = false
        if refreshAgain {
            refreshAgain = false
            refresh()
        }
    }

    private func trackStartedJob(_ job: YotolioJob) {
        awaitingJobID = job.id
        awaitingOperation = job.operation
        awaitingAfterStatusSequence = statusRequestSequence
    }

    private func clearAwaitedJob() {
        awaitingJobID = nil
        awaitingOperation = nil
        awaitingAfterStatusSequence = nil
    }

    private func apply(_ value: YotolioStatus, statusSequence: Int) {
        let firstLoad = !initialized
        let previousStatus = status
        if firstLoad {
            draft = value.config
            portText = String(value.config.oauth_port)
            initialized = true
        } else if previousStatus?.config.workspace != value.config.workspace {
            draft.workspace = value.config.workspace
        }
        let identity = value.config.workspace + "\n" + value.config.root
        if identity != collection {
            if previousStatus?.config.root != value.config.root {
                selected.removeAll()
                seen.removeAll()
            }
            collection = identity
        }
        let unfinishedScan = (awaitingJobID != nil && value.job?.id != awaitingJobID
            && ["scan", "prepare"].contains(awaitingOperation ?? ""))
            || (["scan", "prepare"].contains(value.job?.operation ?? "") && value.job?.state != "completed")
            || (value.job?.operation == "auth" && value.job?.phase == "account-scan"
                && value.job?.state != "completed")
        if !unfinishedScan {
            let available = Set(value.albums.filter { $0.audioIssue == nil }.map(\.title))
            selected.formUnion(available.subtracting(seen))
            selected.formIntersection(available)
            seen = available
        }
        status = value
        if previousStatus?.reviewInspection?.id != value.reviewInspection?.id {
            clearExistingChoices()
            if value.reviewInspection == nil { automaticReviewKey = nil }
        }
        if !unfinishedScan, let review = currentReview, review.state == "completed" {
            for result in review.results ?? [] where result.presence == "present"
                && result.error == nil && !overwrites.contains(result.title) {
                selected.remove(result.title)
            }
        }
        if value.accountMigration != nil && previousStatus?.accountMigration == nil {
            step = 2
            showingGuide = false
        }
        if firstLoad && hasCurrentScan { step = hasCurrentConnection ? 3 : 2 }
        if let expectedID = awaitingJobID {
            if value.job?.id == expectedID {
                clearAwaitedJob()
            } else if let after = awaitingAfterStatusSequence, statusSequence > after {
                clearAwaitedJob()
                if reviewAfterScanID == expectedID { reviewAfterScanID = nil }
                if openLoginAfterStartID == expectedID { openLoginAfterStartID = nil }
                if reviewAfterLoginID == expectedID { reviewAfterLoginID = nil }
                if resultsAfterUploadID == expectedID { resultsAfterUploadID = nil }
                taskStatusUnknown = true
            }
        }
        if let job = value.job, job.id == reviewAfterScanID, !job.active {
            reviewAfterScanID = nil
            if job.state == "completed" {
                if value.albums.isEmpty {
                    errorMessage = text("No cards were found. Choose a folder with audio and try again.",
                                        "没有找到卡片。请选择包含音频的文件夹后重试。")
                } else {
                    step = hasCurrentConnection ? 3 : 2
                }
            }
        }
        if let job = value.job, job.id == openLoginAfterStartID {
            if job.state == "running", let login = job.loginUrl {
                openLoginAfterStartID = nil
                open(login, login: true)
            } else if job.state != "running" {
                openLoginAfterStartID = nil
            }
        }
        if let job = value.job, job.id == reviewAfterLoginID, !job.active, value.accountMigration == nil {
            reviewAfterLoginID = nil
            if job.state == "completed" && hasCurrentConnection && hasCurrentScan {
                step = 3
            }
        }
        if let job = value.job, job.id == resultsAfterUploadID, !job.active {
            resultsAfterUploadID = nil
            if job.state == "completed" { step = 4 }
        }
        if step == 3 && !showingGuide {
            DispatchQueue.main.async { [weak self] in self?.inspectReviewIfNeeded() }
        }
        if step == 4 && !showingGuide {
            DispatchQueue.main.async { [weak self] in self?.inspectOverviewIfNeeded() }
        }
    }

    func inspectReviewIfNeeded(force: Bool = false) {
        guard step == 3, !showingGuide, !unavailable, !taskStatusUnknown, hasCurrentScan, hasCurrentConnection,
              !recoveryNeedsAttention, !albums.isEmpty else { return }
        let titles = albums.map(\.title)
        let key = "\(status?.scanRevision ?? 0)\n\(status?.config.workspace ?? "")\n"
            + titles.sorted().joined(separator: "\n")
        if !force && waitingAfterUpload { return }
        if !force && (currentReview != nil || automaticReviewKey == key) { return }
        if force { clearExistingChoices() }
        automaticReviewKey = key
        runJob("inspect", titles: titles, purpose: "review")
    }

    func inspectOverviewIfNeeded(force: Bool = false) {
        guard step == 4, !viewingReadOnlyResults, !showingGuide, !unavailable, !taskStatusUnknown,
              hasCurrentScan, hasCurrentConnection,
              !recoveryNeedsAttention, !inspectableTitles.isEmpty else { return }
        let key = "\(status?.scanRevision ?? 0)\n\(status?.lastUpload?.id ?? "selection")\n"
            + inspectableTitles.sorted().joined(separator: "\n")
        if !force && waitingAfterUpload { return }
        if !force && (currentInspection != nil || automaticInspectionKey == key) { return }
        automaticInspectionKey = key
        runJob("inspect", titles: inspectableTitles)
    }

    func recheckOverview() {
        if viewingReadOnlyResults {
            guard canNavigate(to: 3) else { return }
            navigate(to: 3)
            inspectReviewIfNeeded(force: true)
        } else {
            inspectOverviewIfNeeded(force: true)
        }
    }

    private func request<T: Decodable>(_ path: String, body: [String: Any]? = nil,
                                       completion: @escaping (Result<T, Error>) -> Void) {
        var request = URLRequest(url: serviceURL.appendingPathComponent(path))
        request.timeoutInterval = 20
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue(csrf, forHTTPHeaderField: "X-Yoto-CSRF")
            do {
                request.httpBody = try JSONSerialization.data(withJSONObject: body)
            } catch {
                completion(.failure(error))
                return
            }
        }
        session.dataTask(with: request) { data, response, error in
            let result: Result<T, Error>
            if let error {
                result = .failure(error)
            } else if let response = response as? HTTPURLResponse, let data {
                if (200..<300).contains(response.statusCode) {
                    do {
                        result = .success(try JSONDecoder().decode(T.self, from: data))
                    } catch {
                        result = .failure(RequestError(message: "Invalid response from local service: \(error.localizedDescription)"))
                    }
                } else {
                    let serverError = try? JSONDecoder().decode(ServerError.self, from: data)
                    let message = self.accountError(serverError?.code, fallback: serverError?.error
                        ?? "Local service returned HTTP \(response.statusCode).")
                    result = .failure(RequestError(message: message))
                }
            } else {
                result = .failure(RequestError(message: "No response from the local service."))
            }
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }

    private func post(_ path: String, body: [String: Any] = [:],
                      completion: @escaping (Result<EmptyResponse, Error>) -> Void) {
        request(path, body: body, completion: completion)
    }

    private func configBody() -> [String: Any]? {
        guard let port = Int(portText.trimmingCharacters(in: .whitespacesAndNewlines)),
              (1024...65535).contains(port) else {
            errorMessage = text("Use a callback port from 1024 to 65535.",
                                "回调端口应为 1024 到 65535。")
            return nil
        }
        return ["workspace": draft.workspace, "root": draft.root, "client_id": draft.client_id,
                "oauth_port": port, "icons_scope": draft.icons_scope]
    }

    func runJob(_ operation: String, titles: [String]? = nil, reviewWhenComplete: Bool = false,
                recoveryJobID: String? = nil, recoveryConfirmed: Bool = false, switchAccount: Bool = false,
                purpose: String? = nil, uploadConfirmed: Bool = false) {
        guard !unavailable, let config = configBody() else { return }
        if recoveryNeedsAttention && ["scan", "prepare", "verify", "inspect"].contains(operation) {
            errorMessage = text("Return to Review & upload and resolve the missing-card confirmation first.",
                                "请先返回“检查与上传”处理缺失卡片确认。")
            return
        }
        if let titles, titles.isEmpty {
            errorMessage = text("Select at least one card in Review your cards.", "请先在“检查卡片”中选择至少一张卡片。")
            return
        }
        if operation == "upload" {
            guard uploadConfirmed, let titles, !titles.isEmpty,
                  Set(titles) == Set(actionableTitles), canUpload else {
                errorMessage = text("Check the cards again and select at least one action.",
                                    "请重新检查卡片，并至少选择一项操作。")
                return
            }
        }
        if operation == "inspect" {
            guard let titles, !titles.isEmpty, Set(titles).isSubset(of: Set(albums.map(\.title))),
                  !taskStatusUnknown, hasCurrentScan, hasCurrentConnection, !recoveryNeedsAttention else {
                errorMessage = text("Connect Yoto and resolve any pending upload confirmation before checking.",
                                    "请先连接 Yoto，并处理待确认的上传，然后再检查。")
                return
            }
        }
        if operation == "recreate" {
            guard recoveryConfirmed, let titles, titles.count == 1, let recoveryJobID,
                  let job = status?.job, job.id == recoveryJobID, job.operation == "upload", job.state == "failed",
                  job.recovery?.type == "missing_prior_card", job.recovery?.title == titles[0],
                  canRecover(titles[0]) else {
                errorMessage = text("The card or failed task changed. Check Yoto and Review again before taking another action.",
                                    "卡片或失败任务已变化。请先检查 Yoto 并重新检查卡片，再进行其他操作。")
                return
            }
        }
        startingOperation = operation
        pending = true
        errorMessage = ""
        notice = ""
        post("api/config", body: config) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.startingOperation = nil
                self.pending = false
                self.errorMessage = error.localizedDescription
                self.refresh()
            case .success:
                var body: [String: Any] = ["operation": operation]
                if switchAccount && operation == "auth" { body["switchAccount"] = true }
                if operation == "inspect", purpose == "review" { body["purpose"] = "review" }
                if operation == "upload" {
                    guard let review = self.currentReview, let titles,
                          Set(titles) == Set(self.actionableTitles) else {
                        self.startingOperation = nil
                        self.pending = false
                        self.errorMessage = self.text("The review changed. Check the cards again.",
                                                      "检查结果已变化，请重新检查卡片。")
                        self.refresh()
                        return
                    }
                    body["reviewId"] = review.id
                    body["overwriteTitles"] = titles.filter { self.overwrites.contains($0) }
                    body["skippedTitles"] = self.selectedTitles.filter { !titles.contains($0) }
                    body["confirmed"] = uploadConfirmed
                }
                if operation == "recreate", let titles, let recoveryJobID {
                    body["album"] = titles[0]
                    body["confirmed"] = true
                    body["recoveryJobId"] = recoveryJobID
                } else if let titles {
                    body["albums"] = titles
                }
                self.request("api/jobs", body: body) { [weak self] (result: Result<JobStartResponse, Error>) in
                    guard let self else { return }
                    switch result {
                    case .success(let response):
                        if response.job.id.isEmpty || response.job.operation != operation {
                            self.errorMessage = "Invalid task response from the local service."
                        } else {
                            self.trackStartedJob(response.job)
                            if reviewWhenComplete { self.reviewAfterScanID = response.job.id }
                            if operation == "auth" {
                                self.openLoginAfterStartID = response.job.id
                                self.reviewAfterLoginID = response.job.id
                            }
                            if operation == "upload" { self.resultsAfterUploadID = response.job.id }
                        }
                    case .failure(let error):
                        self.errorMessage = error.localizedDescription
                    }
                    self.startingOperation = nil
                    self.pending = false
                    self.refresh()
                }
            }
        }
    }

    func resolveAccount(_ choice: String) {
        guard !pending, !busy, !offline, let migration = status?.accountMigration,
              ["same", "different", "cancel"].contains(choice) else { return }
        pending = true
        errorMessage = ""
        post("api/account-migration", body: ["id": migration.id, "choice": choice]) { [weak self] result in
            guard let self else { return }
            self.pending = false
            if case .failure(let error) = result { self.errorMessage = error.localizedDescription }
            self.refresh()
        }
    }

    func stopJob() {
        guard !pending, busy else { return }
        pending = true
        errorMessage = ""
        post("api/stop") { [weak self] result in
            guard let self else { return }
            self.pending = false
            if case .failure(let error) = result { self.errorMessage = error.localizedDescription }
            self.refresh()
        }
    }

    func disconnect() {
        guard !unavailable else { return }
        pending = true
        errorMessage = ""
        post("api/disconnect") { [weak self] result in
            guard let self else { return }
            self.pending = false
            if case .failure(let error) = result { self.errorMessage = error.localizedDescription }
            self.refresh()
        }
    }

    func chooseFolder() {
        guard !unavailable else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = text("Choose", "选择")
        if panel.runModal() == .OK, let url = panel.url { draft.root = url.path }
    }

    func chooseAsset(album: YotolioAlbum, kind: String, track: Int? = nil) {
        guard !unavailable, !recoveryNeedsAttention else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = kind == "cover" ? [.png, .jpeg] : [.png, .jpeg, .gif]
        panel.prompt = text("Choose", "选择")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        pending = true
        errorMessage = ""
        let body: [String: Any] = ["album": album.title, "kind": kind, "track": track as Any? ?? NSNull(),
                                   "path": url.path]
        request("api/asset", body: body) { [weak self] (result: Result<AssetResponse, Error>) in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.pending = false
                self.errorMessage = error.localizedDescription
                self.refresh()
            case .success(let value):
                guard value.chosen else {
                    self.pending = false
                    self.refresh()
                    return
                }
                self.request("api/jobs", body: ["operation": "scan"]) { [weak self] (result: Result<JobStartResponse, Error>) in
                    guard let self else { return }
                    switch result {
                    case .success(let response) where !response.job.id.isEmpty && response.job.operation == "scan":
                        self.trackStartedJob(response.job)
                    case .success:
                        self.errorMessage = "Invalid task response from the local service."
                    case .failure(let error):
                        self.errorMessage = error.localizedDescription
                    }
                    self.pending = false
                    self.refresh()
                }
            }
        }
    }

    func coverURL(index: Int) -> URL? {
        var components = URLComponents(url: serviceURL.appendingPathComponent("api/cover"),
                                       resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "index", value: String(index)),
                                  URLQueryItem(name: "v", value: String(status?.scanRevision ?? 0))]
        return components?.url
    }

    func iconURL(index: Int, track: Int) -> URL? {
        var components = URLComponents(url: serviceURL.appendingPathComponent("api/icon"),
                                       resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "index", value: String(index)),
                                  URLQueryItem(name: "track", value: String(track)),
                                  URLQueryItem(name: "v", value: String(status?.scanRevision ?? 0))]
        return components?.url
    }

    func copyCallback() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        if pasteboard.setString("http://127.0.0.1:\(portText)/callback", forType: .string) {
            notice = text("Callback copied.", "回调地址已复制。")
        } else {
            errorMessage = text("Could not copy the callback address.", "无法复制回调地址。")
        }
    }

    func open(_ address: String, login: Bool = false) {
        guard let url = URL(string: address) else {
            errorMessage = text("Invalid link.", "链接无效。")
            return
        }
        let allowed: Bool
        if login {
            allowed = url.scheme == "http" && url.host == "127.0.0.1"
                && url.port == status?.config.oauth_port && url.path == "/start"
                && url.user == nil && url.password == nil && url.query == nil && url.fragment == nil
        } else {
            allowed = ["https://dashboard.yoto.dev/", "https://my.yotoplay.com/cards",
                       "https://www.python.org/downloads/"].contains(address)
        }
        guard allowed, openURL(url) else {
            errorMessage = text("Could not open the link.", "无法打开链接。")
            return
        }
    }
}
