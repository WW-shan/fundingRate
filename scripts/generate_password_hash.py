#!/usr/bin/env python3
"""
密码哈希生成工具
用于生成Web界面登录密码的哈希值
"""

import sys
from werkzeug.security import generate_password_hash

def main():
    print("=" * 50)
    print("Web界面密码哈希生成工具")
    print("=" * 50)
    print()

    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = input("请输入要设置的密码: ")

    if not password:
        print("错误: 密码不能为空")
        sys.exit(1)

    password_hash = generate_password_hash(password)

    print()
    print("生成的密码哈希:")
    print("-" * 50)
    print(password_hash)
    print("-" * 50)
    print()
    print("请将上述哈希值添加到 .env 文件中:")
    print(f"WEB_PASSWORD_HASH={password_hash}")
    print()

if __name__ == "__main__":
    main()
