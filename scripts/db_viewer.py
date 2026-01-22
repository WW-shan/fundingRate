#!/usr/bin/env python3
"""
SQLite数据库Web查看器
类似phpMyAdmin的简单界面
"""
import sqlite3
from flask import Flask, render_template_string, request, jsonify
import os

app = Flask(__name__)

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'database.db')

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>SQLite数据库查看器</title>
    <meta charset="utf-8">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f5f5;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { color: #333; margin-bottom: 20px; }
        .sidebar {
            background: white;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .sidebar h2 { font-size: 16px; margin-bottom: 15px; color: #666; }
        .table-list {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }
        .table-btn {
            padding: 8px 16px;
            background: #007bff;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: background 0.2s;
        }
        .table-btn:hover { background: #0056b3; }
        .table-btn.active { background: #28a745; }

        .content {
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .query-section {
            margin-bottom: 20px;
            padding-bottom: 20px;
            border-bottom: 1px solid #eee;
        }
        textarea {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-family: monospace;
            font-size: 14px;
            resize: vertical;
        }
        .btn {
            padding: 10px 20px;
            background: #007bff;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            margin-top: 10px;
        }
        .btn:hover { background: #0056b3; }

        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }
        th {
            background: #f8f9fa;
            font-weight: 600;
            color: #333;
        }
        tr:hover { background: #f8f9fa; }

        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 6px;
            border-left: 4px solid #007bff;
        }
        .stat-card h3 { font-size: 14px; color: #666; margin-bottom: 5px; }
        .stat-card p { font-size: 24px; font-weight: bold; color: #333; }

        .error {
            background: #f8d7da;
            color: #721c24;
            padding: 12px;
            border-radius: 4px;
            margin-top: 10px;
        }
        .success {
            background: #d4edda;
            color: #155724;
            padding: 12px;
            border-radius: 4px;
            margin-top: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 SQLite数据库查看器</h1>

        <div class="sidebar">
            <h2>数据表列表</h2>
            <div class="table-list" id="tableList"></div>
        </div>

        <div class="content">
            <div class="stats" id="stats"></div>

            <div class="query-section">
                <h2>SQL查询</h2>
                <textarea id="sqlQuery" rows="5" placeholder="输入SQL查询...例如: SELECT * FROM funding_rates LIMIT 100"></textarea>
                <button class="btn" onclick="executeQuery()">执行查询</button>
            </div>

            <div id="results"></div>
        </div>
    </div>

    <script>
        let currentTable = null;

        async function loadTables() {
            const response = await fetch('/api/tables');
            const tables = await response.json();

            const tableList = document.getElementById('tableList');
            tableList.innerHTML = tables.map(table =>
                `<button class="table-btn" onclick="loadTable('${table}')">${table}</button>`
            ).join('');
        }

        async function loadTable(tableName) {
            currentTable = tableName;

            // 更新按钮状态
            document.querySelectorAll('.table-btn').forEach(btn => {
                btn.classList.toggle('active', btn.textContent === tableName);
            });

            // 获取表统计
            const statsResponse = await fetch(`/api/table/${tableName}/stats`);
            const stats = await statsResponse.json();

            const statsDiv = document.getElementById('stats');
            statsDiv.innerHTML = `
                <div class="stat-card">
                    <h3>表名</h3>
                    <p>${stats.table_name}</p>
                </div>
                <div class="stat-card">
                    <h3>总记录数</h3>
                    <p>${stats.row_count.toLocaleString()}</p>
                </div>
                <div class="stat-card">
                    <h3>列数</h3>
                    <p>${stats.column_count}</p>
                </div>
            `;

            // 加载表数据
            document.getElementById('sqlQuery').value = `SELECT * FROM ${tableName} LIMIT 100`;
            executeQuery();
        }

        async function executeQuery() {
            const query = document.getElementById('sqlQuery').value;
            const resultsDiv = document.getElementById('results');

            try {
                const response = await fetch('/api/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query })
                });

                const data = await response.json();

                if (data.error) {
                    resultsDiv.innerHTML = `<div class="error">${data.error}</div>`;
                    return;
                }

                if (data.rows.length === 0) {
                    resultsDiv.innerHTML = '<div class="success">查询成功，但没有返回数据</div>';
                    return;
                }

                // 生成表格
                const columns = data.columns;
                const rows = data.rows;

                let html = `<div class="success">返回 ${rows.length} 条记录</div>`;
                html += '<table><thead><tr>';
                columns.forEach(col => {
                    html += `<th>${col}</th>`;
                });
                html += '</tr></thead><tbody>';

                rows.forEach(row => {
                    html += '<tr>';
                    row.forEach(cell => {
                        html += `<td>${cell !== null ? cell : '<em>NULL</em>'}</td>`;
                    });
                    html += '</tr>';
                });

                html += '</tbody></table>';
                resultsDiv.innerHTML = html;

            } catch (error) {
                resultsDiv.innerHTML = `<div class="error">错误: ${error.message}</div>`;
            }
        }

        // 初始化
        loadTables();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/tables')
def get_tables():
    """获取所有表名"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    return jsonify(tables)

@app.route('/api/table/<table_name>/stats')
def get_table_stats(table_name):
    """获取表统计信息"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 获取行数
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    row_count = cursor.fetchone()[0]

    # 获取列信息
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = cursor.fetchall()

    conn.close()

    return jsonify({
        'table_name': table_name,
        'row_count': row_count,
        'column_count': len(columns)
    })

@app.route('/api/query', methods=['POST'])
def execute_query():
    """执行SQL查询"""
    query = request.json.get('query', '')

    # 安全检查：只允许SELECT查询
    if not query.strip().upper().startswith('SELECT'):
        return jsonify({'error': '只允许执行SELECT查询'})

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(query)

        # 获取列名
        columns = [description[0] for description in cursor.description] if cursor.description else []

        # 获取数据
        rows = cursor.fetchall()

        conn.close()

        return jsonify({
            'columns': columns,
            'rows': rows
        })
    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    print(f"数据库路径: {DB_PATH}")
    print("正在启动Web服务器...")
    print("请在浏览器中访问: http://localhost:5001")
    app.run(host='0.0.0.0', port=5001, debug=True)
