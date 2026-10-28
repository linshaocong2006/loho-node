#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
loho_sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
loho_tools="$loho_sdk/build-tools/35.0.0"
loho_platform="$loho_sdk/platforms/android-35/android.jar"
loho_qa="$PWD/build/gesture-qa"
mkdir -p "$loho_qa/classes" "$loho_qa/dex"
bash ../../loho-node-onekey.sh --extract-source "$loho_qa/panel-source"
mkdir -p "$loho_qa/assets/panel"
cp -R "$loho_qa/panel-source/www/." "$loho_qa/assets/panel/"
cp ../qa/fixture.js "$loho_qa/assets/panel/fixture.js"
python3 - "$loho_qa/assets/panel" <<'PY'
from pathlib import Path
import sys
for page in Path(sys.argv[1]).glob('*.html'):
    page.write_text(page.read_text().replace('<head>','<head><script src="fixture.js"></script>',1))
PY
javac -source 11 -target 11 -classpath "$loho_platform" -d "$loho_qa/classes" qa/src/app/loho/nodeclient/test/ZoomCheck.java
mapfile -d '' loho_classes < <(find "$loho_qa/classes" -name '*.class' -print0)
"$loho_tools/d8" --lib "$loho_platform" --min-api 26 --output "$loho_qa/dex" "${loho_classes[@]}"
"$loho_tools/aapt2" link -I "$loho_platform" --manifest qa/AndroidManifest.xml -A "$loho_qa/assets" -o "$loho_qa/check-base.apk"
zip -q -j "$loho_qa/check-base.apk" "$loho_qa/dex/classes.dex"
"$loho_tools/zipalign" -P 16 -f 4 "$loho_qa/check-base.apk" "$loho_qa/check.apk"
cp build/artifacts/LoHo-Node-unsigned.apk "$loho_qa/app.apk"
keytool -genkeypair -keystore "$loho_qa/temporary.jks" -storepass temporary -keypass temporary -alias qa -keyalg RSA -dname 'CN=Temporary gesture check' -validity 2
for loho_apk in "$loho_qa/app.apk" "$loho_qa/check.apk"; do
    "$loho_tools/apksigner" sign --ks "$loho_qa/temporary.jks" --ks-pass pass:temporary "$loho_apk"
done
"$loho_sdk/cmdline-tools/latest/bin/sdkmanager" 'emulator' 'system-images;android-30;google_apis;x86_64'
export ANDROID_AVD_HOME="$loho_qa/avd"
mkdir -p "$ANDROID_AVD_HOME"
echo no | "$loho_sdk/cmdline-tools/latest/bin/avdmanager" create avd -n loho-gesture -k 'system-images;android-30;google_apis;x86_64' --device pixel_2 --path "$ANDROID_AVD_HOME/loho-gesture.avd" --force
test -f "$ANDROID_AVD_HOME/loho-gesture.avd/config.ini"
cat > "$ANDROID_AVD_HOME/loho-gesture.ini" <<EOF
avd.ini.encoding=UTF-8
path=$ANDROID_AVD_HOME/loho-gesture.avd
target=android-30
EOF
sudo chmod 666 /dev/kvm
"$loho_sdk/emulator/emulator" -avd loho-gesture -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect -no-snapshot -camera-back none -camera-front none -memory 2048 -cores 2 -partition-size 1024 > "$loho_qa/emulator.log" 2>&1 &
loho_emulator_pid=$!
trap 'loho_status=$?; if [[ "$loho_status" != 0 ]]; then cat "$loho_qa/emulator.log"; fi; kill "$loho_emulator_pid" 2>/dev/null || true' EXIT
export PATH="$loho_sdk/platform-tools:$PATH"
timeout 120 adb wait-for-device
loho_booted=false
for loho_attempt in $(seq 1 120); do
    if [[ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == 1 ]]; then loho_booted=true; break; fi
    sleep 1
done
if [[ "$loho_booted" != true ]]; then cat "$loho_qa/emulator.log"; exit 1; fi
adb shell input keyevent 82
adb shell settings put global window_animation_scale 0
adb shell settings put global transition_animation_scale 0
adb shell settings put global animator_duration_scale 0
adb install -r "$loho_qa/app.apk"
adb install -r "$loho_qa/check.apk"
adb shell am instrument -w app.loho.nodeclient.test/.ZoomCheck | tee "$loho_qa/result.txt"
grep -q 'LOHO_ZOOM_PASS' "$loho_qa/result.txt"
grep -q 'LOHO_BACK_PASS' "$loho_qa/result.txt"
