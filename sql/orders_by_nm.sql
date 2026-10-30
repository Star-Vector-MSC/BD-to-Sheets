WITH fbs AS MATERIALIZED (
 SELECT t.date,t.article_id AS nm_id,a.local_vendor_code AS wild,COUNT(DISTINCT t.task_id) AS fbs_qnt
 FROM public.assembly_task t JOIN public.article a ON a.nm_id=t.article_id
 WHERE t.date >= %(start)s::date AND t.date < %(end)s::date
 AND EXISTS (SELECT 1 FROM public.status_assembly_task s WHERE s.assembly_task_id=t.task_id
 AND s.wb_status NOT IN ('declined_by_client','canceled','canceled_by_carrier','canceled_by_client'))
 GROUP BY 1,2,3
), chosen AS (SELECT DISTINCT date,LOWER(TRIM(wild)) AS wild FROM fbs),
funnel AS MATERIALIZED (
 SELECT fd.date,fd.nm_id,SUM(fd.order_count) AS qty FROM public.funnel_daily fd
 WHERE fd.date >= %(start)s::date AND fd.date < %(end)s::date GROUP BY 1,2
), candidates AS (
 SELECT date,nm_id FROM fbs UNION
 SELECT fd.date,fd.nm_id FROM public.funnel_daily fd
 JOIN chosen ch ON ch.date=fd.date AND ch.wild=LOWER(TRIM(fd.wild))
 WHERE fd.date >= %(start)s::date AND fd.date < %(end)s::date
 GROUP BY 1,2 HAVING SUM(fd.order_count)<>0
), mapping AS MATERIALIZED (
 SELECT nm_id,MIN(local_vendor_code) AS wild,COUNT(DISTINCT local_vendor_code) AS count_wild
 FROM public.article WHERE NULLIF(TRIM(local_vendor_code),'') IS NOT NULL GROUP BY nm_id
)
SELECT c.date,c.nm_id,m.wild,COALESCE(fbs.fbs_qnt,0),
 GREATEST(COALESCE(fn.qty,0)-COALESCE(fbs.fbs_qnt,0),0),COALESCE(fn.qty,0),m.count_wild
FROM candidates c LEFT JOIN mapping m USING(nm_id)
LEFT JOIN fbs ON fbs.date=c.date AND fbs.nm_id=c.nm_id
LEFT JOIN funnel fn ON fn.date=c.date AND fn.nm_id=c.nm_id
ORDER BY c.date DESC,c.nm_id
