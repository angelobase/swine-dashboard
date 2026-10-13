#!/usr/bin/env bash
# Installs what the prototype needs on a Raspberry Pi (Raspberry Pi OS / Debian).
# Run:  bash setup_pi.sh        (no need for chmod +x)
set -u
cd "$(dirname "$0")"

have() { python3 -c "import $1" 2>/dev/null; }

echo "== Prototype setup =="
command -v python3 >/dev/null || { echo "python3 not found. Install it first: sudo apt install python3"; exit 1; }

# 1. MQTT client + serial (for the SIM800L). apt first (works on current Pi OS), pip as fallback.
if have paho.mqtt.client && have serial; then
  echo "paho-mqtt and pyserial: already installed"
else
  echo "Installing paho-mqtt and pyserial..."
  if ! (sudo apt-get update -qq && sudo apt-get install -y python3-paho-mqtt python3-serial); then
    echo "apt failed, trying pip..."
    python3 -m pip install --user paho-mqtt pyserial 2>/dev/null \
      || python3 -m pip install --user --break-system-packages paho-mqtt pyserial \
      || { echo "Could not install paho-mqtt / pyserial. Install them manually."; exit 1; }
  fi
fi

# 2. Packages your camera workers use. Only checked here, because install steps differ per setup.
missing=""
for mod in spidev cv2 numpy onnxruntime; do have "$mod" || missing="$missing $mod"; done
if [ -n "$missing" ]; then
  echo
  echo "Still missing (needed by the camera workers):$missing"
  echo "  spidev / cv2 / numpy:  sudo apt install python3-spidev python3-opencv python3-numpy"
  echo "  onnxruntime:           python3 -m pip install --user --break-system-packages onnxruntime"
fi

# 3. Files the prototype needs in this folder
echo
for f in inlayo8.py hub_discovery.py thermal_worker.py yolo_worker.py gsm_sms.py YOLOv8A_SD_best.onnx; do
  [ -f "$f" ] && echo "found   $f" || echo "MISSING $f"
done
[ -f prototype.json ] || echo "note: prototype.json not found; defaults are used and one is created on first run"

echo
echo "Done. Start the prototype with:  python3 inlayo8.py"
