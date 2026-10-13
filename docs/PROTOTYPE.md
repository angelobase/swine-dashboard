# Prototype (headless) over MQTT

Pi (prototype): `prototype/inlayo8.py` runs the thermal and webcam workers with no window, latches the two flags,
sends the SMS through the SIM800L when BOTH flags are 1, and publishes everything to its own MQTT broker.
Dashboard computer: `tools/mqtt_bridge.py` forwards the MQTT data to http://127.0.0.1:8080.

## One-time setup on the prototype Pi
    sudo ./setup_network.sh <wifi-ssid> <wifi-password> <mqtt-password>   # own Wi-Fi + password-protected broker
Edit `prototype.json` (mqtt.password, GSM recipient/port, threshold). Copy `YOLOv8A_SD_best.onnx` next to `inlayo8.py`.
Install: `pip install paho-mqtt pyserial` (plus the existing spidev, opencv, numpy, onnxruntime).

## Run
    python3 prototype/inlayo8.py          # prints:  mqtt://10.42.0.1:1883/proto-xxxxxx   (also in prototype_address.txt)
    MQTT_PASS=<mqtt-password> python3 tools/mqtt_bridge.py     # on the dashboard computer, joined to the Pi's Wi-Fi

In the dashboard: **+ > Add a pen**, enter the pen number and paste the printed address. Start a session for the pen.
Set `preview.enabled: true` in `config.json` for camera images.

## Topics (base = swine/<device_id>)
reading, state (retained), event, status (retained, LWT), preview/rgb, preview/thermal, cmd (dashboard -> prototype).

## Notes
- Reading temperature = hottest stable thermal region; posture = Lying if any tracked pig is lying. Thermal regions and pigs are not matched.
- The bridge stamps readings with its own arrival time, so Pi clock drift cannot get readings rejected.
- Only one camera runs at a time. `mode: auto` alternates every `cycle_s` seconds; the dashboard tile's **Cam** button picks Auto / Thermal / Webcam / Off.
- **Reset flags** on the tile = the original "Reset Flags / Start New Alert Cycle". It is refused while an SMS attempt is running.
- SMS is sent once per cycle, never retried automatically. Its result shows on the tile and is stored in table `device_events`.
