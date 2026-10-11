SELECT CASE WHEN a > 1 THEN 'x' ELSE coalesce(b, 'y') END, a::text, ARRAY[1,2,3] FROM t
