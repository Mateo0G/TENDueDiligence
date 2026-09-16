from psycopg_pool import ConnectionPool

from app.config import DATABASE_URL

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=True)
