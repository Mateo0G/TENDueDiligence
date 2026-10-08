from psycopg_pool import ConnectionPool

from app.config import DATABASE_URL

# check=check_connection: validate a connection (a cheap round-trip) before
# handing it out, rather than handing out one that looks fine in the pool's
# own bookkeeping but whose underlying socket already died -- observed in
# practice during Phase 7's Batch API polling loops, where several minutes
# can pass with no Postgres traffic while waiting on Anthropic; over a local
# SSH tunnel (dev only -- Phase 8's in-network deployment won't go through
# one) that idle gap is long enough to trip the tunnel's own idle timeout.
pool = ConnectionPool(
    # max_size covers the web service's own request handling plus the
    # worker's concurrent task slots (app/worker.py's WORKER_CONCURRENCY,
    # default 6) -- each only holds a connection for one short query, not for
    # the lifetime of a task, but enough slots must fit at once to avoid
    # queuing behind the pool itself.
    DATABASE_URL, min_size=1, max_size=10, open=True, check=ConnectionPool.check_connection
)
