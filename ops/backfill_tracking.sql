-- 回填 pod_sync 创建的 stock.tracking 记录：
--   补 carrier、carrier_tracking_url、tracking_number
--   数据来源：关联的 stock.picking
--
-- 先看影响范围（dry-run）：
SELECT st.id,
       st.tracking_number   AS old_tn,
       st.carrier           AS old_carrier,
       st.carrier_tracking_url AS old_url,
       sp.x_studio_wms_carrier AS picking_carrier,
       sp.carrier_tracking_ref AS picking_tn_ref,
       sp.x_studio_wms_tracking_url AS picking_url
FROM stock_tracking st
JOIN stock_picking sp ON sp.id = st.picking_id
WHERE st.pod_link IS NOT NULL
  AND st.pod_link != ''
  AND (
      st.carrier IS NULL OR st.carrier = ''
      OR st.carrier_tracking_url IS NULL OR st.carrier_tracking_url = ''
      OR st.tracking_number ~ '/'
  )
ORDER BY st.id;

-- 执行更新：
UPDATE stock_tracking st
SET
    -- carrier: 当前为空则从 picking 补
    carrier = CASE
        WHEN st.carrier IS NULL OR st.carrier = ''
        THEN sp.x_studio_wms_carrier
        ELSE st.carrier
    END,
    -- carrier_tracking_url: 当前为空则从 picking 补
    carrier_tracking_url = CASE
        WHEN st.carrier_tracking_url IS NULL OR st.carrier_tracking_url = ''
        THEN sp.x_studio_wms_tracking_url
        ELSE st.carrier_tracking_url
    END,
    -- tracking_number: 当前包含 '/'（picking_name 格式）则替换为 picking 的第一个真实单号
    tracking_number = CASE
        WHEN st.tracking_number ~ '/'
             AND sp.carrier_tracking_ref IS NOT NULL
             AND sp.carrier_tracking_ref != ''
        THEN SPLIT_PART(sp.carrier_tracking_ref, ',', 1)
        ELSE st.tracking_number
    END
FROM stock_picking sp
WHERE st.picking_id = sp.id
  AND st.pod_link IS NOT NULL
  AND st.pod_link != ''
  AND (
      st.carrier IS NULL OR st.carrier = ''
      OR st.carrier_tracking_url IS NULL OR st.carrier_tracking_url = ''
      OR (st.tracking_number ~ '/' AND sp.carrier_tracking_ref IS NOT NULL AND sp.carrier_tracking_ref != '')
  );
