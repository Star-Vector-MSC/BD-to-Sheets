WITH fbs AS (
 SELECT t.date,a.local_vendor_code AS wild,COUNT(DISTINCT t.task_id) AS fbs_qnt
 FROM public.assembly_task t JOIN public.article a ON a.nm_id=t.article_id
 WHERE t.date >= %(start)s::date AND t.date < %(end)s::date
 AND EXISTS(SELECT 1 FROM public.status_assembly_task s WHERE s.assembly_task_id=t.task_id
 AND s.wb_status NOT IN ('declined_by_client','canceled','canceled_by_carrier','canceled_by_client'))
 GROUP BY 1,2
), funnel AS (
 SELECT date,wild,SUM(order_count) AS qty FROM public.funnel_daily
 WHERE date >= %(start)s::date AND date < %(end)s::date GROUP BY 1,2
)
SELECT fbs.date,fbs.wild,fbs.fbs_qnt,GREATEST(COALESCE(funnel.qty,0)-fbs.fbs_qnt,0),COALESCE(funnel.qty,0)
FROM fbs LEFT JOIN funnel ON funnel.date=fbs.date AND LOWER(TRIM(funnel.wild))=LOWER(TRIM(fbs.wild))
