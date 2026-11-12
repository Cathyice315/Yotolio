import AppKit
import Foundation

guard CommandLine.arguments.count == 3,
      let source = NSImage(contentsOfFile: CommandLine.arguments[1]) else {
    fputs("Usage: swift generate_app_icon.swift web/icon.svg output.iconset\n", stderr)
    exit(1)
}

let destination = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        guard let bitmap = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ), let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
            fputs("Cannot create icon bitmap\n", stderr)
            exit(1)
        }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        context.imageInterpolation = .high
        source.draw(in: NSRect(x: 0, y: 0, width: pixels, height: pixels),
                    from: NSRect(origin: .zero, size: source.size),
                    operation: .copy, fraction: 1)
        NSGraphicsContext.restoreGraphicsState()
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            fputs("Cannot encode icon PNG\n", stderr)
            exit(1)
        }
        let suffix = scale == 2 ? "@2x" : ""
        try png.write(to: destination.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
