#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
APP="$ROOT/Yotolio.app"
DEFAULT_OUTPUT="$APP/Contents/MacOS/Yotolio"
OUTPUT=${1:-"$DEFAULT_OUTPUT"}
OUTPUT_PARENT=$(dirname -- "$OUTPUT")
OUTPUT_DIR=
if [ -d "$OUTPUT_PARENT" ]; then
  OUTPUT_DIR=$(CDPATH= cd -- "$OUTPUT_PARENT" && pwd -P)
elif [ "$(basename -- "$OUTPUT_PARENT")" = "MacOS" ] && [ -d "$(dirname -- "$OUTPUT_PARENT")" ]; then
  OUTPUT_DIR=$(CDPATH= cd -- "$(dirname -- "$OUTPUT_PARENT")" && pwd -P)/MacOS
fi
SIGN_APP=
case "$OUTPUT_DIR" in
  *.app/Contents/MacOS) SIGN_APP=${OUTPUT_DIR%/Contents/MacOS} ;;
esac
if [ -z "$SIGN_APP" ] && [ "$OUTPUT" = "$DEFAULT_OUTPUT" ]; then SIGN_APP="$APP"; fi
if [ -n "$SIGN_APP" ]; then
  if ! ATTRIBUTES=$(xattr "$SIGN_APP"); then
    echo "Cannot inspect the existing app; refusing to overwrite it." >&2
    exit 1
  fi
  case "$ATTRIBUTES" in
    *com.apple.FinderInfo*)
      echo "The existing app has Finder customization. Refusing to overwrite it or its icon; build to a separate output path instead." >&2
      exit 1
      ;;
  esac
fi
BUILD_DIR=$(mktemp -d "$TMPDIR/yotolio-build.XXXXXX")
cleanup() {
  rm -f "$BUILD_DIR/arm64" "$BUILD_DIR/x86_64" "$BUILD_DIR/Yotolio"
  rmdir "$BUILD_DIR"
}
trap cleanup EXIT

SDK=$(xcrun --sdk macosx --show-sdk-path)
for ARCH in arm64 x86_64; do
  xcrun swiftc -sdk "$SDK" -target "$ARCH-apple-macos12.0" -O \
    "$ROOT/Yotolio.swift" "$ROOT/YotolioModel.swift" "$ROOT/YotolioNative.swift" \
    -o "$BUILD_DIR/$ARCH" -framework AppKit -framework SwiftUI
done
xcrun lipo -create "$BUILD_DIR/arm64" "$BUILD_DIR/x86_64" -output "$BUILD_DIR/Yotolio"
codesign --force --sign - "$BUILD_DIR/Yotolio"
codesign --verify "$BUILD_DIR/Yotolio"
mkdir -p "$(dirname -- "$OUTPUT")"
mv "$BUILD_DIR/Yotolio" "$OUTPUT"
if [ -n "$SIGN_APP" ]; then
  codesign --force --sign - "$SIGN_APP"
  codesign --verify --strict "$SIGN_APP"
fi
printf 'Built %s\n' "$OUTPUT"
