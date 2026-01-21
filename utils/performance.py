"""
系统性能监控和错误处理增强
"""

import time
import traceback
from typing import Callable, Any
from functools import wraps
from loguru import logger
import psutil
import os


class PerformanceMonitor:
    """性能监控器"""

    def __init__(self):
        self.metrics = {
            'api_calls': 0,
            'db_queries': 0,
            'errors': 0,
            'warnings': 0
        }
        self.start_time = time.time()

    def track_metric(self, metric_name: str, value: int = 1):
        """追踪指标"""
        if metric_name in self.metrics:
            self.metrics[metric_name] += value
        else:
            self.metrics[metric_name] = value

    def get_system_stats(self) -> dict:
        """获取系统统计信息"""
        process = psutil.Process(os.getpid())

        return {
            'uptime_seconds': int(time.time() - self.start_time),
            'cpu_percent': process.cpu_percent(),
            'memory_mb': process.memory_info().rss / 1024 / 1024,
            'threads': len(process.threads()),
            'metrics': self.metrics.copy()
        }

    def log_stats(self):
        """记录统计信息"""
        stats = self.get_system_stats()
        logger.info(f"System Stats - Uptime: {stats['uptime_seconds']}s, "
                   f"CPU: {stats['cpu_percent']:.1f}%, "
                   f"Memory: {stats['memory_mb']:.1f}MB, "
                   f"Threads: {stats['threads']}")


# 全局性能监控器实例
performance_monitor = PerformanceMonitor()


def retry_on_error(max_retries: int = 3, delay: float = 1.0, exceptions: tuple = (Exception,)):
    """
    错误重试装饰器

    Args:
        max_retries: 最大重试次数
        delay: 重试延迟(秒)
        exceptions: 需要重试的异常类型
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    performance_monitor.track_metric('errors')

                    if attempt < max_retries:
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries + 1} failed for {func.__name__}: {e}. "
                            f"Retrying in {delay}s..."
                        )
                        time.sleep(delay * (attempt + 1))  # 指数退避
                    else:
                        logger.error(
                            f"All {max_retries + 1} attempts failed for {func.__name__}: {e}"
                        )

            raise last_exception

        return wrapper
    return decorator


def log_execution_time(func: Callable) -> Callable:
    """记录函数执行时间装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            execution_time = time.time() - start_time

            if execution_time > 1.0:  # 只记录超过1秒的操作
                logger.debug(f"{func.__name__} took {execution_time:.2f}s")

            return result
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(
                f"{func.__name__} failed after {execution_time:.2f}s: {e}"
            )
            raise

    return wrapper


def safe_execute(func: Callable, *args, default_return=None, **kwargs) -> Any:
    """
    安全执行函数，捕获所有异常

    Args:
        func: 要执行的函数
        default_return: 出错时的默认返回值
        *args, **kwargs: 传递给函数的参数

    Returns:
        函数执行结果，或default_return
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.error(f"Error in {func.__name__}: {e}")
        logger.debug(traceback.format_exc())
        performance_monitor.track_metric('errors')
        return default_return


class CircuitBreaker:
    """熔断器 - 防止系统过载"""

    def __init__(self, failure_threshold: int = 5, timeout: float = 60.0):
        """
        Args:
            failure_threshold: 失败次数阈值
            timeout: 熔断超时时间(秒)
        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = 'closed'  # closed, open, half_open

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """调用函数，带熔断保护"""
        if self.state == 'open':
            if time.time() - self.last_failure_time >= self.timeout:
                self.state = 'half_open'
                logger.info(f"Circuit breaker entering half-open state for {func.__name__}")
            else:
                raise Exception(f"Circuit breaker is OPEN for {func.__name__}")

        try:
            result = func(*args, **kwargs)

            if self.state == 'half_open':
                self.reset()

            return result

        except Exception as e:
            self.record_failure()
            raise e

    def record_failure(self):
        """记录失败"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = 'open'
            logger.error(f"Circuit breaker OPENED after {self.failure_count} failures")

    def reset(self):
        """重置熔断器"""
        self.failure_count = 0
        self.state = 'closed'
        logger.info("Circuit breaker reset to CLOSED state")


class RateLimiter:
    """速率限制器"""

    def __init__(self, max_calls: int, time_window: float):
        """
        Args:
            max_calls: 时间窗口内最大调用次数
            time_window: 时间窗口(秒)
        """
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = []

    def is_allowed(self) -> bool:
        """检查是否允许调用"""
        now = time.time()

        # 清理过期记录
        self.calls = [call_time for call_time in self.calls if now - call_time < self.time_window]

        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True

        return False

    def wait_if_needed(self):
        """如果需要，等待直到可以调用"""
        while not self.is_allowed():
            time.sleep(0.1)


def create_rate_limiter_decorator(max_calls: int, time_window: float):
    """创建速率限制装饰器"""
    limiter = RateLimiter(max_calls, time_window)

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            limiter.wait_if_needed()
            return func(*args, **kwargs)
        return wrapper
    return decorator


class ErrorCollector:
    """错误收集器 - 用于分析系统错误模式"""

    def __init__(self, max_errors: int = 100):
        self.max_errors = max_errors
        self.errors = []

    def collect(self, error: Exception, context: dict = None):
        """收集错误"""
        error_info = {
            'timestamp': time.time(),
            'type': type(error).__name__,
            'message': str(error),
            'traceback': traceback.format_exc(),
            'context': context or {}
        }

        self.errors.append(error_info)

        # 保持列表大小
        if len(self.errors) > self.max_errors:
            self.errors.pop(0)

        performance_monitor.track_metric('errors')

    def get_recent_errors(self, count: int = 10) -> list:
        """获取最近的错误"""
        return self.errors[-count:]

    def get_error_summary(self) -> dict:
        """获取错误摘要"""
        if not self.errors:
            return {'total': 0, 'by_type': {}}

        by_type = {}
        for error in self.errors:
            error_type = error['type']
            by_type[error_type] = by_type.get(error_type, 0) + 1

        return {
            'total': len(self.errors),
            'by_type': by_type,
            'recent': self.get_recent_errors(5)
        }


# 全局错误收集器
error_collector = ErrorCollector()
