-- Odoo POD scope export for comparison / backfill analysis
--
-- Output columns are intentionally aligned with the Phase 1A scope reader:
--   - odoo_picking_id
--   - picking_name
--   - tracking_numbers
--   - eligible
--   - in_scope
--   - company_id
--
-- Usage:
-- 1. Replace the values in the params CTE.
-- 2. Run the SELECT directly in pgAdmin / DBeaver, or wrap it in \copy in psql.
-- 3. Save as CSV with header and send that CSV back for matching.
--
-- Notes:
-- - Standard Odoo tracking field is `stock_picking.carrier_tracking_ref`.
-- - If your database uses a custom tracking field instead, replace that column.
-- - `tracking_numbers` can contain a single value or a comma-separated list.

WITH params AS (
    SELECT
        1::integer AS company_id,
        DATE '2025-01-01' AS date_from,
        DATE '2026-12-31' AS date_to
)
SELECT
    sp.id AS odoo_picking_id,
    sp.name AS picking_name,
    NULLIF(BTRIM(sp.carrier_tracking_ref), '') AS tracking_numbers,
    TRUE AS eligible,
    TRUE AS in_scope,
    sp.company_id
FROM stock_picking sp
JOIN stock_picking_type spt
  ON spt.id = sp.picking_type_id
JOIN params p
  ON p.company_id = sp.company_id
WHERE spt.code = 'outgoing'
  AND COALESCE(sp.date_done::date, sp.scheduled_date::date, sp.create_date::date)
      BETWEEN p.date_from AND p.date_to
  AND sp.company_id = p.company_id
  AND sp.state = 'done'
ORDER BY
    COALESCE(sp.date_done, sp.scheduled_date, sp.create_date) DESC,
    sp.id DESC;

-- Example psql export:
-- \copy (
--   WITH params AS (
--       SELECT
--           1::integer AS company_id,
--           DATE '2025-01-01' AS date_from,
--           DATE '2026-12-31' AS date_to
--   )
--   SELECT
--       sp.id AS odoo_picking_id,
--       sp.name AS picking_name,
--       NULLIF(BTRIM(sp.carrier_tracking_ref), '') AS tracking_numbers,
--       TRUE AS eligible,
--       TRUE AS in_scope,
--       sp.company_id
--   FROM stock_picking sp
--   JOIN stock_picking_type spt
--     ON spt.id = sp.picking_type_id
--   JOIN params p
--     ON p.company_id = sp.company_id
--   WHERE spt.code = 'outgoing'
--     AND COALESCE(sp.date_done::date, sp.scheduled_date::date, sp.create_date::date)
--         BETWEEN p.date_from AND p.date_to
--     AND sp.company_id = p.company_id
--     AND sp.state = 'done'
-- ) TO '/tmp/odoo_scope.csv' WITH CSV HEADER;
