"""
日志配置
"""
import sys
from loguru import logger
import os


def setup_logger(log_file: str = "logs/app.log", log_level: str = "INFO"):
    """配置日志系统"""
    # 确保日志目录存在
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # 移除默认handler
    logger.remove()

    # 添加控制台输出
    logger.add(
        sys.stdout,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level=log_level
    )

    # 添加文件输出
    logger.add(
        log_file,
        rotation="100 MB",  # 文件大小达到100MB时轮转
        retention="30 days",  # 保留30天
        compression="zip",  # 压缩旧日志
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} - {message}",
        level=log_level
    )

    logger.info("Logger initialized")
    return logger
