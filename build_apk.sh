#!/data/data/com.termux/files/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "=== Building KhmerDub Native Android APK ==="

rm -rf build
mkdir -p build/gen build/obj build/bin libs

if [ ! -f libs/android.jar ]; then
    if [ -f /data/data/com.termux/files/home/ytd/libs/android.jar ]; then
        cp /data/data/com.termux/files/home/ytd/libs/android.jar libs/android.jar
    else
        echo "[1/7] Downloading android.jar..."
        curl -sSL -o libs/android.jar "https://github.com/Sable/android-platforms/raw/master/android-30/android.jar"
    fi
fi

echo "[2/7] Generating R.java..."
aapt package -f -m \
    -J build/gen \
    -S res \
    -A assets \
    -M AndroidManifest.xml \
    -I libs/android.jar

echo "[3/7] Compiling Java source files..."
javac -d build/obj \
    -classpath libs/android.jar \
    -sourcepath "src:build/gen" \
    $(find src build/gen -name "*.java")

echo "[4/7] Converting bytecode to DEX using d8..."
d8 --output build/bin --classpath libs/android.jar $(find build/obj -name "*.class")

echo "[5/7] Packaging APK with AAPT..."
aapt package -f \
    -M AndroidManifest.xml \
    -S res \
    -A assets \
    -I libs/android.jar \
    -F build/app-unsigned.apk \
    build/bin

echo "[6/7] Zip-aligning APK..."
zipalign -f -p 4 build/app-unsigned.apk build/app-aligned.apk

echo "[7/7] Signing APK with apksigner..."
if [ ! -f debug.keystore ]; then
    keytool -genkey -v \
        -keystore debug.keystore \
        -storepass android \
        -alias androiddebugkey \
        -keypass android \
        -keyalg RSA \
        -keysize 2048 \
        -validity 10000 \
        -dname "CN=Android Debug,O=Android,C=US"
fi

apksigner sign \
    --ks debug.keystore \
    --ks-pass pass:android \
    --key-pass pass:android \
    --out khmerdubapk.apk \
    build/app-aligned.apk

cp khmerdubapk.apk khmerdubapk-v1.0.1.apk

echo "=== BUILD SUCCESSFUL ==="
echo "APK Output Path: $APP_DIR/khmerdubapk.apk"
ls -lh khmerdubapk.apk khmerdubapk-v1.0.1.apk
