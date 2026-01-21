#!/bin/bash
# GitHub推送脚本
# 使用方法：将YOUR_TOKEN替换为你的GitHub Personal Access Token

# 方法1：使用Token推送（临时使用）
# git push https://YOUR_TOKEN@github.com/WW-shan/fundingRate.git main

# 方法2：配置凭证存储后推送
# git config credential.helper store
# git push -u origin main
# 然后输入 username: ww
# 然后输入 password: YOUR_TOKEN

# 方法3：使用SSH（推荐）
# git remote set-url origin git@github.com:WW-shan/fundingRate.git
# git push -u origin main

echo "请选择一种方法推送到GitHub"
echo "详细说明请查看 README.md"
