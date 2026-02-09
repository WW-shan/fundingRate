# Strategy3 Trailing Stop Take-Profit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add dynamic trailing stop take-profit to Strategy 3 (directional funding rate) so that profits are locked in after price moves favorably.

**Architecture:** When a directional position's unrealized PnL reaches an activation threshold (default 4%), the system begins tracking the best price (lowest for shorts, highest for longs). If price retraces from the best price by more than the callback threshold (default 4%), the position is closed for profit. Exit priority: stop-loss > funding rate reversal > trailing stop.

**Tech Stack:** Python 3.10, SQLite, existing project modules (ConfigManager, DatabaseManager, StrategyExecutor)

---

### Task 1: Database Schema — Add trailing stop columns to positions table

**Files:**
- Modify: `database/db_manager.py:231-248` (positions table CREATE)

**Step 1: Add three new columns to the positions table schema**

In `database/db_manager.py`, update the `CREATE TABLE IF NOT EXISTS positions` statement to add trailing stop fields:

```python
            # 持仓表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_type VARCHAR(50),
                    symbol VARCHAR(20),
                    exchanges TEXT,
                    entry_details TEXT,
                    position_size DECIMAL(18,2),
                    current_pnl DECIMAL(18,2),
                    realized_pnl DECIMAL(18,2),
                    funding_collected DECIMAL(18,2),
                    fees_paid DECIMAL(18,2),
                    status VARCHAR(20),
                    open_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    close_time TIMESTAMP,
                    trailing_stop_activated BOOLEAN DEFAULT FALSE,
                    best_price DECIMAL(20,8) DEFAULT NULL,
                    activation_price DECIMAL(20,8) DEFAULT NULL
                )
            """)
```

**Step 2: Add migration logic for existing databases**

After the `CREATE TABLE` block (around line 297, before `conn.commit()`), add migration logic to add columns if they don't exist:

```python
            # 迁移：为 positions 表添加 trailing stop 字段
            try:
                cursor.execute("ALTER TABLE positions ADD COLUMN trailing_stop_activated BOOLEAN DEFAULT FALSE")
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                cursor.execute("ALTER TABLE positions ADD COLUMN best_price DECIMAL(20,8) DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE positions ADD COLUMN activation_price DECIMAL(20,8) DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
```

**Step 3: Commit**

```bash
git add database/db_manager.py
git commit -m "feat(db): add trailing stop columns to positions table"
```

---

### Task 2: Database Schema — Add trailing stop config columns to trading_pair_configs table

**Files:**
- Modify: `database/db_manager.py:103-143` (trading_pair_configs CREATE)

**Step 1: Add three new columns to trading_pair_configs**

Add these columns after line 134 (`s3_long_exit_threshold`):

```python
                    s3_trailing_stop_enabled BOOLEAN DEFAULT TRUE,
                    s3_trailing_activation_pct DECIMAL(10,4) DEFAULT 0.04,
                    s3_trailing_callback_pct DECIMAL(10,4) DEFAULT 0.04,
```

**Step 2: Add migration logic for existing databases**

Add migration alongside Task 1's migration block:

```python
            # 迁移：为 trading_pair_configs 表添加 trailing stop 配置字段
            try:
                cursor.execute("ALTER TABLE trading_pair_configs ADD COLUMN s3_trailing_stop_enabled BOOLEAN DEFAULT TRUE")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE trading_pair_configs ADD COLUMN s3_trailing_activation_pct DECIMAL(10,4) DEFAULT 0.04")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE trading_pair_configs ADD COLUMN s3_trailing_callback_pct DECIMAL(10,4) DEFAULT 0.04")
            except sqlite3.OperationalError:
                pass
```

**Step 3: Commit**

```bash
git add database/db_manager.py
git commit -m "feat(db): add trailing stop config columns to trading_pair_configs"
```

---

### Task 3: Config — Add trailing stop defaults to ConfigManager

**Files:**
- Modify: `config/config_manager.py:108-137` (_get_default_pair_config method)
- Modify: `config/config_manager.py:173-180` (init_default_configs method)

**Step 1: Add defaults to `_get_default_pair_config`**

After line 133 (`'s3_long_exit_threshold'`), add:

```python
            's3_trailing_stop_enabled': self.get('strategy3', 'trailing_stop_enabled', True),
            's3_trailing_activation_pct': self.get('strategy3', 'trailing_activation_pct', 0.04),
            's3_trailing_callback_pct': self.get('strategy3', 'trailing_callback_pct', 0.04),
```

**Step 2: Add default config entries to `init_default_configs`**

After line 180 (`long_exit_threshold`), add:

```python
        self.set_default('strategy3', 'trailing_stop_enabled', True, True, "是否启用动态追踪止盈")
        self.set_default('strategy3', 'trailing_activation_pct', 0.04, True, "追踪止盈启动阈值（4%盈利）")
        self.set_default('strategy3', 'trailing_callback_pct', 0.04, True, "追踪止盈回撤阈值（4%回撤）")
```

**Step 3: Commit**

```bash
git add config/config_manager.py
git commit -m "feat(config): add trailing stop configuration defaults"
```

---

### Task 4: Core Logic — Implement trailing stop in `_check_directional_position`

**Files:**
- Modify: `core/strategy_executor.py:854-946` (_check_directional_position method)

**Step 1: Read trailing stop config**

After line 867 (`long_exit_threshold`), add config reads:

```python
            trailing_stop_enabled = pair_config.get('s3_trailing_stop_enabled', True)
            trailing_activation_pct = float(pair_config.get('s3_trailing_activation_pct', 0.04))
            trailing_callback_pct = float(pair_config.get('s3_trailing_callback_pct', 0.04))
```

