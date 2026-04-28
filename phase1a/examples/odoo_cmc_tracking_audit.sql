-- CMC / EMTC tracking coverage audit
--
-- Carrier mapping:
--   24 = EMTC
--   25 = CMC
--
-- Business interpretation:
-- - only outgoing deliveries
-- - only completed deliveries (`state = 'done'`)
-- - for each carrier separately, start counting from the earliest delivery
--   that already has a tracking number
-- - from that point onward, count how many deliveries have / do not have tracking
--
-- Assumption:
-- - tracking field = `stock_picking.carrier_tracking_ref`
-- - business date = `date_done`, fallback `scheduled_date`, fallback `create_date`

WITH carrier_scope AS (
    SELECT 24::integer AS carrier_id, 'EMTC'::text AS carrier_name
    UNION ALL
    SELECT 25::integer AS carrier_id, 'CMC'::text AS carrier_name
),
done_pickings AS (
    SELECT
        sp.id,
        sp.name,
        sp.company_id,
        sp.carrier_id,
        cs.carrier_name,
        sp.carrier_tracking_ref,
        COALESCE(sp.date_done, sp.scheduled_date, sp.create_date) AS business_datetime,
        COALESCE(sp.date_done::date, sp.scheduled_date::date, sp.create_date::date) AS business_date
    FROM stock_picking sp
    JOIN stock_picking_type spt
      ON spt.id = sp.picking_type_id
    JOIN carrier_scope cs
      ON cs.carrier_id = sp.carrier_id
    WHERE spt.code = 'outgoing'
      AND sp.state = 'done'
),
first_with_tracking AS (
    SELECT
        carrier_id,
        carrier_name,
        MIN(business_datetime) AS start_datetime
    FROM done_pickings
    WHERE NULLIF(BTRIM(carrier_tracking_ref), '') IS NOT NULL
    GROUP BY carrier_id, carrier_name
),
scope_rows AS (
    SELECT
        d.*
    FROM done_pickings d
    JOIN first_with_tracking f
      ON f.carrier_id = d.carrier_id
    WHERE d.business_datetime >= f.start_datetime
)
SELECT
    s.carrier_id,
    s.carrier_name,
    MIN(s.business_datetime) AS counting_start_datetime,
    MAX(s.business_datetime) AS counting_end_datetime,
    COUNT(*) AS total_done_outgoing,
    COUNT(*) FILTER (WHERE NULLIF(BTRIM(s.carrier_tracking_ref), '') IS NOT NULL) AS with_tracking_number,
    COUNT(*) FILTER (WHERE NULLIF(BTRIM(s.carrier_tracking_ref), '') IS NULL) AS without_tracking_number
FROM scope_rows s
GROUP BY s.carrier_id, s.carrier_name
ORDER BY s.carrier_id;


-- If you also want the detailed list without tracking for both carriers:
--
WITH carrier_scope AS (
    SELECT 24::integer AS carrier_id, 'EMTC'::text AS carrier_name
    UNION ALL
    SELECT 25::integer AS carrier_id, 'CMC'::text AS carrier_name
),
done_pickings AS (
    SELECT
        sp.id,
        sp.name,
        sp.company_id,
        sp.carrier_id,
        cs.carrier_name,
        sp.carrier_tracking_ref,
        COALESCE(sp.date_done, sp.scheduled_date, sp.create_date) AS business_datetime
    FROM stock_picking sp
    JOIN stock_picking_type spt
      ON spt.id = sp.picking_type_id
    JOIN carrier_scope cs
      ON cs.carrier_id = sp.carrier_id
    WHERE spt.code = 'outgoing'
      AND sp.state = 'done'
),
first_with_tracking AS (
    SELECT
        carrier_id,
        MIN(business_datetime) AS start_datetime
    FROM done_pickings
    WHERE NULLIF(BTRIM(carrier_tracking_ref), '') IS NOT NULL
    GROUP BY carrier_id
)
SELECT
    d.carrier_id,
    d.carrier_name,
    d.id AS odoo_picking_id,
    d.name AS picking_name,
    d.business_datetime,
    d.carrier_tracking_ref
FROM done_pickings d
JOIN first_with_tracking f
  ON f.carrier_id = d.carrier_id
WHERE d.business_datetime >= f.start_datetime
  AND NULLIF(BTRIM(d.carrier_tracking_ref), '') IS NULL
ORDER BY d.carrier_id, d.business_datetime, d.id;
