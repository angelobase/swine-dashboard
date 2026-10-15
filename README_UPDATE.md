# Scheduled sessions update

For installation, testing and deployment commands, see README_INSTRUCTION.md.

Copy these files over your project, keeping the folders as they are. Every file here is new or replaces the old one.
Files not included are unchanged: yolo_worker.py, thermal_worker.py, gsm_sms.py, preview_support.py, hub_discovery.py,
lib/beacon.js, public/index.html, public/style.css.

| Path | Status |
|---|---|
| prototype/inlayo8.py | replaced (scheduled sessions) |
| prototype/prototype.json | replaced (new sections; copy your own mqtt.password and device_id into it) |
| prototype/capture_once.py, screening.py, pig_ids.py, pi_store.py, calibrate.py | new |
| prototype/tests/test_scheduled.py | new |
| server.js, lib/db.js, lib/export.js, lib/mqtt_hub.js, lib/config.js | replaced |
| public/app.js | replaced |
| public/schedule.css | new (loaded by app.js) |
| tools/schedule_test.js | new |
| tools/test_push.py | replaced: now a dummy prototype (see docs/SCHEDULED_SESSIONS.md); add tools/sim_data/ to .gitignore |
| .gitignore, .gitattributes, .github/workflows/tests.yml | new: for GitHub (see README_INSTRUCTION.md section 4); hidden files, copy them too |
| config.example.json, prototype/prototype.example.json | new: settings templates committed instead of config.json / prototype.json |
| docs/SCHEDULED_SESSIONS.md | new |
| docs/PROTOTYPE.md, docs/INTEGRATION.md, docs/VERIFICATION.md | updated |

Before the first run:
1. Back up data/swine.db. The dashboard upgrades it in place when it starts (adds columns and tables only).
2. In prototype.json, set screening.abnormal_lying_consecutive_captures to the value your study cites (6 is a placeholder).
3. In config.json, set preview.enabled to true to see capture images on the tiles.
4. Stop running tools/mqtt_bridge.py; the dashboard's built-in broker replaces it.
5. On the Pi: python3 calibrate.py (needs a screen), then python3 -m unittest discover -s tests -v
6. On the dashboard computer: node tools/schedule_test.js, then your existing npm test.
7. Run one Test mode session end to end before the 19-hour run.
