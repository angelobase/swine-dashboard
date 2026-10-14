I’ve read it. The methodology describes a different design from the one we’ve built. We built a live stream with real-time alerts. The methodology samples every 30 minutes and decides once, at the end of 19 hours. So this is a structural change in several places, not a tweak to settings. Nothing has been changed yet.

Where the methodology and the code disagree

Topic	Methodology	Current code	What changes
Capture rhythm	One RGB and one thermal capture every 30 min	Cameras run continuously. Readings go out every few seconds and a live preview updates about every second	The Pi needs a capture scheduler. Cameras and YOLO stay off between captures, which is the stated reason for the 30-minute interval
Unit of monitoring	Each pig, with its own ID	One device per pen. The dashboard gets one temperature (the hottest region) and one posture (“Lying” if any pig is lying). Thermal regions and pigs are not matched	Readings must carry a list of pigs, and each pig’s temperature must come from that pig’s own thermal region
Lying duration	Estimated per pig from the samples	The dashboard accumulates seconds, and a gap over 15 s (max_lying_gap_s) breaks the count	With 30-minute gaps, the duration has to be built from consecutive samples under a rule you define
Record fields	Time, pig ID, posture, lying duration, temperature, behavioral status, thermal status	Time, pen, event, temperature, posture, lying seconds and one combined status. No pig ID and no separate behavioral and thermal statuses	Database schema change (existing databases need a migration), plus the CSV and PDF export columns
Alert rule	After 19 h: abnormal lying occurred during the period AND the final temperature is in the abnormal range	Real-time. High-temperature and abnormal-behavior flags latch, and the alert fires when both are set. The dashboard also flags “Threshold reached” after N consecutive readings	Replace with a single end-of-session evaluation per pig
SMS	Once, after the 19 h evaluation	The Pi sends one SMS during monitoring as soon as both flags are set	The SMS trigger moves to the end of the session
Temperature limit	36.7 °C	Default 35.0 °C, set separately in config.json and prototype.json	New value. The two-place setting problem matters more now
Session length	19 hours	Manual Start and End	Auto-end after a configured duration, which triggers the evaluation
Live display	None implied	Tiles go stale after 30 s, with a live camera view	Stale time must exceed the interval, and the tile should show the last captured image and a “next capture in…” countdown
Data safety	A 19-hour field run	Data exists only in MQTT messages and the dashboard database	The Pi should keep its own local log, so a Wi-Fi drop doesn’t lose the study data

What can stay: the built-in broker, auto-discovery and adding pens from the list. The same goes for sessions, the pen wall layout, the fake prototype for testing, and the test setup. The fake prototype would need updating to match.

Decisions I need from you

Pigs per camera. If each Pi watches one pig, “pen” and “pig” are the same thing and most of the per-pig work disappears. If several pigs share one view, I need to know how IDs are assigned. YOLO’s tracking IDs won’t survive 30 minutes between captures, so it would have to be position-based, ear tags, or something else. This is the biggest piece of work and depends on your answer.
What “abnormal lying” means in numbers. For example, lying in N consecutive samples, or lying for at least X minutes in total. I also need the rule for turning 30-minute snapshots into a duration, such as counting 30 minutes for every sample where the pig is lying.
The temperature rule. “Within the range of 36.7 °C” could mean at or above 36.7, or a band such as 36.7 to some upper value. I also need to know what counts as the final reading if the last capture has no pig or no valid thermal value. It could be the last valid reading, or it could count as “no result”.
Who evaluates and sends the SMS. The GSM modem is on the Pi, so either the Pi evaluates its own 19 hours and sends the SMS, or the dashboard evaluates and commands the Pi. The first works without the dashboard. The second depends on the dashboard computer being up and on the farm network for the whole run. Tell me whether the dashboard will actually be running during the 19 hours.
Statuses. The labels for “behavioral status” and “thermal status”. For example Normal / Abnormal / No data.
Saved images. Whether each interval’s RGB and thermal images should be saved as evidence. The database already has an evidence_path column.
Thermal-to-RGB matching. The FLIR Lepton and the webcam have different fields of view, so assigning a temperature to a specific pig needs alignment. Tell me whether they are mounted side by side, and whether a one-time calibration is acceptable.
Test mode. I’d suggest keeping the interval and duration configurable (for example 10 seconds and 5 minutes) so you can test without waiting 19 hours. The 30-minute and 19-hour values would stay the defaults.

Files I need. yolo_worker.py, thermal_worker.py, gsm_sms.py and prototype.json. Per-pig results and one-shot captures are mostly changes inside the workers, and I haven’t seen them yet.

Suggested order

Pi side: the scheduler, one-shot capture, a per-sample message, and a local log.
Dashboard: the new record structure with statuses and pig IDs, and the stale and gap logic.
The end-of-session evaluation, the SMS path and the 19-hour auto-end.
The tile summary, exports, fake prototype, tests and README.

Decisions 1 to 4 shape the architecture, so I’d settle those first. The rest can follow as we go.