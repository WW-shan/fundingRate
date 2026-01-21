"""
数据库连接池和缓存优化
"""

import sqlite3
from contextlib import contextmanager
from threading import Lock
from collections import OrderedDict
import time
from loguru import logger


class ConnectionPool:
    """SQLite连接池"""

    def __init__(self, database_path: str, max_connections: int = 10):
        self.database_path = database_path
        self.max_connections = max_connections
        self.connections = []
        self.lock = Lock()
        self.in_use = set()

    @contextmanager
    def get_connection(self):
        """获取连接"""
        conn = None
        try:
            conn = self._acquire()
            yield conn
        finally:
            if conn:
                self._release(conn)

    def _acquire(self):
        """获取可用连接"""
        with self.lock:
            # 尝试复用现有连接
            for conn in self.connections:
                if conn not in self.in_use:
                    self.in_use.add(conn)
                    return conn

            # 创建新连接
            if len(self.connections) < self.max_connections:
                conn = sqlite3.connect(
                    self.database_path,
                    check_same_thread=False,
                    timeout=30.0
                )
                conn.row_factory = sqlite3.Row
                # 优化设置
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
                conn.execute("PRAGMA temp_store=MEMORY")

                self.connections.append(conn)
                self.in_use.add(conn)
                return conn

            # 等待可用连接
            raise Exception("Connection pool exhausted")

    def _release(self, conn):
        """释放连接"""
        with self.lock:
            self.in_use.discard(conn)

    def close_all(self):
        """关闭所有连接"""
        with self.lock:
            for conn in self.connections:
                try:
                    conn.close()
                except:
                    pass
            self.connections.clear()
            self.in_use.clear()


class QueryCache:
    """查询结果缓存"""

    def __init__(self, max_size: int = 1000, ttl: float = 60.0):
        """
        Args:
            max_size: 最大缓存条目数
            ttl: 缓存过期时间(秒)
        """
        self.max_size = max_size
        self.ttl = ttl
        self.cache = OrderedDict()
        self.lock = Lock()

    def get(self, key: str):
        """获取缓存"""
        with self.lock:
            if key not in self.cache:
                return None

            value, timestamp = self.cache[key]

            # 检查是否过期
            if time.time() - timestamp > self.ttl:
                del self.cache[key]
                return None

            # 移到末尾(LRU)
            self.cache.move_to_end(key)
            return value

    def set(self, key: str, value):
        """设置缓存"""
        with self.lock:
            if key in self.cache:
                del self.cache[key]

            self.cache[key] = (value, time.time())

            # 限制大小
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

    def clear(self):
        """清空缓存"""
        with self.lock:
            self.cache.clear()

    def invalidate(self, pattern: str = None):
        """使缓存失效"""
        with self.lock:
            if pattern:
                # 删除匹配模式的键
                keys_to_delete = [k for k in self.cache.keys() if pattern in k]
                for key in keys_to_delete:
                    del self.cache[key]
            else:
                # 清空所有
                self.cache.clear()


class BatchWriter:
    """批量写入优化器"""

    def __init__(self, db_manager, batch_size: int = 100, flush_interval: float = 5.0):
        """
        Args:
            db_manager: 数据库管理器
            batch_size: 批量大小
            flush_interval: 自动刷新间隔(秒)
        """
        self.db_manager = db_manager
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self.buffer = []
        self.lock = Lock()
        self.last_flush = time.time()

    def add(self, table: str, data: dict):
        """添加待写入数据"""
        with self.lock:
            self.buffer.append((table, data))

            if len(self.buffer) >= self.batch_size or \
               time.time() - self.last_flush > self.flush_interval:
                self._flush()

    def _flush(self):
        """刷新缓冲区"""
        if not self.buffer:
            return

        try:
            # 按表分组
            by_table = {}
            for table, data in self.buffer:
                if table not in by_table:
                    by_table[table] = []
                by_table[table].append(data)

            # 批量插入
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()

                for table, rows in by_table.items():
                    if not rows:
                        continue

                    # 构建批量插入SQL
                    columns = rows[0].keys()
                    placeholders = ','.join(['?' for _ in columns])
                    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"

                    # 批量执行
                    cursor.executemany(sql, [tuple(row.values()) for row in rows])

                conn.commit()

            logger.debug(f"Flushed {len(self.buffer)} records")
            self.buffer.clear()
            self.last_flush = time.time()

        except Exception as e:
            logger.error(f"Error flushing batch: {e}")

    def force_flush(self):
        """强制刷新"""
        with self.lock:
            self._flush()
