-- Процент брака по казанам: wild, продано (фин отчёт), брак (goods_returns_dev),
-- принято складом по поступлениям от поставщика (incoming_items, без incoming_returns)
-- за окно [start, end) по Москве.
WITH amap AS (
 SELECT nm_id, MIN(LOWER(TRIM(local_vendor_code))) AS wild,
        COUNT(DISTINCT LOWER(TRIM(local_vendor_code))) AS wilds
 FROM public.article WHERE NULLIF(TRIM(local_vendor_code), '') IS NOT NULL GROUP BY nm_id
), sold AS (
 SELECT nm_id, SUM(quantity) AS qty FROM public.daily_fin_reports_full
 WHERE subject_name = 'Казаны' AND supplier_oper_name = 'Продажа' AND doc_type_name = 'Продажа'
   AND sale_dt >= %(start)s::timestamp AT TIME ZONE 'Europe/Moscow'
   AND sale_dt <  %(end)s::timestamp AT TIME ZONE 'Europe/Moscow'
 GROUP BY nm_id
), ret AS (
 SELECT nm_id, COUNT(DISTINCT srid) AS qty FROM public.goods_returns_dev
 WHERE subject_name = 'Казаны' AND return_type = 'Возврат брака'
   AND created_at >= %(start)s::timestamp AT TIME ZONE 'Europe/Moscow'
   AND created_at <  %(end)s::timestamp AT TIME ZONE 'Europe/Moscow'
 GROUP BY nm_id
), per_wild AS (
 SELECT m.wild, COALESCE(SUM(s.qty), 0) AS sold, COALESCE(SUM(r.qty), 0) AS defect, MAX(m.wilds) AS wilds
 FROM amap m LEFT JOIN sold s USING (nm_id) LEFT JOIN ret r USING (nm_id)
 WHERE s.nm_id IS NOT NULL OR r.nm_id IS NOT NULL
 GROUP BY m.wild
), recv AS (
 SELECT LOWER(TRIM(i.product_id)) AS wild, SUM(i.quantity) AS qty
 FROM public.incoming_items i JOIN public.incoming_documents d ON d.guid = i.guid
 WHERE i.is_valid AND d.doc_date >= %(start)s::date AND d.doc_date < %(end)s::date
 GROUP BY 1
)
SELECT p.wild, p.sold, p.defect, COALESCE(v.qty, 0) AS received, p.wilds
FROM per_wild p LEFT JOIN recv v ON v.wild = p.wild ORDER BY p.wild
