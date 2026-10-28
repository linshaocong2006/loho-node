#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
loho_sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [[ -z "$loho_sdk" ]]; then echo '请安装 Android SDK 并设置 ANDROID_HOME。' >&2; exit 1; fi
loho_tools="$loho_sdk/build-tools/35.0.0"
loho_platform="$loho_sdk/platforms/android-35/android.jar"
for loho_tool in "$loho_tools/aapt2" "$loho_tools/d8" "$loho_tools/zipalign" "$loho_platform"; do
    [[ -f "$loho_tool" ]] || { echo "缺少构建组件：$loho_tool" >&2; exit 1; }
done
mkdir -p build/classes build/dex build/artifacts
mapfile -d '' loho_sources < <(find src -name '*.java' -print0)
javac -source 11 -target 11 -classpath "$loho_platform" -d build/classes "${loho_sources[@]}"
mapfile -d '' loho_classes < <(find build/classes -name '*.class' -print0)
"$loho_tools/d8" --lib "$loho_platform" --min-api 26 --output build/dex "${loho_classes[@]}"
"$loho_tools/aapt2" compile --dir res -o build/resources.zip
"$loho_tools/aapt2" link -I "$loho_platform" --manifest AndroidManifest.xml -R build/resources.zip -o build/base.apk
zip -q -j build/base.apk build/dex/*.dex
"$loho_tools/zipalign" -P 16 -f 4 build/base.apk build/artifacts/LoHo-Node-unsigned.apk
cp "$loho_tools/lib/apksigner.jar" build/artifacts/apksigner.jar
"$loho_tools/aapt2" dump badging build/artifacts/LoHo-Node-unsigned.apk > build/artifacts/package-info.txt
(cd build/artifacts && sha256sum LoHo-Node-unsigned.apk apksigner.jar > SHA256SUMS)
