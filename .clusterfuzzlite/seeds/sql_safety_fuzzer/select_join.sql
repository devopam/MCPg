SELECT a, count(*) FROM t JOIN u ON t.id = u.id WHERE x IN (1, 2) GROUP BY a ORDER BY 1 LIMIT 10
