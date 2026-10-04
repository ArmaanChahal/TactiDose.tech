-- TactiDose analytics queries for Snowflake (handoff §20).
--
-- Format: every query starts with "-- name: <identifier>" followed by
-- "-- description: <one line>". tactidose.integrations.snowflake.SnowflakeSync.report()
-- runs the blocks in file order (GET /api/analytics/snowflake/report) and returns at most
-- 200 rows per query. Text before the first "-- name:" (this header) is ignored.
--
-- Tables (created by SnowflakeSync.ensure_schema()):
--   ADHERENCE_EVENTS  one row per dose event, EVENT_UID = '<device_id>:<event_id>'.
--                     The latest state wins (MERGE on RECORDED_AT).
--   DEVICE_EVENTS     hardware faults / resets / disconnects, one row per EVENT_KEY.
-- Conventions:
--   * *_AT columns are TIMESTAMP_NTZ holding UTC.
--   * SCHEDULED_LOCAL_DATE / _HOUR / _DOW and TIME_WINDOW are in the device's own time zone
--     (computed on the device). TIME_WINDOW: morning 05-12, afternoon 12-17, evening 17-22,
--     night 22-05.
--   * "Last 30 days" = local dates from CURRENT_DATE() - 29 (session time zone) for dose
--     events, and OCCURRED_AT within 30 days of SYSDATE() (UTC) for device events.
--   * FINAL_STATUS: TAKEN (confirmed), DISPENSED (accessed, not confirmed), MISSED,
--     HARDWARE_ERROR, CANCELLED (skipped by a caregiver), SCHEDULED / DUE / DISPENSING (open).
--     "Resolved" doses = TAKEN + DISPENSED + MISSED + HARDWARE_ERROR. Adherence rate =
--     TAKEN / resolved, the same definition as the local /api/analytics/summary.
--   * Every query de-duplicates defensively with QUALIFY ROW_NUMBER() in case rows were ever
--     loaded outside the MERGE path (manual COPY, replays).
--   * Data is de-identified: pseudonymous USER_HASH / SCHEDULE_HASH, no names, no medication
--     details. Everything here is analytics only and never feeds back into dispensing.

-- name: adherence_overview
-- description: Overall outcome counts and adherence rate per device for the last 30 days.
-- One row per device; open doses (not yet resolved) are counted separately.
WITH doses AS (
    SELECT *
    FROM ADHERENCE_EVENTS
    WHERE SCHEDULED_LOCAL_DATE >= DATEADD(day, -29, CURRENT_DATE())
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY EVENT_UID ORDER BY RECORDED_AT DESC NULLS LAST, SYNCED_AT DESC NULLS LAST) = 1
)
SELECT
    DEVICE_ID,
    COUNT_IF(FINAL_STATUS <> 'CANCELLED') AS SCHEDULED_DOSES,
    COUNT_IF(FINAL_STATUS = 'TAKEN') AS TAKEN_DOSES,
    COUNT_IF(FINAL_STATUS = 'DISPENSED') AS ACCESSED_UNCONFIRMED,
    COUNT_IF(FINAL_STATUS = 'MISSED') AS MISSED_DOSES,
    COUNT_IF(FINAL_STATUS = 'HARDWARE_ERROR') AS HARDWARE_ERROR_DOSES,
    COUNT_IF(FINAL_STATUS = 'CANCELLED') AS CANCELLED_DOSES,
    COUNT_IF(FINAL_STATUS IN ('SCHEDULED', 'DUE', 'DISPENSING')) AS OPEN_DOSES,
    ROUND(COUNT_IF(FINAL_STATUS = 'TAKEN')
          / NULLIF(COUNT_IF(FINAL_STATUS IN ('TAKEN', 'DISPENSED', 'MISSED', 'HARDWARE_ERROR')), 0),
          3) AS ADHERENCE_RATE
FROM doses
GROUP BY DEVICE_ID
ORDER BY DEVICE_ID;

-- name: missed_by_time_window
-- description: Most frequently missed time window over the last 30 days (missed doses and miss rate per window).
-- Sorted so the first row is the window with the most missed doses. Miss rate = missed /
-- resolved doses in that window.
WITH doses AS (
    SELECT *
    FROM ADHERENCE_EVENTS
    WHERE SCHEDULED_LOCAL_DATE >= DATEADD(day, -29, CURRENT_DATE())
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY EVENT_UID ORDER BY RECORDED_AT DESC NULLS LAST, SYNCED_AT DESC NULLS LAST) = 1
)
SELECT
    TIME_WINDOW,
    COUNT_IF(FINAL_STATUS IN ('TAKEN', 'DISPENSED', 'MISSED', 'HARDWARE_ERROR')) AS RESOLVED_DOSES,
    COUNT_IF(FINAL_STATUS = 'MISSED') AS MISSED_DOSES,
    ROUND(COUNT_IF(FINAL_STATUS = 'MISSED')
          / NULLIF(COUNT_IF(FINAL_STATUS IN ('TAKEN', 'DISPENSED', 'MISSED', 'HARDWARE_ERROR')), 0),
          3) AS MISS_RATE
