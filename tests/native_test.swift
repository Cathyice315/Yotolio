import AppKit
import Foundation

private final class StubProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var statusData = Data()
    private static var statusFails = false
    private static var jobFails = false
    private static var requests: [URLRequest] = []
    private static var jobCounter = 0
    private static var lastJobID = ""
    private static var nextJobStatus = "started"
    private static var holdStatus = false
    private static var heldStatus: (StubProtocol, Data)?

    static func setStatus(_ data: Data) {
        lock.lock()
        statusData = data
        lock.unlock()
    }

    static func failStatus(_ fail: Bool) {
        lock.lock()
        statusFails = fail
        lock.unlock()
    }

    static func failJob(_ fail: Bool) {
        lock.lock()
        jobFails = fail
        lock.unlock()
    }

    static func captured() -> [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    static func startedJobID() -> String {
        lock.lock()
        defer { lock.unlock() }
        return lastJobID
    }

    static func setNextJobStatus(_ value: String) {
        lock.lock()
        nextJobStatus = value
        lock.unlock()
    }

    static func holdNextStatus() {
        lock.lock()
        holdStatus = true
        lock.unlock()
    }

    static func hasHeldStatus() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return heldStatus != nil
    }

    static func releaseHeldStatus() {
        lock.lock()
        let held = heldStatus
        heldStatus = nil
        lock.unlock()
        guard let (request, data) = held else { fatalError("No held status") }
        request.respond(data, code: 200)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var capturedRequest = request
        if capturedRequest.httpBody == nil, let stream = capturedRequest.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 4096)
            var body = Data()
            while true {
                let count = stream.read(&bytes, maxLength: bytes.count)
                if count < 0 { fatalError("Could not read the mocked request body") }
                if count == 0 { break }
                body.append(contentsOf: bytes[..<count])
            }
            capturedRequest.httpBody = body
        }
        let operation: String
        if let body = capturedRequest.httpBody,
           let value = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
            operation = value["operation"] as? String ?? ""
        } else {
            operation = ""
        }
        Self.lock.lock()
        Self.requests.append(capturedRequest)
        let data = Self.statusData
        let failStatus = Self.statusFails
        let failJob = Self.jobFails
        if request.url?.path == "/api/jobs" && !failJob {
            Self.jobCounter += 1
            Self.lastJobID = "job-\(Self.jobCounter)"
            var value = try! JSONSerialization.jsonObject(with: Self.statusData) as! [String: Any]
            switch Self.nextJobStatus {
            case "missing":
                value["job"] = NSNull()
            case "replacement":
                value["job"] = job(state: "running", operation: "upload", id: "other-window-job")
            default:
                value["job"] = job(state: "running", operation: operation, id: Self.lastJobID)
            }
            Self.statusData = try! JSONSerialization.data(withJSONObject: value)
            Self.nextJobStatus = "started"
        }
        let jobID = Self.lastJobID
        let hold = request.url?.path == "/api/status" && Self.holdStatus && !failStatus
        if hold {
            Self.holdStatus = false
            Self.heldStatus = (self, data)
        }
        Self.lock.unlock()

        guard let url = request.url else { return }
        if hold { return }
        if url.path == "/api/status" && failStatus {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            return
        }
        let code = url.path == "/api/jobs" && failJob ? 400 : 200
        let body: Data
        switch url.path {
        case "/api/bootstrap": body = Data(#"{"csrf":"synthetic-csrf"}"#.utf8)
        case "/api/status": body = data
        case "/api/jobs" where failJob: body = Data(#"{"error":"Synthetic task failure"}"#.utf8)
        case "/api/jobs":
            body = try! JSONSerialization.data(withJSONObject: [
                "job": job(state: "running", operation: operation, id: jobID),
            ])
        default: body = Data(#"{"ok":true}"#.utf8)
        }
        respond(body, code: code)
    }

    private func respond(_ body: Data, code: Int) {
        guard let url = request.url else { return }
        let response = HTTPURLResponse(url: url, statusCode: code, httpVersion: nil,
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private enum CoverReply {
    case networkFailure
    case httpFailure(Int)
    case image(Data)
    case invalidImage
}

private final class CoverProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var replies: [CoverReply] = []
    private static var requests: [URL] = []

    static func arrange(_ values: [CoverReply]) {
        lock.lock()
        replies = values
        requests = []
        lock.unlock()
    }

    static func captured() -> [URL] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url else { fatalError("Cover request has no URL") }
        Self.lock.lock()
        Self.requests.append(url)
        guard !Self.replies.isEmpty else {
            Self.lock.unlock()
            fatalError("Unexpected cover request")
        }
        let reply = Self.replies.removeFirst()
        Self.lock.unlock()
        if case .networkFailure = reply {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            return
        }
        let code: Int
        let data: Data
        switch reply {
        case .httpFailure(let status):
            code = status
            data = Data("Unavailable".utf8)
        case .image(let image):
            code = 200
            data = image
        case .invalidImage:
            code = 200
            data = Data("Not an image".utf8)
        case .networkFailure:
            return
        }
        let response = HTTPURLResponse(url: url, statusCode: code, httpVersion: nil,
                                       headerFields: ["Content-Type": "image/png"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}

private func waitUntil(_ message: String, _ condition: () -> Bool) {
    let deadline = Date().addingTimeInterval(5)
    while !condition() && Date() < deadline {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
    expect(condition(), message)
}

private func album(_ title: String, cover: Bool = false) -> [String: Any] {
    [
        "title": title, "tracks": 1, "icons": 0, "cover": cover ? "/synthetic/cover.png" : NSNull(),
        "coverStatus": cover ? "provided" : "missing", "coverCandidates": [],
        "unmatchedIcons": ["/synthetic/unmatched.png"], "source": title, "audioIssue": NSNull(),
        "trackDetails": [["name": "Song.mp3", "source": "/synthetic/Song.mp3",
                          "hasIcon": false, "issue": "missing", "candidates": []]],
    ]
}

private func job(state: String, operation: String = "scan", id: String = "scan-123",
                 completed: [String] = [], error: String? = nil, login: String? = nil) -> [String: Any] {
    [
        "id": id, "operation": operation, "state": state, "logs": [],
        "error": error as Any? ?? NSNull(), "loginUrl": login as Any? ?? NSNull(),
        "phase": operation, "completed": completed,
        "skipped": [], "currentAlbum": "", "audioDone": 0, "audioTotal": 0, "total": 2,
    ]
}

private func status(_ titles: [String], workspace: String = "default", root: String = "/synthetic/source",
                    currentJob: [String: Any]? = nil, connected: Bool = true,
                    lastUpload: [String: Any]? = nil, inspection: [String: Any]? = nil,
                    scanRevision: Int = 0, review: [String: Any]? = nil,
                    includeReview: Bool = true, migration: [String: Any]? = nil) -> Data {
    let value: [String: Any] = [
        "config": ["workspace": workspace, "root": root, "client_id": "SYNTHETIC_CLIENT",
                   "oauth_port": 8788, "icons_scope": false],
        "connection": ["saved": connected, "usable": connected, "expiresAt": NSNull(), "scopes": []],
        "albums": titles.enumerated().map { album($0.element, cover: $0.offset == 0) },
        "job": currentJob as Any? ?? NSNull(), "assetStatusError": NSNull(),
        "lastUpload": lastUpload as Any? ?? NSNull(), "inspection": inspection as Any? ?? NSNull(),
        "reviewInspection": includeReview ? (review ?? reviewReport(titles, scanRevision: scanRevision)) : NSNull(),
        "accountMigration": migration as Any? ?? NSNull(),
        "scanRevision": scanRevision,
    ]
    return try! JSONSerialization.data(withJSONObject: value)
}

private func reviewResult(_ title: String, presence: String = "missing",
                          pendingCreate: Bool = false, pendingOverwrite: Bool = false) -> [String: Any] {
    [
        "title": title, "presence": presence, "cover": "unverified",
        "remoteTracks": presence == "present" ? 1 : NSNull(),
        "remoteIcons": presence == "present" ? 0 : NSNull(),
        "remoteCovers": presence == "present" ? 1 : NSNull(),
        "pendingCreate": pendingCreate, "pendingOverwrite": pendingOverwrite,
        "tracks": [], "error": NSNull(),
    ]
}

private func reviewReport(_ titles: [String], scanRevision: Int = 0,
                          results: [[String: Any]]? = nil, id: String = "review-1") -> [String: Any] {
    [
        "id": id, "operation": "inspect", "state": "completed", "requested": titles,
        "completed": titles, "skipped": [], "results": results ?? titles.map { reviewResult($0) },
        "error": NSNull(), "sourceUploadId": NSNull(), "scanRevision": scanRevision,
    ]
}

private func report(id: String, operation: String, state: String, requested: [String],
                    completed: [String] = [], skipped: [[String: String]] = [],
                    sourceUploadID: String? = nil, error: String? = nil) -> [String: Any] {
    [
        "id": id, "operation": operation, "state": state, "requested": requested,
        "completed": completed, "skipped": skipped, "results": [],
        "sourceUploadId": sourceUploadID as Any? ?? NSNull(), "error": error as Any? ?? NSNull(),
    ]
}

private func waitForNewJob(after previousID: String, model: YotolioModel) -> String {
    waitUntil("Task did not start") {
        StubProtocol.startedJobID() != previousID && !model.pending && model.busy
    }
    return StubProtocol.startedJobID()
}

private func applyJob(_ model: YotolioModel, titles: [String], id: String, state: String,
                      operation: String = "scan", error: String? = nil, login: String? = nil) {
    StubProtocol.setStatus(status(titles, currentJob: job(state: state, operation: operation, id: id,
                                                         error: error, login: login)))
    model.refresh()
    waitUntil("Task status was not applied") {
        model.status?.job?.id == id && model.status?.job?.state == state
            && model.status?.job?.loginUrl == login
    }
}

@main
private struct NativeTests {
    static func main() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubProtocol.self]
        let session = URLSession(configuration: configuration)
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)

        StubProtocol.setStatus(status(["Alpha", "Beta"]))
        model.refresh()
        waitUntil("Initial status did not load") { model.status?.albums.count == 2 }
        expect(model.step == 3, "A connected collection should open in review")
        expect(model.selected == Set(["Alpha", "Beta"]), "Usable cards should initially be selected")
        expect(model.coverURL(index: 0)?.query?.contains("index=0") == true, "Cover URL must use the album index")

        model.selected.remove("Beta")
        expect(model.selectedAlbums.map(\.title) == ["Alpha"], "Upload metrics must exclude unselected cards")
        expect(model.selectedAlbums.reduce(0) { $0 + $1.tracks } == 1, "Upload audio counts must follow the selection")
        model.draft.root = "/synthetic/unsaved-edit"
        StubProtocol.setStatus(status([], currentJob: job(state: "running")))
        model.refresh()
        waitUntil("Scan did not start") { model.status?.job?.state == "running" }
        expect(model.selected == Set(["Alpha"]), "An in-progress scan must not erase the selection")
        expect(model.draft.root == "/synthetic/unsaved-edit", "Polling must not overwrite edits")

        StubProtocol.setStatus(status(["Partial"], currentJob: job(state: "failed", error: "Synthetic scan failure")))
        model.refresh()
        waitUntil("Scan failure was not displayed") { model.status?.job?.state == "failed" }
        expect(model.selected == Set(["Alpha"]), "A failed scan must not select partial results")
        expect(!model.canNavigate(to: 2), "A failed scan cannot authorize review or upload")

        StubProtocol.setStatus(status(["Alpha", "Beta", "Gamma"], currentJob: job(state: "completed")))
        model.refresh()
        waitUntil("Scan did not complete") { model.status?.albums.count == 3 }
        expect(model.selected == Set(["Alpha", "Gamma"]), "Rescans must retain choices and select new cards")
        expect(model.coverURL(index: 0)?.query?.contains("v=0") == true, "Cover URL must use the scan revision")
        model.draft.root = "/synthetic/source"
        model.selected.removeAll()
        expect(!model.canContinue, "Review must require at least one selected card")
        model.navigate(to: 4)
        expect(model.step == 3, "The sidebar must not bypass an empty selection")
        model.selected = Set(["Alpha", "Gamma"])
        expect(model.canUpload && !model.canContinue, "A usable selection uploads directly from review")
        model.continueToNextStep()
        expect(model.step == 3, "A usable selection must not open a separate upload step")

        var switching = job(state: "running", operation: "auth", id: "switch-account")
        switching["phase"] = "account-scan"
        StubProtocol.setStatus(status([], workspace: "account-b", currentJob: switching))
        model.refresh()
        waitUntil("Account scan did not start") { model.status?.job?.id == "switch-account" }
        expect(model.selected == Set(["Alpha", "Gamma"]), "Account rescans must retain the user's selection")
        switching["state"] = "completed"
        switching["phase"] = "connected"
        StubProtocol.setStatus(status(["Alpha", "Beta", "Gamma"], workspace: "account-b", currentJob: switching))
        model.refresh()
        waitUntil("Account scan did not finish") { model.status?.job?.state == "completed" }
        expect(model.selected == Set(["Alpha", "Gamma"]), "The same collection keeps selection across accounts")
        StubProtocol.setStatus(status(["Alpha", "Beta", "Gamma"]))
        model.refresh()
        waitUntil("Original account did not return") { model.status?.config.workspace == "default" }

        model.draft.root = "/synthetic/unsaved-edit"
        StubProtocol.setStatus(status(["Delta"], workspace: "other", root: "/synthetic/other"))
        model.refresh()
        waitUntil("Workspace did not change") { model.status?.config.workspace == "other" }
        expect(model.selected == Set(["Delta"]), "Selection must not leak between collections")
        expect(model.draft.root == "/synthetic/unsaved-edit", "External refresh must preserve an in-progress edit")

        StubProtocol.setStatus(status(["Alpha", "Beta", "Gamma"]))
        model.refresh()
        waitUntil("Collection did not return") { model.status?.config.workspace == "default" }
        model.selected.remove("Beta")
        model.draft = model.status!.config
        model.portText = "9000"
        let previousJob = StubProtocol.startedJobID()
        model.runJob("upload", titles: ["Alpha", "Gamma"], uploadConfirmed: true)
        let uploadID = waitForNewJob(after: previousJob, model: model)
        let requests = StubProtocol.captured()
        let configRequest = requests.last { $0.url?.path == "/api/config" }!
        let jobRequest = requests.last { $0.url?.path == "/api/jobs" }!
        expect(configRequest.httpBody != nil && jobRequest.httpBody != nil, "POST bodies must be available to the test")
        let configBody = try! JSONSerialization.jsonObject(with: configRequest.httpBody!) as! [String: Any]
        let jobBody = try! JSONSerialization.jsonObject(with: jobRequest.httpBody!) as! [String: Any]
        expect(configRequest.value(forHTTPHeaderField: "X-Yoto-CSRF") == "synthetic-csrf",
               "Writes must carry the session token")
        expect(configBody["oauth_port"] as? Int == 9000, "The edited callback port must be saved")
        expect(jobBody["operation"] as? String == "upload", "The creation batch must reach the backend")
        expect(jobBody["albums"] as? [String] == ["Alpha", "Gamma"], "A selected batch must retain every title")
        expect(jobBody["reviewId"] as? String == "review-1" && jobBody["confirmed"] as? Bool == true,
               "Uploads must carry the current review and explicit confirmation")

        applyJob(model, titles: ["Alpha", "Beta", "Gamma"], id: uploadID,
                 state: "failed", operation: "upload")
        StubProtocol.failJob(true)
        model.runJob("scan")
        waitUntil("Task failure was not displayed") { model.errorMessage == "Synthetic task failure" }
        StubProtocol.failJob(false)

        StubProtocol.failStatus(true)
        model.refresh()
        waitUntil("Offline status was not displayed") { model.offline }
        StubProtocol.failStatus(false)
        model.refresh()
        waitUntil("Service did not recover") { !model.offline }
        expect(model.errorMessage.isEmpty, "A recovered connection must not keep the stale offline error")

        model.portText = "1"
        model.runJob("scan")
        expect(!model.errorMessage.isEmpty, "Invalid callback ports must be rejected before starting a task")

        let running = try! JSONDecoder().decode(YotolioJob.self,
                                               from: JSONSerialization.data(withJSONObject: job(state: "running", completed: ["A", "B"])))
        let completed = try! JSONDecoder().decode(YotolioJob.self,
                                                 from: JSONSerialization.data(withJSONObject: job(state: "completed", completed: ["A", "B"])))
        expect(running.progress < 1, "A running task must not claim completion")
        expect(completed.progress == 1, "A completed task should show its actual completed fraction")

        testAutomaticScanning(session)
        testAutomaticLogin(session)
        testAccountConfirmation(session)
        testLostAndDelayedTasks(session)
        testCoverURLs(session)
        testCoverPreviews()
        testLanguagePreferenceMigration()
        testLocalizedLoginFailures(session)
        testAutomaticInspection(session)
        testRecreationConfirmation(session)
        testReviewChoicesAndReadOnlyResults(session)
        testReviewUploadAndResume(session)
        session.invalidateAndCancel()
        print("Native model tests passed")
    }

    private static func testCoverURLs(_ session: URLSession) {
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        StubProtocol.setStatus(status(["Alpha"], currentJob: job(state: "completed", id: "cover-scan-1"),
                                      scanRevision: 4))
        model.refresh()
        waitUntil("Cover scan status did not load") { model.status?.job?.id == "cover-scan-1" }
        let first = model.coverURL(index: 0)
        expect(first?.query?.contains("v=4") == true, "Cover URLs must include the current scan revision")

        StubProtocol.setStatus(status(["Alpha"], currentJob: job(state: "completed", operation: "upload",
                                                                id: "cover-upload"), scanRevision: 4))
        model.refresh()
        waitUntil("Cover upload status did not load") { model.status?.job?.id == "cover-upload" }
        expect(model.coverURL(index: 0) == first, "A new upload job must not reload unchanged covers")

        StubProtocol.setStatus(status(["Alpha"], currentJob: job(state: "completed", id: "cover-scan-2"),
                                      scanRevision: 5))
        model.refresh()
        waitUntil("New cover scan status did not load") { model.status?.scanRevision == 5 }
        expect(model.coverURL(index: 0) != first, "A new scan must refresh the cover URL")
    }

    private static func testCoverPreviews() {
        let portrait = fittedCoverSize(NSSize(width: 638, height: 1011), maxWidth: 96)
        expect(portrait.width == 96 && portrait.height > 96 && portrait.height < 192,
               "Portrait covers must retain their whole natural aspect ratio")
        expect(abs(portrait.width / portrait.height - 638.0 / 1011.0) < 0.0001,
               "Portrait cover proportions must not change")
        let landscape = fittedCoverSize(NSSize(width: 200, height: 100), maxWidth: 96)
        expect(landscape == NSSize(width: 96, height: 48), "Landscape covers must not be cropped")
        let extreme = fittedCoverSize(NSSize(width: 10, height: 1000), maxWidth: 96)
        expect(extreme.height == 192 && extreme.width < 2, "Very tall covers must fit within the height limit")

        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 8, pixelsHigh: 12,
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                     isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        let imageData = bitmap.representation(using: .png, properties: [:])!
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CoverProtocol.self]
        let session = URLSession(configuration: configuration)
        let url = URL(string: "http://127.0.0.1:12345/api/cover?index=0&v=1")!
        let nextURL = URL(string: "http://127.0.0.1:12345/api/cover?index=0&v=2")!
        let loader = CoverPreviewLoader(session: session, retryDelays: [0.01, 0.01])

        CoverProtocol.arrange([.networkFailure, .image(imageData)])
        loader.load(url)
        waitUntil("A temporary cover failure was not retried") {
            if case .loaded = loader.state { return true }
            return false
        }
        expect(CoverProtocol.captured() == [url, url], "A temporary failure should retry the same cover")

        CoverProtocol.arrange([.httpFailure(404), .httpFailure(404), .httpFailure(404), .image(imageData)])
        loader.load(url)
        waitUntil("A persistent cover error was not reported") {
            if case .failed(.http(404)) = loader.state { return true }
            return false
        }
        expect(CoverProtocol.captured().count == 3, "Automatic cover retries must be bounded")
        loader.load(url)
        waitUntil("A manual cover retry did not recover") {
            if case .loaded = loader.state { return true }
            return false
        }
        expect(CoverProtocol.captured().count == 4, "A manual retry must start a fresh request")

        let noRetries = CoverPreviewLoader(session: session, retryDelays: [])
        CoverProtocol.arrange([.invalidImage])
        noRetries.load(url)
        waitUntil("Invalid cover data was not reported") {
            if case .failed(.invalidImage) = noRetries.state { return true }
            return false
        }

        let changing = CoverPreviewLoader(session: session, retryDelays: [0.2])
        CoverProtocol.arrange([.httpFailure(503), .image(imageData)])
        changing.load(url)
        waitUntil("The original cover request did not start") { CoverProtocol.captured().count == 1 }
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        changing.load(nextURL)
        waitUntil("The new cover did not load") {
            if case .loaded = changing.state { return true }
            return false
        }
        RunLoop.main.run(until: Date().addingTimeInterval(0.25))
        expect(CoverProtocol.captured() == [url, nextURL], "A new scan must cancel obsolete retries")
        session.invalidateAndCancel()
    }

    private static func testRecreationConfirmation(_ session: URLSession) {
        var failed = job(state: "failed", operation: "upload", id: "failed-upload")
        failed["recovery"] = ["type": "missing_prior_card", "title": "Alpha", "priorStatus": "verified"]
        StubProtocol.setStatus(status(["Alpha", "Beta"], currentJob: failed))
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        model.refresh()
        waitUntil("Recovery status did not load") { model.status?.job?.recovery?.title == "Alpha" }
        let initialCount = StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count

        model.navigate(to: 1)
        expect(!model.canContinue, "A pending recovery must prevent a new scan")
        model.runJob("scan")
        model.runJob("inspect", titles: ["Alpha"])
        model.navigate(to: 4)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == initialCount,
               "Pending recovery must not start a scan or an automatic inspection")

        model.runJob("recreate", titles: ["Alpha"], recoveryJobID: "failed-upload")
        expect(!model.pending && !model.errorMessage.isEmpty, "Recovery requires explicit confirmation")
        model.runJob("recreate", titles: ["Alpha"], recoveryJobID: "stale-upload", recoveryConfirmed: true)
        expect(!model.pending && !model.errorMessage.isEmpty, "Recovery must bind to the failed task")
        model.runJob("recreate", titles: ["Beta"], recoveryJobID: "failed-upload", recoveryConfirmed: true)
        expect(!model.pending && !model.errorMessage.isEmpty, "Recovery must bind to the failed card")
        model.runJob("recreate", titles: ["Alpha", "Beta"], recoveryJobID: "failed-upload", recoveryConfirmed: true)
        expect(!model.pending && !model.errorMessage.isEmpty, "Recovery must handle only one card")
        model.runJob("recreate", recoveryJobID: "failed-upload", recoveryConfirmed: true)
        expect(!model.pending && !model.errorMessage.isEmpty, "Recovery requires a card")
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == initialCount,
               "Rejected confirmations must not start a task")

        let previousID = StubProtocol.startedJobID()
        model.selected.remove("Alpha")
        expect(model.canRecover("Alpha"), "Recovery must not depend on a cleared review selection")
        model.runJob("recreate", titles: ["Alpha"], recoveryJobID: "failed-upload", recoveryConfirmed: true)
        let recreationID = waitForNewJob(after: previousID, model: model)
        let request = StubProtocol.captured().last { $0.url?.path == "/api/jobs" }!
        let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
        expect(body["operation"] as? String == "recreate", "Confirmed recovery must use the dedicated operation")
        expect(body["album"] as? String == "Alpha" && body["albums"] == nil,
               "Confirmed recovery must carry exactly one card")
        expect(body["confirmed"] as? Bool == true && body["recoveryJobId"] as? String == "failed-upload",
               "Confirmed recovery must bind the user's confirmation to the failed task")
        let recreation = report(id: recreationID, operation: "recreate", state: "completed", requested: ["Alpha"])
        StubProtocol.setStatus(status(["Alpha", "Beta"],
                                      currentJob: job(state: "completed", operation: "recreate", id: recreationID),
                                      lastUpload: recreation, includeReview: false))
        model.refresh()
        waitUntil("Recreation completion did not load") { model.status?.job?.state == "completed" }
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        expect(model.step != 4, "A one-card recreation must not pretend the original batch completed")
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == initialCount + 1,
               "Recreation must not automatically replace its task status with a review")
    }

    private static func testAutomaticInspection(_ session: URLSession) {
        let titles = ["Alpha", "Beta", "Outside batch"]
        let batch = report(id: "batch-1", operation: "upload", state: "completed",
                           requested: ["Alpha", "Beta", "Missing source"], completed: ["Alpha"],
                           skipped: [["title": "Beta", "reason": "already_existing"]])
        let stale = report(id: "old-check", operation: "inspect", state: "completed",
                           requested: ["Alpha", "Beta"], sourceUploadID: "different-batch")
        StubProtocol.setStatus(status(titles, lastUpload: batch, inspection: stale, scanRevision: 1))
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        model.refresh()
        waitUntil("Batch did not load") { model.status?.lastUpload?.id == "batch-1" }
        expect(model.currentInspection == nil, "An inspection from a different batch cannot be reused")
        expect(model.overviewTitles == ["Alpha", "Beta", "Missing source"],
               "The overview must retain all requested cards, even when a source is missing")

        func inspectionRequests() -> [URLRequest] {
            StubProtocol.captured().filter {
                guard $0.url?.path == "/api/jobs", let data = $0.httpBody,
                      let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
                return body["operation"] as? String == "inspect"
            }
        }

        let before = inspectionRequests().count
        StubProtocol.failJob(true)
        model.navigate(to: 4)
        waitUntil("Inspection startup failure was not displayed") { model.errorMessage == "Synthetic task failure" }
        model.refresh()
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        expect(inspectionRequests().count == before + 1, "A failed automatic check must not retry on polling")
        StubProtocol.failJob(false)

        var previousID = StubProtocol.startedJobID()
        model.inspectOverviewIfNeeded(force: true)
        var id = waitForNewJob(after: previousID, model: model)
        let request = inspectionRequests().last!
        let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
        expect(body["albums"] as? [String] == ["Alpha", "Beta"],
               "Inspection must cover available cards from the batch, not the whole collection")

        func publish(_ state: String, id: String) {
            let check = report(id: id, operation: "inspect", state: state,
                               requested: ["Alpha", "Beta"], sourceUploadID: "batch-1",
                               error: state == "failed" ? "Synthetic check failure" : nil)
            StubProtocol.setStatus(status(titles, currentJob: job(state: state, operation: "inspect", id: id),
                                          lastUpload: batch, inspection: check, scanRevision: 1))
            model.refresh()
            waitUntil("Inspection status was not applied") {
                model.status?.job?.id == id && model.currentInspection?.state == state
            }
        }

        publish("running", id: id)
        publish("failed", id: id)
        model.refresh()
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        expect(inspectionRequests().count == before + 2, "A failed online check must wait for an explicit retry")
        previousID = StubProtocol.startedJobID()
        model.inspectOverviewIfNeeded(force: true)
        id = waitForNewJob(after: previousID, model: model)
        publish("completed", id: id)
        model.navigate(to: 3)
        model.navigate(to: 4)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        expect(inspectionRequests().count == before + 3, "Returning to results must reuse the current check")

        StubProtocol.setStatus(status(titles, currentJob: job(state: "completed", id: "new-scan"),
                                      lastUpload: batch, scanRevision: 2))
        model.refresh()
        _ = waitForNewJob(after: id, model: model)
        expect(inspectionRequests().count == before + 4, "A new scan must trigger a new batch check")
    }

    private static func testAutomaticScanning(_ session: URLSession) {
        StubProtocol.setStatus(status(["Old"], currentJob: job(state: "completed", id: "old-scan")))
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        model.refresh()
        waitUntil("Existing scan did not load") { model.step == 3 }
        model.navigate(to: 1)

        var previousID = StubProtocol.startedJobID()
        model.continueToNextStep()
        var id = waitForNewJob(after: previousID, model: model)
        expect(model.step == 1, "A stale completed scan must not advance a new scan")
        expect(!model.canNavigate(to: 2), "Review cannot bypass a scan awaiting its first status")
        applyJob(model, titles: [], id: id, state: "running")
        expect(model.step == 1, "Review must wait for the scan to finish")
        applyJob(model, titles: ["Alpha", "Beta"], id: id, state: "completed")
        expect(model.step == 3, "A successful scan with a connected account should open review")
        expect(model.selected == Set(["Alpha", "Beta"]), "Newly found cards should be selected")

        model.selected.removeAll()
        model.continueToNextStep()
        model.navigate(to: 4)
        expect(model.step == 3 && !model.canContinue, "Zero selected cards cannot continue or skip ahead")
        model.selected.insert("Alpha")
        model.continueToNextStep()
        expect(model.step == 3 && model.canUpload, "A selected new card uploads from review")
        model.draft.root = "/synthetic/unsaved-folder"
        model.back()
        expect(model.step == 1, "Back should find an available earlier step when review is stale")
        model.draft.root = "/synthetic/source"

        previousID = StubProtocol.startedJobID()
        model.continueToNextStep()
        id = waitForNewJob(after: previousID, model: model)
        applyJob(model, titles: ["Partial"], id: id, state: "failed", error: "Synthetic scan failure")
        expect(model.step == 1 && !model.canNavigate(to: 2), "A failed scan cannot open review")
        expect(!model.selected.contains("Partial"), "Partial failed-scan results cannot be selected")

        previousID = StubProtocol.startedJobID()
        model.continueToNextStep()
        id = waitForNewJob(after: previousID, model: model)
        applyJob(model, titles: ["Partial"], id: id, state: "stopped")
        expect(model.step == 1 && !model.canNavigate(to: 2), "A cancelled scan cannot open review")

        previousID = StubProtocol.startedJobID()
        model.continueToNextStep()
        id = waitForNewJob(after: previousID, model: model)
        applyJob(model, titles: [], id: id, state: "completed")
        expect(model.step == 1 && !model.errorMessage.isEmpty, "An empty scan must explain why it cannot continue")

        previousID = StubProtocol.startedJobID()
        model.continueToNextStep()
        id = waitForNewJob(after: previousID, model: model)
        applyJob(model, titles: ["Alpha"], id: id, state: "completed")
        expect(model.step == 3 && model.selectedTitles == ["Alpha"], "A retry can complete normally")
    }

    private static func testAutomaticLogin(_ session: URLSession) {
        var opened: [String] = []
        StubProtocol.setStatus(status(["Alpha"]))
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session,
                                 openURL: { opened.append($0.absoluteString); return true })
        model.refresh()
        waitUntil("Login test status did not load") { model.step == 3 }
        expect(model.canUpload && model.canVerify, "Current saved connection should allow upload and verification")
        model.draft.client_id = "OTHER_CLIENT"
        expect(!model.canUpload && !model.canVerify, "An edited client ID must require reconnection")
        model.draft.client_id = "SYNTHETIC_CLIENT"
        model.navigate(to: 2)

        var previousID = StubProtocol.startedJobID()
        model.runJob("auth")
        var id = waitForNewJob(after: previousID, model: model)
        applyJob(model, titles: ["Alpha"], id: id, state: "running", operation: "auth")
        expect(opened.isEmpty, "Login cannot open before the callback server is ready")
        let login = "http://127.0.0.1:8788/start"
        applyJob(model, titles: ["Alpha"], id: id, state: "running", operation: "auth", login: login)
        expect(opened == [login], "Ready login should open exactly once")
        model.refresh()
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        expect(opened == [login], "Polling must not reopen the login page")
        applyJob(model, titles: ["Alpha"], id: id, state: "completed", operation: "auth")

        previousID = StubProtocol.startedJobID()
        model.runJob("auth")
        id = waitForNewJob(after: previousID, model: model)
        applyJob(model, titles: ["Alpha"], id: id, state: "stopping", operation: "auth", login: login)
        expect(opened == [login], "Cancelling a connection must not open login")
        applyJob(model, titles: ["Alpha"], id: id, state: "stopped", operation: "auth", login: login)

        previousID = StubProtocol.startedJobID()
        model.runJob("auth")
        id = waitForNewJob(after: previousID, model: model)
        applyJob(model, titles: ["Alpha"], id: id, state: "running", operation: "auth",
                 login: "https://example.invalid/start")
        expect(opened == [login] && !model.errorMessage.isEmpty, "Unsafe login URLs must be rejected")
    }

    private static func testAccountConfirmation(_ session: URLSession) {
        StubProtocol.setStatus(status(["Alpha"], connected: false))
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        model.refresh()
        waitUntil("Disconnected collection did not load") { model.step == 2 }

        let previousID = StubProtocol.startedJobID()
        model.runJob("auth")
        let id = waitForNewJob(after: previousID, model: model)
        var pendingJob = job(state: "running", operation: "auth", id: id)
        pendingJob["phase"] = "account-confirmation"
        let migration: [String: Any] = ["id": "confirmation-1", "name": "default", "remaining": 1]
        StubProtocol.setStatus(status(["Alpha"], currentJob: pendingJob, connected: false, migration: migration))
        model.refresh()
        waitUntil("Running account confirmation did not appear") { model.accountConfirmationPending && model.busy }
        let confirmations = StubProtocol.captured().filter { $0.url?.path == "/api/account-migration" }.count
        model.resolveAccount("different")
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/account-migration" }.count == confirmations,
               "Account choices must wait until the login worker finishes")
        pendingJob["state"] = "completed"
        StubProtocol.setStatus(status(["Alpha"], currentJob: pendingJob, connected: false, migration: migration))
        model.refresh()
        waitUntil("Account confirmation did not appear") { model.accountConfirmationPending && !model.busy }
        expect(model.step == 2 && !model.canContinue && !model.canUpload,
               "Unresolved account history must block continuing and uploads")
        expect(model.canNavigate(to: 1) && model.canNavigate(to: 2) && !model.canNavigate(to: 3),
               "Previous steps remain inspectable without bypassing confirmation")
        model.back()
        expect(model.step == 1 && model.accountConfirmationPending,
               "Back must work without dismissing the confirmation")
        let count = StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count
        model.continueToNextStep()
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == count,
               "Continuing from the previous step cannot start a scan before confirmation")

        model.resolveAccount("different")
        waitUntil("Account confirmation was not sent") {
            !model.pending && StubProtocol.captured().contains { $0.url?.path == "/api/account-migration" }
        }
        let request = StubProtocol.captured().last { $0.url?.path == "/api/account-migration" }!
        let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: String]
        expect(body == ["id": "confirmation-1", "choice": "different"],
               "The explicit choice must bind to the current question")

        var scanning = job(state: "running", operation: "auth", id: id)
        scanning["phase"] = "account-scan"
        StubProtocol.setStatus(status([], workspace: "separate", currentJob: scanning, connected: true))
        model.refresh()
        waitUntil("Account scan did not begin") { model.status?.job?.phase == "account-scan" }
        expect(model.step != 3 && !model.canUpload, "Review waits for the account-specific scan")
        scanning["state"] = "completed"
        scanning["phase"] = "connected"
        StubProtocol.setStatus(status(["Alpha"], workspace: "separate", currentJob: scanning, connected: true))
        model.refresh()
        waitUntil("Connected account did not open review") { model.step == 3 && model.hasCurrentConnection }
        expect(model.canUpload, "Review becomes usable only after connection and scan finish")
    }

    private static func testLostAndDelayedTasks(_ session: URLSession) {
        StubProtocol.setStatus(status(["Alpha"]))
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        model.refresh()
        waitUntil("Task test status did not load") { model.step == 3 }
        StubProtocol.setNextJobStatus("missing")
        let before = StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count
        model.runJob("upload", titles: ["Alpha"], uploadConfirmed: true)
        waitUntil("A missing task was not reported") { model.taskStatusUnknown && !model.pending }
        expect(!model.busy, "A missing task cannot keep the interface permanently busy")
        expect(!model.canUpload && !model.canContinue, "An unknown upload outcome blocks another upload")
        model.runJob("upload", titles: ["Alpha"], uploadConfirmed: true)
        model.runJob("inspect", titles: ["Alpha"])
        model.refresh()
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == before + 1,
               "An upload with an unknown result must never be retried automatically")

        StubProtocol.setStatus(status(["Alpha"], connected: false))
        let replaced = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        replaced.refresh()
        waitUntil("Replacement test status did not load") { replaced.step == 2 }
        StubProtocol.setNextJobStatus("replacement")
        replaced.runJob("auth")
        waitUntil("A replaced task was not reported") {
            replaced.taskStatusUnknown && replaced.status?.job?.id == "other-window-job"
        }
        expect(replaced.busy, "A different active task must still keep mutations disabled")
        StubProtocol.setStatus(status(["Alpha"], currentJob: job(state: "completed", operation: "upload",
                                                                  id: "other-window-job"), connected: false))
        replaced.refresh()
        waitUntil("Replacement task did not finish") { !replaced.busy }
        expect(replaced.taskStatusUnknown, "The unknown outcome warning persists after another task finishes")

        StubProtocol.setStatus(status(["Alpha"], connected: false))
        let delayed = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        delayed.refresh()
        waitUntil("Delayed test status did not load") { delayed.step == 2 }
        StubProtocol.holdNextStatus()
        delayed.refresh()
        waitUntil("Old status request was not held") { StubProtocol.hasHeldStatus() }
        let previousID = StubProtocol.startedJobID()
        delayed.runJob("auth")
        let id = waitForNewJob(after: previousID, model: delayed)
        StubProtocol.holdNextStatus()
        StubProtocol.releaseHeldStatus()
        waitUntil("Fresh status request was not held") { StubProtocol.hasHeldStatus() }
        expect(delayed.busy && !delayed.taskStatusUnknown,
               "A status requested before task startup cannot mark the new task as lost")
        StubProtocol.releaseHeldStatus()
        waitUntil("Fresh task status was not applied") { delayed.status?.job?.id == id }
        expect(delayed.busy && !delayed.taskStatusUnknown, "The running task must be tracked normally")
    }

    private static func testLanguagePreferenceMigration() {
        let earlierSettings = [
            "local.yotomyo.yotolio.reviewandupload": "zh",
            "local.yotomyo.yotolio.reviewfix": "en",
            "local.yotomyo.yotolio.loginfix": "en",
            "local.yotomyo.yotolio.update": "en",
        ]
        let latest = YotolioModel.previousLanguage { earlierSettings[$0] }
        expect(latest == "zh", "A new app must inherit the most recently delivered app's language")
        expect(YotolioModel.initialLanguage(saved: "en", previous: latest) == "en",
               "A new app's explicit choice overrides the previous app")
        expect(YotolioModel.previousLanguage { $0 == "local.yotomyo.yotolio.loginfix" ? "zh" : nil } == "zh",
               "Older app settings remain available when the latest app has none")
        expect(YotolioModel.initialLanguage(saved: nil, previous: "zh") == "zh",
               "A new app should inherit the previous app's Chinese setting")
        expect(YotolioModel.initialLanguage(saved: "en", previous: "zh") == "en",
               "The current app's setting takes precedence")
        expect(YotolioModel.initialLanguage(saved: "zh", previous: "en") == "zh",
               "The current app retains its Chinese setting")
        expect(YotolioModel.initialLanguage(saved: nil, previous: nil) == "en",
               "English remains the default without a saved setting")
    }

    private static func testLocalizedLoginFailures(_ session: URLSession) {
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        let failures = [
            ("ACCOUNT_IDENTITY_REJECTED", "rejected this login", "拒绝用这次登录"),
            ("ACCOUNT_IDENTITY_SERVICE_UNAVAILABLE", "verification service", "账号验证服务"),
            ("ACCOUNT_IDENTITY_KEYS_UNAVAILABLE", "verification keys", "账号验证密钥"),
            ("ACCOUNT_IDENTITY_VERIFIER_UNAVAILABLE", "OpenSSL", "OpenSSL"),
            ("ACCOUNT_IDENTITY_TOKEN_INVALID", "security verification", "未通过安全验证"),
            ("ACCOUNT_AUTH_RESPONSE_INVALID", "unreadable", "无法识别"),
            ("ACCOUNT_AUTH_TOKEN_INVALID", "usable login token", "登录凭据无效"),
            ("ACCOUNT_AUTH_EXPIRY_INVALID", "expiry", "有效期"),
            ("ACCOUNT_AUTH_SCOPES_INSUFFICIENT", "required permissions", "所需权限"),
            ("ACCOUNT_AUTH_CONTENT_VIEW_DENIED", "check your cards", "查看卡片"),
            ("ACCOUNT_AUTH_ICON_ACCESS_DENIED", "custom icons", "自定义图标"),
            ("ACCOUNT_AUTH_SCOPE_CHECK_UNAVAILABLE", "service or connection", "网络或 Yoto 服务"),
        ]
        for (code, english, chinese) in failures {
            var value = job(state: "failed", operation: "auth", error: "Synthetic technical detail")
            value["errorCode"] = code
            let failed = try! JSONDecoder().decode(YotolioJob.self, from: JSONSerialization.data(withJSONObject: value))
            model.language = "en"
            expect(model.jobError(failed).contains(english), "Login failure must explain the problem in English")
            model.language = "zh"
            expect(model.jobError(failed).contains(chinese), "Login failure must explain the problem in Chinese")
            expect(!model.jobError(failed).contains("Synthetic technical detail"), "Known errors must not expose raw details")
            expect(!failed.showsPhase, "A failed login cannot continue to say it is waiting")
        }
        let running = try! JSONDecoder().decode(YotolioJob.self,
                                               from: JSONSerialization.data(withJSONObject: job(state: "running", operation: "auth")))
        let stopped = try! JSONDecoder().decode(YotolioJob.self,
                                               from: JSONSerialization.data(withJSONObject: job(state: "stopped", operation: "auth")))
        let completed = try! JSONDecoder().decode(YotolioJob.self,
                                                 from: JSONSerialization.data(withJSONObject: job(state: "completed", operation: "auth")))
        expect(running.showsPhase && !stopped.showsPhase && completed.showsPhase,
               "Only active or successful login attempts show their phase")
    }

    private static func testReviewChoicesAndReadOnlyResults(_ session: URLSession) {
        let titles = ["Existing", "Interrupted", "Pending", "New", "Ambiguous"]
        let review = reviewReport(titles, results: [
            reviewResult("Existing", presence: "present"),
            reviewResult("Interrupted", presence: "present", pendingCreate: true),
            reviewResult("Pending", presence: "present", pendingOverwrite: true),
            reviewResult("New"),
            reviewResult("Ambiguous", presence: "duplicate"),
        ])
        StubProtocol.setStatus(status(titles, review: review))
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        model.refresh()
        waitUntil("Review did not load") { model.currentReview?.id == "review-1" }
        expect(model.actionableTitles == ["New"], "Existing and ambiguous cards must not be changed by default")
        expect(model.selected == Set(["New", "Ambiguous"]), "Existing cards default to Skip and are unselected")
        model.setExistingChoice("Existing", replace: true)
        expect(model.selected.contains("Existing") && model.overwrites.contains("Existing"),
               "Choosing Replace must select the existing card")
        model.setExistingChoice("Existing", replace: false)
        expect(!model.selected.contains("Existing") && !model.overwrites.contains("Existing"),
               "Choosing Skip must deselect the existing card")
        model.setSelected("Existing", selected: true)
        expect(model.overwrites.contains("Existing"), "Selecting an existing card must choose its displayed action")
        model.setSelected("Existing", selected: false)
        expect(!model.overwrites.contains("Existing"), "Deselecting an existing card restores Skip")
        model.setSelected("New", selected: false)
        model.selectAllNewCards()
        expect(model.selected.contains("New") && !model.selected.contains("Existing")
               && model.overwrites.isEmpty, "Select all new must not authorize replacement")
        let before = StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count
        model.runJob("upload", titles: ["New"])
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == before,
               "An unconfirmed upload must not start")
        model.setExistingChoice("Existing", replace: true)
        var refreshedReview = review
        refreshedReview["id"] = "review-2"
        StubProtocol.setStatus(status(titles, review: refreshedReview))
        model.refresh()
        waitUntil("Fresh review did not load") { model.currentReview?.id == "review-2" }
        expect(!model.selected.contains("Existing") && model.overwrites.isEmpty,
               "A new review must clear replacement authorization")
        for title in ["Existing", "Interrupted", "Pending"] {
            model.setExistingChoice(title, replace: true)
        }
        expect(model.actionableTitles == ["Existing", "Interrupted", "Pending", "New"],
               "Only explicitly chosen existing cards may join the batch")
        let previousID = StubProtocol.startedJobID()
        model.runJob("upload", titles: model.actionableTitles, uploadConfirmed: true)
        _ = waitForNewJob(after: previousID, model: model)
        let request = StubProtocol.captured().last { $0.url?.path == "/api/jobs" }!
        let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
        expect(body["reviewId"] as? String == "review-2", "Upload must bind to the reviewed snapshot")
        expect(body["overwriteTitles"] as? [String] == ["Existing", "Interrupted", "Pending"],
               "The explicit existing-card choices must be sent")
        expect(body["skippedTitles"] as? [String] == ["Ambiguous"], "Skipped cards must reach the results")
        expect(body["confirmed"] as? Bool == true, "Upload requires explicit confirmation")

        var failed = report(id: "failed", operation: "upload", state: "failed", requested: ["Pending"],
                            error: "Synthetic technical detail")
        model.language = "zh"
        for code in ["OVERWRITE_UNCONFIRMED", "OVERWRITE_CHANGED"] {
            failed["errorCode"] = code
            let decoded = try! JSONDecoder().decode(YotolioReport.self, from: JSONSerialization.data(withJSONObject: failed))
            expect(model.reportError(decoded).contains("没有再次发送覆盖"), "The result error must be localized")
        }

        let existingTitles = ["Existing", "Skipped"]
        let readReview = reviewReport(existingTitles, results: [
            reviewResult("Existing", presence: "present"),
            reviewResult("Skipped", presence: "duplicate"),
        ], id: "read-only-review")
        let oldBatch = report(id: "old-batch", operation: "upload", state: "completed", requested: ["Old card"])
        StubProtocol.setStatus(status(existingTitles, lastUpload: oldBatch, review: readReview))
        let readOnly = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        readOnly.refresh()
        waitUntil("Read-only review did not load") { readOnly.currentReview?.id == "read-only-review" }
        readOnly.selected.removeAll()
        expect(!readOnly.canContinue, "Zero selected cards cannot open results")
        readOnly.setSelected("Skipped", selected: true)
        let jobsBefore = StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count
        readOnly.continueToNextStep()
        expect(readOnly.step == 4 && readOnly.viewingReadOnlyResults, "All-skipped review opens read-only results")
        expect(readOnly.overviewUpload == nil && readOnly.overviewTitles == ["Skipped"],
               "Read-only results cannot show a previous upload")
        expect(readOnly.currentInspection?.id == "read-only-review", "Read-only results reuse the current review")
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == jobsBefore,
               "Viewing skipped cards must not start an upload or another inspection")

        StubProtocol.setStatus(status(existingTitles, connected: false, lastUpload: oldBatch, includeReview: false))
        readOnly.refresh()
        waitUntil("Disconnect did not apply") { readOnly.status?.connection.usable == false }
        expect(readOnly.currentInspection == nil && readOnly.overviewUpload == nil,
               "A stale review must not expose the old batch")
        readOnly.recheckOverview()
        expect(readOnly.step == 4, "A stale review cannot start a recheck without a connection")
        readOnly.back()
        expect(readOnly.step == 2 && !readOnly.viewingReadOnlyResults,
               "Back must return to the available connection step after disconnect")

        StubProtocol.setStatus(status(existingTitles, review: readReview))
        let staleScan = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        staleScan.refresh()
        waitUntil("Second read-only review did not load") { staleScan.currentReview?.id == "read-only-review" }
        staleScan.continueToNextStep()
        staleScan.draft.root = "/synthetic/changed"
        staleScan.recheckOverview()
        expect(staleScan.step == 4, "A changed folder cannot recheck the stale review")
        staleScan.back()
        expect(staleScan.step == 1, "Back must return to the folder step when the scan is stale")
    }

    private static func testReviewUploadAndResume(_ session: URLSession) {
        let titles = ["Alpha", "Beta", "Ambiguous"]
        let review = reviewReport(titles, results: [
            reviewResult("Alpha"), reviewResult("Beta"), reviewResult("Ambiguous", presence: "duplicate"),
        ])
        StubProtocol.setStatus(status(titles, review: review))
        let model = YotolioModel(serviceURL: URL(string: "http://127.0.0.1:12345")!, session: session)
        model.refresh()
        waitUntil("Upload review did not load") { model.currentReview?.id == "review-1" }
        expect(model.step == 3 && model.canUpload && !model.canContinue,
               "A valid batch must upload directly from review")
        expect(model.actionableTitles == ["Alpha", "Beta"], "Ambiguous selections cannot enter the batch")

        let before = StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count
        model.runJob("upload", titles: model.actionableTitles)
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == before,
               "Uploading from review still requires confirmation")
        var previousID = StubProtocol.startedJobID()
        model.runJob("upload", titles: model.actionableTitles, uploadConfirmed: true)
        var uploadID = waitForNewJob(after: previousID, model: model)
        let body = try! JSONSerialization.jsonObject(with: StubProtocol.captured().last {
            $0.url?.path == "/api/jobs"
        }!.httpBody!) as! [String: Any]
        expect(body["albums"] as? [String] == ["Alpha", "Beta"]
               && body["skippedTitles"] as? [String] == ["Ambiguous"],
               "The approved batch and excluded selection must be submitted separately")

        var upload = report(id: uploadID, operation: "upload", state: "running",
                            requested: ["Alpha", "Beta"])
        upload["reviewSkipped"] = ["Ambiguous"]
        StubProtocol.setStatus(status(titles, currentJob: job(state: "running", operation: "upload", id: uploadID),
                                      lastUpload: upload, includeReview: false))
        model.refresh()
        waitUntil("Submitted batch did not load") { model.presentedUpload?.id == uploadID }
        expect(model.presentedUpload?.requested == ["Alpha", "Beta"]
               && model.presentedUpload?.reviewSkipped == ["Ambiguous"],
               "The review page must retain the actual submitted and excluded titles")
        expect(model.presentedUpload?.unfinishedCount == 2,
               "An active batch must count uncompleted submitted cards without including excluded selections")
        expect(!model.canUpload, "A running task cannot be resubmitted")

        upload["state"] = "stopped"
        upload["completed"] = ["Alpha"]
        StubProtocol.setStatus(status(titles, currentJob: job(state: "stopped", operation: "upload", id: uploadID,
                                                              completed: ["Alpha"]),
                                      lastUpload: upload, includeReview: false))
        model.refresh()
        waitUntil("Paused batch did not load") { model.status?.job?.state == "stopped" }
        expect(model.presentedUpload?.unfinishedCount == 1,
               "A paused batch must count its unfinished submitted cards")
        let mixed = report(id: "mixed", operation: "upload", state: "failed",
                           requested: ["Alpha", "Beta", "Gamma"], completed: ["Alpha"],
                           skipped: [["title": "Beta", "reason": "already_existing"]])
        let mixedReport = try! JSONDecoder().decode(YotolioReport.self, from: JSONSerialization.data(withJSONObject: mixed))
        expect(mixedReport.unfinishedCount == 1, "Skipped cards must not be counted as unfinished")
        model.navigate(to: 4)
        model.navigate(to: 3)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        expect(model.step == 3 && !model.canUpload, "A paused batch stays in review without re-uploading")
        expect(StubProtocol.captured().filter { $0.url?.path == "/api/jobs" }.count == before + 1,
               "Viewing a paused batch must not automatically replace its status with a check")

        previousID = StubProtocol.startedJobID()
        model.inspectReviewIfNeeded(force: true)
        let inspectionID = waitForNewJob(after: previousID, model: model)
        let inspectionBody = try! JSONSerialization.jsonObject(with: StubProtocol.captured().last {
            $0.url?.path == "/api/jobs"
        }!.httpBody!) as! [String: Any]
        expect(inspectionBody["purpose"] as? String == "review",
               "Resuming requires an explicitly requested review")
        let refreshedReview = reviewReport(titles, results: [
            reviewResult("Alpha", presence: "present"), reviewResult("Beta"),
            reviewResult("Ambiguous", presence: "duplicate"),
        ], id: inspectionID)
        StubProtocol.setStatus(status(titles, currentJob: job(state: "completed", operation: "inspect", id: inspectionID),
                                      lastUpload: upload, review: refreshedReview))
        model.refresh()
        waitUntil("Explicit recheck did not complete") { model.currentReview?.id == inspectionID }
        expect(!model.selected.contains("Alpha") && model.actionableTitles == ["Beta"],
               "The completed card is not silently selected for replacement on resume")

        previousID = StubProtocol.startedJobID()
        model.runJob("upload", titles: model.actionableTitles, uploadConfirmed: true)
        uploadID = waitForNewJob(after: previousID, model: model)
        var resumed = report(id: uploadID, operation: "upload", state: "completed",
                             requested: ["Beta"], completed: ["Beta"])
        resumed["reviewSkipped"] = ["Ambiguous"]
        StubProtocol.setStatus(status(titles, currentJob: job(state: "completed", operation: "upload", id: uploadID,
                                                              completed: ["Beta"]),
                                      lastUpload: resumed, includeReview: false))
        model.refresh()
        waitUntil("A successful batch did not open results") { model.step == 4 }
        waitUntil("A successful batch did not start its online check") {
            StubProtocol.captured().contains { request in
                guard request.url?.path == "/api/jobs", let data = request.httpBody,
                      let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
                return value["operation"] as? String == "inspect"
                    && value["purpose"] == nil
                    && value["albums"] as? [String] == ["Beta", "Ambiguous"]
            }
        }
    }
}
