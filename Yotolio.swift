import AppKit
import Foundation
import SwiftUI

private struct LaunchError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private struct RestartRequired: Error {}

private enum StopResult {
    case stopped
    case busy
    case failed(String)
}

enum YotolioBrand {
    static let name = "Yotolio"
    static var icon: NSImage? { NSApp.applicationIconImage }
}

final class YotolioApp: NSObject, NSApplicationDelegate {
    private let root = Bundle.main.bundleURL.deletingLastPathComponent()
    private var window: NSWindow!
    private var model: YotolioModel?
    private var serviceURL: URL?
    private var starting = true

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        installMenus()
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false
        )
        window.title = YotolioBrand.name
        window.minSize = NSSize(width: 800, height: 600)
        window.isReleasedWhenClosed = false
        let content = NSView()
        let label = NSTextField(labelWithString: "Opening \(YotolioBrand.name)… / 正在打开 \(YotolioBrand.name)…")
        label.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            label.centerYAnchor.constraint(equalTo: content.centerYAnchor),
        ])
        window.contentView = content
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startService()
    }

    private func installMenus() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu(title: YotolioBrand.name)
        appMenu.addItem(withTitle: "About \(YotolioBrand.name)", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit \(YotolioBrand.name)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        for (title, action, key) in [
            ("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
            ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a"),
        ] {
            edit.addItem(withTitle: title, action: NSSelectorFromString(action), keyEquivalent: key)
        }
        editItem.submenu = edit

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let view = NSMenu(title: "View")
        let reload = NSMenuItem(title: "Refresh", action: #selector(refreshView(_:)), keyEquivalent: "r")
        reload.target = self
        view.addItem(reload)
        viewItem.submenu = view
        NSApp.mainMenu = main
    }

    @objc private func refreshView(_ sender: Any?) {
        model?.refresh()
    }

    private func pythonExecutable() throws -> URL {
        for path in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"] {
            guard FileManager.default.isExecutableFile(atPath: path) else { continue }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = ["-c", "import sys; sys.exit(sys.version_info < (3, 11))"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
            } catch {
                throw LaunchError(message: "Cannot start Python at \(path): \(error.localizedDescription)")
            }
            process.waitUntilExit()
            if process.terminationStatus == 0 { return process.executableURL! }
        }
        throw LaunchError(message: "Python 3.11 or newer is required. Install it from python.org, then reopen \(YotolioBrand.name).\n\n需要先安装 Python 3.11 或更新版本。")
    }

    private func startService(replaceIdleService: Bool = false) {
        let project = root
        DispatchQueue.global(qos: .userInitiated).async {
            let result: Result<URL, Error>
            do {
                let script = project.appendingPathComponent("yoto_ui.py")
                guard FileManager.default.fileExists(atPath: script.path) else {
                    throw LaunchError(message: "Keep this app next to yoto_ui.py and the web folder.\n\n请将应用与项目文件放在同一文件夹。")
                }
                let process = Process()
                process.executableURL = try self.pythonExecutable()
                process.arguments = [script.path, "--native-launch"]
                if replaceIdleService { process.arguments?.append("--replace-idle-service") }
                process.currentDirectoryURL = project
                let output = Pipe()
                let errors = Pipe()
                process.standardOutput = output
                process.standardError = errors
                try process.run()
                process.waitUntilExit()
                let stdout = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                let stderr = String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                guard process.terminationStatus == 0 else {
                    if process.terminationStatus == 42 && stdout.split(whereSeparator: \.isNewline).contains("YOTO_UI_RESTART_REQUIRED") {
                        throw RestartRequired()
                    }
                    throw LaunchError(message: "The local service could not start.\n\(stderr.suffix(3000))\nSee .yoto-myo/ui-launch.log.")
                }
                let prefix = "YOTO_UI_NATIVE "
                guard let line = stdout.split(whereSeparator: \.isNewline).last(where: { $0.hasPrefix(prefix) }),
                      let url = URL(string: String(line.dropFirst(prefix.count))),
                      url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
                      url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
                      url.path.isEmpty else {
                    throw LaunchError(message: "The local service returned an invalid address. See .yoto-myo/ui-launch.log.")
                }
                result = .success(url)
            } catch {
                result = .failure(error)
            }
            DispatchQueue.main.async {
                self.starting = false
                switch result {
                case .success(let url):
                    self.serviceURL = url
                    self.showNativeView(url)
                case .failure(let error):
                    if error is RestartRequired {
                        self.offerServiceRestart()
                    } else {
                        self.showStartupError(error.localizedDescription)
                    }
                }
            }
        }
    }

    private func offerServiceRestart() {
        let alert = NSAlert()
        alert.messageText = "Restart the older Yoto session? / 重新启动旧版本地服务？"
        alert.informativeText = "The older service must restart to use the current app. A running task will not be stopped. Finish editing in any older window first; saved login and progress will remain.\n\n旧版服务需重新启动才能使用新版。不会中断正在运行的任务；请先完成旧窗口中的编辑。已保存的登录和进度会保留。"
        alert.addButton(withTitle: "Restart and open / 重启并打开")
        alert.addButton(withTitle: "Cancel / 取消")
        if alert.runModal() == .alertFirstButtonReturn {
            starting = true
            startService(replaceIdleService: true)
        } else {
            NSApp.terminate(nil)
        }
    }

    private func showStartupError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "\(YotolioBrand.name) could not start / 启动失败"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.addButton(withTitle: "Quit / 退出")
        if message.contains("Python 3.11 or newer is required") {
            alert.addButton(withTitle: "Get Python / 下载 Python")
        }
        let result = alert.runModal()
        if result == .alertSecondButtonReturn, let url = URL(string: "https://www.python.org/downloads/macos/") {
            NSWorkspace.shared.open(url)
        }
        NSApp.terminate(nil)
    }

    private func showNativeView(_ url: URL) {
        let model = YotolioModel(serviceURL: url)
        self.model = model
        window.contentView = NSHostingView(rootView: YotolioNativeView(model: model))
        model.start()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if starting {
            let alert = NSAlert()
            alert.messageText = "\(YotolioBrand.name) is starting / 应用正在启动"
            alert.informativeText = "Please wait a moment before quitting. / 请稍等片刻再退出。"
            alert.runModal()
            return .terminateCancel
        }
        guard let url = serviceURL else { return .terminateNow }
        stopService(url) { result in
            switch result {
            case .stopped:
                sender.reply(toApplicationShouldTerminate: true)
            case .busy:
                let alert = NSAlert()
                alert.messageText = "A task is still running / 任务仍在运行"
                alert.informativeText = "Finish or pause the task before quitting.\n请等待任务完成或暂停后再退出。"
                alert.runModal()
                sender.reply(toApplicationShouldTerminate: false)
            case .failed(let reason):
                let alert = NSAlert()
                alert.messageText = "Could not stop the local service / 无法停止本地服务"
                alert.informativeText = reason
                alert.addButton(withTitle: "Keep open / 保持打开")
                alert.addButton(withTitle: "Quit, leave service running / 退出但保留服务")
                sender.reply(toApplicationShouldTerminate: alert.runModal() == .alertSecondButtonReturn)
            }
        }
        return .terminateLater
    }

    private func stopService(_ url: URL, completion: @escaping (StopResult) -> Void) {
        let finish: (StopResult) -> Void = { result in DispatchQueue.main.async { completion(result) } }
        var bootstrap = URLRequest(url: url.appendingPathComponent("api/bootstrap"))
        bootstrap.timeoutInterval = 5
        URLSession.shared.dataTask(with: bootstrap) { data, response, error in
            if let error = error as NSError? {
                if error.domain == NSURLErrorDomain && error.code == NSURLErrorCannotConnectToHost {
                    finish(.stopped)
                } else {
                    finish(.failed(error.localizedDescription))
                }
                return
            }
            guard let response = response as? HTTPURLResponse, response.statusCode == 200,
                  let data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let csrf = json["csrf"] as? String else {
                finish(.failed("The local service did not return its shutdown token."))
                return
            }
            var request = URLRequest(url: url.appendingPathComponent("api/quit"))
            request.httpMethod = "POST"
            request.timeoutInterval = 5
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue(csrf, forHTTPHeaderField: "X-Yoto-CSRF")
            request.httpBody = Data("{}".utf8)
            URLSession.shared.dataTask(with: request) { _, response, error in
                if let error {
                    finish(.failed(error.localizedDescription))
                } else if let response = response as? HTTPURLResponse, response.statusCode == 200 {
                    finish(.stopped)
                } else if let response = response as? HTTPURLResponse, response.statusCode == 409 {
                    finish(.busy)
                } else {
                    finish(.failed("The local service refused the shutdown request."))
                }
            }.resume()
        }.resume()
    }
}

@main
struct YotolioMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = YotolioApp()
        application.delegate = delegate
        application.run()
    }
}