FROM doses
GROUP BY TIME_WINDOW
ORDER BY 3 DESC, 4 DESC NULLS LAST, 1;

-- name: average_delays
-- description: Average and median delay from the scheduled time to dispensing and to the "taken" confirmation, per device (last 30 days).
-- Minutes, computed on the device; negative values mean earlier than scheduled (the window
-- opens before the scheduled time). Dispense delay covers every accessed dose, confirm delay
-- only confirmed (TAKEN) doses.
WITH doses AS (
    SELECT *
    FROM ADHERENCE_EVENTS
    WHERE SCHEDULED_LOCAL_DATE >= DATEADD(day, -29, CURRENT_DATE())
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY EVENT_UID ORDER BY RECORDED_AT DESC NULLS LAST, SYNCED_AT DESC NULLS LAST) = 1
)
SELECT
    DEVICE_ID,
    COUNT_IF(DISPENSE_DELAY_MINUTES IS NOT NULL) AS DISPENSED_DOSES,
    ROUND(AVG(DISPENSE_DELAY_MINUTES), 1) AS AVG_SCHEDULE_TO_DISPENSE_MIN,
    ROUND(MEDIAN(DISPENSE_DELAY_MINUTES), 1) AS MEDIAN_SCHEDULE_TO_DISPENSE_MIN,
    COUNT_IF(CONFIRM_DELAY_MINUTES IS NOT NULL) AS CONFIRMED_DOSES,
    ROUND(AVG(CONFIRM_DELAY_MINUTES), 1) AS AVG_SCHEDULE_TO_CONFIRM_MIN,
    ROUND(MEDIAN(CONFIRM_DELAY_MINUTES), 1) AS MEDIAN_SCHEDULE_TO_CONFIRM_MIN,
    ROUND(AVG(CONFIRM_DELAY_MINUTES - DISPENSE_DELAY_MINUTES), 1) AS AVG_DISPENSE_TO_CONFIRM_MIN
FROM doses
GROUP BY DEVICE_ID
ORDER BY DEVICE_ID;

-- name: adherence_trend_daily
-- description: Adherence trend by local day for the last 30 days (taken / resolved doses; days without doses show zeros).
-- The generator produces one row per calendar day so gaps are visible; ROW_NUMBER() over
-- SEQ4() is used because SEQ4() alone is not guaranteed to be gap-free.
WITH days AS (
    SELECT DATEADD(day, -(ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1), CURRENT_DATE()) AS LOCAL_DATE
    FROM TABLE(GENERATOR(ROWCOUNT => 30))
),
doses AS (
    SELECT *
    FROM ADHERENCE_EVENTS
    WHERE SCHEDULED_LOCAL_DATE >= DATEADD(day, -29, CURRENT_DATE())
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY EVENT_UID ORDER BY RECORDED_AT DESC NULLS LAST, SYNCED_AT DESC NULLS LAST) = 1
),
daily AS (
    SELECT
        SCHEDULED_LOCAL_DATE AS DOSE_DATE,
        COUNT_IF(FINAL_STATUS <> 'CANCELLED') AS SCHEDULED_DOSES,
        COUNT_IF(FINAL_STATUS = 'TAKEN') AS TAKEN_DOSES,
        COUNT_IF(FINAL_STATUS = 'DISPENSED') AS ACCESSED_UNCONFIRMED,
        COUNT_IF(FINAL_STATUS = 'MISSED') AS MISSED_DOSES,
        COUNT_IF(FINAL_STATUS = 'HARDWARE_ERROR') AS HARDWARE_ERROR_DOSES
    FROM doses
    GROUP BY SCHEDULED_LOCAL_DATE
)
SELECT
    d.LOCAL_DATE,
    COALESCE(x.SCHEDULED_DOSES, 0) AS SCHEDULED_DOSES,
    COALESCE(x.TAKEN_DOSES, 0) AS TAKEN_DOSES,
    COALESCE(x.ACCESSED_UNCONFIRMED, 0) AS ACCESSED_UNCONFIRMED,
    COALESCE(x.MISSED_DOSES, 0) AS MISSED_DOSES,
    COALESCE(x.HARDWARE_ERROR_DOSES, 0) AS HARDWARE_ERROR_DOSES,
    ROUND(x.TAKEN_DOSES
          / NULLIF(x.TAKEN_DOSES + x.ACCESSED_UNCONFIRMED + x.MISSED_DOSES + x.HARDWARE_ERROR_DOSES, 0),
          3) AS ADHERENCE_RATE
