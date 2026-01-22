"""
加密工具类
用于敏感数据的加密和解密
"""
import os
from cryptography.fernet import Fernet
from loguru import logger
from typing import Optional


class CryptoManager:
    """加密管理器 - 使用 Fernet 对称加密"""

    def __init__(self, key_file: str = "data/.encryption_key"):
        """
        初始化加密管理器
        
        Args:
            key_file: 加密密钥文件路径
        """
        self.key_file = key_file
        self._cipher = None
        self._initialize_cipher()

    def _initialize_cipher(self):
        """初始化加密器"""
        # 确保数据目录存在
        os.makedirs(os.path.dirname(self.key_file), exist_ok=True)
        
        # 加载或生成密钥
        if os.path.exists(self.key_file):
            with open(self.key_file, 'rb') as f:
                key = f.read()
            logger.info("Loaded existing encryption key")
        else:
            key = Fernet.generate_key()
            with open(self.key_file, 'wb') as f:
                f.write(key)
            # 设置文件权限（仅所有者可读写）
            os.chmod(self.key_file, 0o600)
            logger.warning(f"Generated new encryption key: {self.key_file}")
            logger.warning("⚠️ 请妥善保管此密钥文件，丢失将无法解密已有数据！")
        
        self._cipher = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """
        加密字符串
        
        Args:
            plaintext: 明文
            
        Returns:
            加密后的字符串（Base64编码）
        """
        if not plaintext:
            return ""
        
        try:
            encrypted_bytes = self._cipher.encrypt(plaintext.encode('utf-8'))
            return encrypted_bytes.decode('utf-8')
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise

    def decrypt(self, ciphertext: str) -> str:
        """
        解密字符串
        
        Args:
            ciphertext: 密文（Base64编码）
            
        Returns:
            解密后的明文
        """
        if not ciphertext:
            return ""
        
        try:
            decrypted_bytes = self._cipher.decrypt(ciphertext.encode('utf-8'))
            return decrypted_bytes.decode('utf-8')
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise

    def encrypt_dict(self, data: dict, fields: list) -> dict:
        """
        加密字典中的指定字段
        
        Args:
            data: 原始数据字典
            fields: 需要加密的字段列表
            
        Returns:
            加密后的数据字典（副本）
        """
        encrypted_data = data.copy()
        for field in fields:
            if field in encrypted_data and encrypted_data[field]:
                encrypted_data[field] = self.encrypt(encrypted_data[field])
        return encrypted_data

    def decrypt_dict(self, data: dict, fields: list) -> dict:
        """
        解密字典中的指定字段
        
        Args:
            data: 加密的数据字典
            fields: 需要解密的字段列表
            
        Returns:
            解密后的数据字典（副本）
        """
        decrypted_data = data.copy()
        for field in fields:
            if field in decrypted_data and decrypted_data[field]:
                try:
                    decrypted_data[field] = self.decrypt(decrypted_data[field])
                except Exception as e:
                    # 如果解密失败，可能是旧的明文数据，保持原值
                    logger.warning(f"Failed to decrypt field '{field}', keeping original value")
        return decrypted_data


# 全局加密管理器实例
_crypto_manager: Optional[CryptoManager] = None


def get_crypto_manager() -> CryptoManager:
    """获取全局加密管理器实例"""
    global _crypto_manager
    if _crypto_manager is None:
        _crypto_manager = CryptoManager()
    return _crypto_manager
