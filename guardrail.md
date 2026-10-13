# guardrail.md

## Purpose

These guardrails define safety, integrity, performance, and scope
constraints for the local Raspberry Pi Swine Monitoring Dashboard. They
supplement `Instruction.md` and must be followed during implementation,
testing, and demonstration.

## 1. Animal-health claims and screening

-   **Do not diagnose.** The dashboard is a monitoring and screening
    interface, not a veterinary diagnostic system.
-   **Do not equate surface and core temperature.** A threshold derived
    from core temperature must not be presented as a validated
    surface-temperature fever threshold.
-   Do not display `Fever confirmed`, `Sick`, or equivalent diagnostic
    claims unless a separate, appropriate validation process supports
    that claim.
-   Use cautious states such as `Monitoring`, `Threshold reached`, and
    `Inconclusive`.
-   Display the actual measured surface temperature, units, timestamp,
    and relevant screening criteria when an alert is shown.
-   Do not invent thresholds, clinical criteria, confidence scores, or
    missing sensor values.
-   If readings are unavailable, stale, invalid, or inconsistent, show
    that limitation explicitly. Do not silently substitute an old
    reading and make it appear current.
-   Treat posture and lying duration as observations from the monitoring
    pipeline, not as definitive evidence of a disease.

## 2. Camera ownership and resource limits

-   Each physical camera must have one acquisition path owned by the
    existing monitoring pipeline.
-   The dashboard must not independently open a camera, create duplicate
    capture loops, or start a second thermal acquisition process.
-   Reuse frames and derived measurements already produced by the
    monitoring pipeline.
-   Camera preview is optional and must not block delivery of core
    logging and session-management functions.
-   Start with a low preview rate, approximately 1--2 updates per
    second, and benchmark on the actual Raspberry Pi.
-   Do not increase preview rate, resolution, or encoding cost without
    measuring effects on inference latency, frame rate, and input lag.
-   If preview degrades monitoring reliability, disable or remove the
    preview.
-   Browser refresh frequency must never be used as the clock for
    posture inference or cumulative lying-duration calculations.

## 3. Pen and session data integrity

-   A pen ID identifies a physical pen, not a permanent pig identity.
-   Every monitoring session must have a distinct session ID and
    explicit start/end timestamps.
-   At most one session should be active per pen unless a different
    behavior is explicitly designed and safely enforced.
-   A replacement animal must start a new session. Do not reuse the
    prior session or carry forward its active measurements or
    lying-duration counter.
-   Ending a session must preserve its historical records by default.
-   Do not permanently delete history as a side effect of starting or
    ending a session.
-   Any future clear-history or delete operation must be separate,
    clearly labelled, and require explicit confirmation.
-   Associate every measurement and alert with the correct pen and
    session. Reject or flag records whose association cannot be
    established.
-   Record session start/end and alert events in the log.
-   Use database transactions for related state changes where
    appropriate. Avoid a UI state that claims a session started when the
    corresponding persisted operation failed.
-   Use a consistent timestamp convention throughout the application and
    document it. Do not fabricate timestamps when the source timestamp
    is missing.

## 4. Data storage and logging

-   Store prototype monitoring records locally in SQLite unless the
    project scope is explicitly revised.
-   Do not require cloud access for normal dashboard operation.
-   Persist selected measurements and events according to a deliberate
    sampling/logging policy; do not store every raw camera frame by
    default.
-   Save evidence images only when the feature is enabled and there is a
    clear need.
-   Use local evidence paths and handle missing files gracefully.
-   Do not display a successful save or session transition before the
    database operation has succeeded.
-   Handle database errors without silently discarding records or
    presenting unpersisted values as saved.
-   Keep historical logs readable and filterable enough to demonstrate
    the event sequence.

## 5. Alert behavior and notification claims

-   The dashboard's visible alert is the primary notification mechanism
    for the prototype.
-   An alert must identify the affected pen/session, include a
    timestamp, and show the measurements or configured conditions that
    triggered it.
-   Do not emit an alert based on an invented or unvalidated threshold.
-   Distinguish a session lifecycle confirmation from a screening alert;
    they are different event types.
-   Make clear that a dashboard alert is only visible when a person
    views or checks the dashboard.
