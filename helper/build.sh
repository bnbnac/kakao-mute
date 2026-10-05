#!/usr/bin/env bash
# VdTest.java -> build/vdtest.jar (dex 포함, app_process 용)
# 필요: JDK 17+, Android SDK (build-tools 의 d8, platforms;android-XX 의 android.jar)
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [ -z "$SDK" ]; then
  for c in "$HOME/Library/Android/sdk" "$HOME/Android/Sdk" \
           "/opt/homebrew/share/android-commandlinetools" \
           "/usr/local/share/android-commandlinetools" \
           "${LOCALAPPDATA:-/nonexistent}/Android/Sdk"; do
    if [ -d "$c" ]; then SDK="$c"; break; fi
  done
fi
[ -d "${SDK:-}" ] || { echo "Android SDK를 못 찾음. ANDROID_HOME을 지정하세요." >&2; exit 1; }

BT="$SDK/build-tools/$(ls "$SDK/build-tools" | sort -V | tail -1)"
ANDROID_JAR=""
for p in $(ls "$SDK/platforms" | sort -rV); do
  if [ -f "$SDK/platforms/$p/android.jar" ]; then ANDROID_JAR="$SDK/platforms/$p/android.jar"; break; fi
done
[ -n "$ANDROID_JAR" ] || { echo "platforms;android-XX 가 설치돼 있지 않음 (sdkmanager 로 설치)" >&2; exit 1; }

D8=""
for ext in "" .bat .exe; do
  if [ -f "$BT/d8$ext" ]; then D8="$BT/d8$ext"; break; fi
done
[ -n "$D8" ] || { echo "build-tools 에 d8 없음" >&2; exit 1; }

OUT=build
rm -rf "$OUT"
mkdir -p "$OUT/classes"
javac --release 17 -nowarn -cp "$ANDROID_JAR" -d "$OUT/classes" VdTest.java
"$D8" --lib "$ANDROID_JAR" --min-api 26 --output "$OUT" $(find "$OUT/classes" -name '*.class')
(cd "$OUT" && jar cf vdtest.jar classes.dex)
echo "built: helper/$OUT/vdtest.jar"
