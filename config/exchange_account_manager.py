"""
交易所账户管理器
支持账户缓存和热更新
支持敏感数据加密存储
"""
from typing import Dict, List, Any, Optional
from loguru import logger
from database.db_manager import DatabaseManager
from utils.crypto_utils import get_crypto_manager


class ExchangeAccountManager:
    """交易所账户管理器"""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.crypto = get_crypto_manager()  # 加密管理器
        self._accounts_cache = {}  # 账户缓存 {exchange_name: account_info}
        self._load_all_accounts()

    def _load_all_accounts(self):
        """从数据库加载所有激活的账户到缓存（自动解密）"""
        logger.info("Loading exchange accounts...")
        accounts = self.db.execute_query(
            "SELECT * FROM exchange_accounts WHERE is_active = TRUE"
        )
        
        self._accounts_cache.clear()
        for acc in accounts:
            exchange_name = acc['exchange_name'].lower()
            # 解密敏感字段
            try:
                decrypted_key = self.crypto.decrypt(acc['api_key'])
                decrypted_secret = self.crypto.decrypt(acc['api_secret'])
                decrypted_passphrase = self.crypto.decrypt(acc.get('passphrase', '')) if acc.get('passphrase') else None
                
                self._accounts_cache[exchange_name] = {
                    'api_key': decrypted_key,
                    'api_secret': decrypted_secret,
                    'passphrase': decrypted_passphrase,
                    'is_active': acc['is_active']
                }
            except Exception as e:
                logger.error(f"Failed to decrypt account {exchange_name}: {e}")
                # 如果解密失败，尝试作为明文（兼容旧数据）
                self._accounts_cache[exchange_name] = {
                    'api_key': acc['api_key'],
                    'api_secret': acc['api_secret'],
                    'passphrase': acc.get('passphrase'),
                    'is_active': acc['is_active']
                }
        
        logger.info(f"Loaded {len(self._accounts_cache)} exchange accounts: {list(self._accounts_cache.keys())}")

    def get_account(self, exchange_name: str) -> Optional[Dict[str, Any]]:
        """获取交易所账户信息"""
        return self._accounts_cache.get(exchange_name.lower())

    def get_all_accounts(self) -> Dict[str, Dict[str, Any]]:
        """获取所有激活的账户"""
        return self._accounts_cache.copy()

    def has_account(self, exchange_name: str) -> bool:
        """检查是否有该交易所账户"""
        return exchange_name.lower() in self._accounts_cache

    def add_account(self, exchange_name: str, api_key: str, api_secret: str,
                   passphrase: Optional[str] = None) -> bool:
        """添加或更新账户（自动加密）"""
        try:
            exchange_name = exchange_name.lower()
            
            # 加密敏感字段
            encrypted_key = self.crypto.encrypt(api_key)
            encrypted_secret = self.crypto.encrypt(api_secret)
            encrypted_passphrase = self.crypto.encrypt(passphrase) if passphrase else None
            
            # 更新数据库（存储加密后的数据）
            self.db.execute_query(
                """
                INSERT INTO exchange_accounts (exchange_name, api_key, api_secret, passphrase, is_active)
                VALUES (?, ?, ?, ?, TRUE)
                ON CONFLICT(exchange_name) 
                DO UPDATE SET api_key=?, api_secret=?, passphrase=?, is_active=TRUE
                """,
                (exchange_name, encrypted_key, encrypted_secret, encrypted_passphrase,
                 encrypted_key, encrypted_secret, encrypted_passphrase)
            )
            
            # 更新缓存（存储明文，供程序使用）
            self._accounts_cache[exchange_name] = {
                'api_key': api_key,
                'api_secret': api_secret,
                'passphrase': passphrase,
                'is_active': True
            }
            
            logger.info(f"Account added/updated (encrypted): {exchange_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding account {exchange_name}: {e}")
            return False

    def remove_account(self, exchange_name: str) -> bool:
        """删除账户"""
        try:
            exchange_name = exchange_name.lower()
            
            # 从数据库删除
            self.db.execute_query(
                "DELETE FROM exchange_accounts WHERE exchange_name = ?",
                (exchange_name,)
            )
            
            # 从缓存移除
            if exchange_name in self._accounts_cache:
                del self._accounts_cache[exchange_name]
            
            logger.info(f"Account removed: {exchange_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error removing account {exchange_name}: {e}")
            return False

    def deactivate_account(self, exchange_name: str) -> bool:
        """停用账户（不删除）"""
        try:
            exchange_name = exchange_name.lower()
            
            # 更新数据库
            self.db.execute_query(
                "UPDATE exchange_accounts SET is_active = FALSE WHERE exchange_name = ?",
                (exchange_name,)
            )
            
            # 从缓存移除
            if exchange_name in self._accounts_cache:
                del self._accounts_cache[exchange_name]
            
            logger.info(f"Account deactivated: {exchange_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error deactivating account {exchange_name}: {e}")
            return False

    def reload_accounts(self):
        """重新加载所有账户（用于热更新）"""
        logger.info("Reloading exchange accounts...")
        self._load_all_accounts()

    def get_account_count(self) -> int:
        """获取激活账户数量"""
        return len(self._accounts_cache)