-   Do not claim SMS or push notification integration merely because a
    GSM module or messaging service passed an isolated test.
-   SMS or push notifications remain out of required scope until
    end-to-end triggering, delivery, error handling, and reliability
    have been tested.

## 6. Network and application security

-   The dashboard is intended for a local network. Do not expose it to
    the public internet by default.
-   Bind and configure the web server deliberately; document the address
    and network assumptions for access from other devices.
-   Do not assume `localhost` on a phone or laptop refers to the
    Raspberry Pi.
-   Do not add unnecessary external dependencies, remote services,
    analytics, or cloud data transfer.
-   Do not expose secrets, credentials, environment variables, or
    sensitive system details in the interface or logs.
-   Validate and constrain user-supplied values such as pen IDs, session
    actions, filters, and file paths.
-   Prevent user-controlled file paths from being used to read arbitrary
    files or write outside the intended evidence directory.
-   Use safe database query parameterization; do not concatenate user
    input into SQL statements.
-   Protect state-changing routes against accidental or unauthorized
    requests appropriate to the deployment environment. If access
    control is later added, enforce it server-side rather than relying
    only on hidden UI controls.
-   Do not claim the system has authentication, encryption, or access
    controls unless those controls are actually implemented and tested.

## 7. UI behavior and accessibility

-   Make pen, session, reading freshness, and alert status
    distinguishable without relying on color alone.
-   Label temperature in °C and make unknown or unavailable values
    explicit.
-   Provide clear confirmation of successful session start/end actions.
-   Show actionable errors when a save or state transition fails; do not
    silently ignore failures.
-   Disable or safely reject invalid lifecycle actions, including
    starting a second active session for a pen that already has one.
-   Confirm destructive operations separately from ordinary session
    actions.
-   Keep the UI responsive and usable on common phone, laptop, and Pi
    display sizes.
-   The camera preview must be clearly presented as an observational
    view, not the source of timing for monitoring calculations.

## 8. Scope control

Required scope: 1. Pen IDs and session start/end controls. 2.
Timestamped temperature and posture records in local storage. 3. A
dashboard showing latest readings and screening status. 4. Session
transition confirmations and historical logs. 5. A visible dashboard
alert for configured screening conditions.

Optional scope, only after the required features work reliably: -
Low-rate RGB preview using the existing capture pipeline. - Thermal
preview using an existing thermal worker's output. - Evidence snapshots
stored locally.

Not required by default: - Native mobile application. - Cloud hosting or
cloud database. - Complex frontend framework. - User accounts, unless
required by the deployment context. - SMS or push notifications. -
Simultaneous independent camera streams or high-frame-rate browser
video.

Do not add optional features if they threaten the reliability of the
required workflow.

## 9. Failure handling and truthful status

-   Treat camera, sensor, inference, database, and network failures as
    distinct conditions where possible.
-   Show when a measurement is missing or stale rather than retaining a
    misleading healthy-looking status.
-   If the monitoring pipeline stops updating, indicate that the data is
    stale or unavailable.
-   Do not infer a pig's status from the absence of an alert.
-   Do not mark an event as completed until the relevant operation has
    succeeded.
-   Preserve prior persisted data when a new session fails to start.
-   Avoid silent fallbacks that make the dashboard appear operational
    when its data source is not functioning.

## 10. Verification checklist

Before demonstration, verify each item:

-   [ ] The dashboard is reachable from the Pi and a permitted device on
    the same network.
-   [ ] The pen ID remains stable across sessions.
-   [ ] Ending a session records the end event and preserves historical
    records.
-   [ ] Starting a replacement session creates a new session ID and
    fresh active counters.
-   [ ] No prior session measurements or lying duration are inherited by
    the new session.
-   [ ] Measurements have timestamps and are associated with the correct
    pen/session.
-   [ ] Missing and stale readings are clearly identified.
-   [ ] Screening alerts show the triggering measurements and are
    logged.
-   [ ] No unvalidated core-temperature threshold is represented as a
    validated surface-temperature fever threshold.
-   [ ] The dashboard uses the existing camera acquisition pipeline.
-   [ ] Optional preview does not materially degrade monitoring
    performance; otherwise it is disabled.
-   [ ] No claim is made that SMS, cloud access, authentication, or
    diagnosis works unless it has been implemented and verified.