**Step 2: Read trailing state from position**

After line 897 (`entry_price`), add:

```python
            trailing_activated = position.get('trailing_stop_activated', False)
            best_price = position.get('best_price')
            if best_price is not None:
                best_price = float(best_price)
```

**Step 3: Add trailing stop logic after the funding rate exit block**

Replace lines 938-943 (the `if should_close:` block) with:

```python
            if should_close:
                self.close_position(position_id)
                self._trigger_callback('strategy_exit', {
                    'position_id': position_id,
                    'message': f"费率条件触发平仓: {symbol} 费率 {current_funding_rate}"
                })
                return

            # 4. Trailing Stop 逻辑
            if not trailing_stop_enabled:
                return

            if not trailing_activated:
                # 未启动：检查是否达到启动条件
                if pnl_pct >= trailing_activation_pct:
                    logger.info(f"Trailing stop activated for position #{position_id}: PnL {pnl_pct:.2%} >= {trailing_activation_pct:.2%}")
                    self.db.execute_update(
                        "UPDATE positions SET trailing_stop_activated = TRUE, best_price = ?, activation_price = ? WHERE id = ?",
                        (current_price, current_price, position_id)
                    )
                    self._trigger_callback('trailing_stop', {
                        'position_id': position_id,
                        'message': f"🔔 追踪止盈已启动: {symbol} 盈利 {pnl_pct:.2%}, 当前价 {current_price}"
                    })
            else:
                # 已启动：更新best_price并检查回撤
                should_update = False
                if direction == 'short':
                    # 做空：追踪最低价
                    if best_price is None or current_price < best_price:
                        best_price = current_price
                        should_update = True
                else:
                    # 做多：追踪最高价
                    if best_price is None or current_price > best_price:
                        best_price = current_price
                        should_update = True

                if should_update:
                    self.db.execute_update(
                        "UPDATE positions SET best_price = ? WHERE id = ?",
                        (best_price, position_id)
                    )

                # 检查回撤止盈
                should_take_profit = False
                if direction == 'short' and best_price is not None:
                    # 做空：价格从最低点反弹超过阈值
                    retracement = (current_price - best_price) / best_price
                    if retracement >= trailing_callback_pct:
                        should_take_profit = True
                elif direction == 'long' and best_price is not None:
                    # 做多：价格从最高点回落超过阈值
                    retracement = (best_price - current_price) / best_price
                    if retracement >= trailing_callback_pct:
                        should_take_profit = True

                if should_take_profit:
                    logger.info(f"Trailing stop take-profit for position #{position_id}: retracement {retracement:.2%}")
                    self.close_position(position_id)
                    self._trigger_callback('trailing_stop', {
                        'position_id': position_id,
                        'message': f"📈 追踪止盈平仓: {symbol} 方向 {direction}, 入场价 {entry_price}, 最优价 {best_price}, 平仓价 {current_price}, 回撤 {retracement:.2%}"
                    })
```

**Step 4: Commit**

```bash
git add core/strategy_executor.py
git commit -m "feat(strategy3): implement trailing stop take-profit logic"
```

---

### Task 5: Verify — Manual smoke test

**Step 1: Start the application and verify DB migration**

```bash
cd /project/fundingRate
source venv/bin/activate
python -c "
from database.db_manager import DatabaseManager
db = DatabaseManager('data/database.db')
db.init_database()
# Verify columns exist
result = db.execute_query('PRAGMA table_info(positions)')
cols = [r['name'] for r in result]
assert 'trailing_stop_activated' in cols, 'Missing trailing_stop_activated'
assert 'best_price' in cols, 'Missing best_price'
assert 'activation_price' in cols, 'Missing activation_price'
print('✅ positions table migration OK')

result = db.execute_query('PRAGMA table_info(trading_pair_configs)')
cols = [r['name'] for r in result]
assert 's3_trailing_stop_enabled' in cols, 'Missing s3_trailing_stop_enabled'
assert 's3_trailing_activation_pct' in cols, 'Missing s3_trailing_activation_pct'
assert 's3_trailing_callback_pct' in cols, 'Missing s3_trailing_callback_pct'
print('✅ trading_pair_configs table migration OK')
"
```

Expected: Both `✅` messages printed.

**Step 2: Verify config defaults load**

```bash
python -c "
from database.db_manager import DatabaseManager
from config.config_manager import ConfigManager
db = DatabaseManager('data/database.db')
db.init_database()
config = ConfigManager(db)
config.init_default_configs()
pair = config._get_default_pair_config('BTCUSDT', 'binance')
assert pair['s3_trailing_stop_enabled'] == True
assert pair['s3_trailing_activation_pct'] == 0.04
assert pair['s3_trailing_callback_pct'] == 0.04
print('✅ Config defaults OK')
"
```

Expected: `✅ Config defaults OK`

**Step 3: Commit verification script (optional) and final commit**

```bash
git add -A
git commit -m "feat(strategy3): complete trailing stop take-profit implementation"
```

---

## Summary of Changes

| File | Change |
|------|--------|
| `database/db_manager.py` | Add 3 columns to `positions`, 3 columns to `trading_pair_configs`, migration logic |
| `config/config_manager.py` | Add 3 trailing stop defaults to `_get_default_pair_config` and `init_default_configs` |
| `core/strategy_executor.py` | Add trailing stop activation, best_price tracking, and retracement take-profit logic to `_check_directional_position` |

## Exit Priority (preserved)

1. **止损** — PnL <= -5% → immediate close
2. **资金费率反转** — funding rate crosses threshold → immediate close
3. **Trailing Stop** — retracement from best price >= 4% → take-profit close
