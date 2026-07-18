-- Secondary indexes applied AFTER bulk load (faster load, then index). The
-- primary keys above already index the point-lookup columns; these cover the
-- join/filter columns the TPC-H analytical queries hit, so heavy joins are
-- index-eligible rather than forced seq-scans on every table.

CREATE INDEX IF NOT EXISTS idx_nation_regionkey   ON nation   (n_regionkey);
CREATE INDEX IF NOT EXISTS idx_supplier_nationkey ON supplier (s_nationkey);
CREATE INDEX IF NOT EXISTS idx_customer_nationkey ON customer (c_nationkey);
CREATE INDEX IF NOT EXISTS idx_orders_custkey     ON orders   (o_custkey);
CREATE INDEX IF NOT EXISTS idx_orders_orderdate   ON orders   (o_orderdate);
CREATE INDEX IF NOT EXISTS idx_lineitem_orderkey  ON lineitem (l_orderkey);
CREATE INDEX IF NOT EXISTS idx_lineitem_suppkey   ON lineitem (l_suppkey);
CREATE INDEX IF NOT EXISTS idx_lineitem_partkey   ON lineitem (l_partkey);
CREATE INDEX IF NOT EXISTS idx_lineitem_shipdate  ON lineitem (l_shipdate);
CREATE INDEX IF NOT EXISTS idx_partsupp_suppkey   ON partsupp (ps_suppkey);