FROM days d
LEFT JOIN daily x ON x.DOSE_DATE = d.LOCAL_DATE
ORDER BY d.LOCAL_DATE;

-- name: device_errors_by_code
-- description: Device error frequency by error code over the last 30 days (faults, resets, disconnects).
-- ERROR_CODE falls back to the event type when the device reported no code.
WITH device_errors AS (
    SELECT *
    FROM DEVICE_EVENTS
    WHERE OCCURRED_AT >= DATEADD(day, -30, SYSDATE())
    QUALIFY ROW_NUMBER() OVER (PARTITION BY EVENT_KEY ORDER BY SYNCED_AT DESC NULLS LAST) = 1
)
SELECT
    COALESCE(CODE, EVENT_TYPE, 'UNKNOWN') AS ERROR_CODE,
    COUNT(*) AS EVENT_COUNT,
    COUNT(DISTINCT DEVICE_ID) AS DEVICES,
    MIN(OCCURRED_AT) AS FIRST_SEEN_UTC,
    MAX(OCCURRED_AT) AS LAST_SEEN_UTC
FROM device_errors
GROUP BY 1
ORDER BY 2 DESC, 1;

-- name: device_errors_by_day
-- description: Device errors per UTC day and error code for the last 30 days.
WITH device_errors AS (
    SELECT *
    FROM DEVICE_EVENTS
    WHERE OCCURRED_AT >= DATEADD(day, -30, SYSDATE())
    QUALIFY ROW_NUMBER() OVER (PARTITION BY EVENT_KEY ORDER BY SYNCED_AT DESC NULLS LAST) = 1
)
SELECT
    TO_DATE(OCCURRED_AT) AS DAY_UTC,
    COALESCE(CODE, EVENT_TYPE, 'UNKNOWN') AS ERROR_CODE,
    COUNT(*) AS EVENT_COUNT
FROM device_errors
GROUP BY 1, 2
ORDER BY 1, 3 DESC, 2;

-- name: hardware_error_rate_by_device
-- description: Hardware-error rate per device over the last 30 days (doses whose dispense hit a hardware problem / doses with a dispense attempt) plus device fault events.
-- A dose "hit a hardware problem" when it ended in HARDWARE_ERROR, was locked for caregiver
-- review, needed more than one attempt, or its last hardware result was not an OK reply.
WITH doses AS (
    SELECT *
    FROM ADHERENCE_EVENTS
    WHERE SCHEDULED_LOCAL_DATE >= DATEADD(day, -29, CURRENT_DATE())
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY EVENT_UID ORDER BY RECORDED_AT DESC NULLS LAST, SYNCED_AT DESC NULLS LAST) = 1
),
per_device AS (
    SELECT
        DEVICE_ID,
        COUNT_IF(ATTEMPTS > 0) AS ATTEMPTED_DOSES,
        COUNT_IF(ATTEMPTS > 0 AND (
            FINAL_STATUS = 'HARDWARE_ERROR'
            OR NEEDS_REVIEW
            OR ATTEMPTS > 1
            OR (HARDWARE_RESULT IS NOT NULL AND HARDWARE_RESULT NOT LIKE 'OK%'))) AS PROBLEM_DOSES
    FROM doses
    GROUP BY DEVICE_ID
),
faults AS (
    SELECT DEVICE_ID, COUNT(*) AS FAULT_EVENTS
    FROM (
        SELECT *
        FROM DEVICE_EVENTS
        WHERE OCCURRED_AT >= DATEADD(day, -30, SYSDATE())
        QUALIFY ROW_NUMBER() OVER (PARTITION BY EVENT_KEY ORDER BY SYNCED_AT DESC NULLS LAST) = 1
    )
    GROUP BY DEVICE_ID
)
SELECT
    COALESCE(p.DEVICE_ID, f.DEVICE_ID) AS DEVICE,
    COALESCE(p.ATTEMPTED_DOSES, 0) AS DOSES_WITH_DISPENSE_ATTEMPT,
    COALESCE(p.PROBLEM_DOSES, 0) AS DOSES_WITH_HARDWARE_PROBLEM,
    ROUND(p.PROBLEM_DOSES / NULLIF(p.ATTEMPTED_DOSES, 0), 3) AS HARDWARE_ERROR_RATE,
    COALESCE(f.FAULT_EVENTS, 0) AS DEVICE_FAULT_EVENTS
FROM per_device p
FULL OUTER JOIN faults f ON f.DEVICE_ID = p.DEVICE_ID
ORDER BY 4 DESC NULLS LAST, 1;
