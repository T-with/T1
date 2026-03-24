"""MyTradingPlatform Flask App"""
from flask import Flask, render_template, jsonify, request
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path

from engine.core import (
    StrategyConfig, ExchangeClient, BacktestEngine,
    LiveTrader, Indicators, StrategyEngine
)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 状态存储（生产环境应用数据库）
DATA_DIR = Path(__file__).parent / 'data'
DATA_DIR.mkdir(exist_ok=True)
STRATEGIES_FILE = DATA_DIR / 'strategies.json'
EXCHANGE_FILE = DATA_DIR / 'exchange.json'

live_trader = LiveTrader()


def load_strategies() -> dict:
    if STRATEGIES_FILE.exists():
        return json.loads(STRATEGIES_FILE.read_text())
    return {}

def save_strategies(data: dict):
    STRATEGIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def load_exchange() -> dict:
    if EXCHANGE_FILE.exists():
        return json.loads(EXCHANGE_FILE.read_text())
    return {}

def save_exchange(data: dict):
    EXCHANGE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ================================================================
# 页面路由
# ================================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/strategy')
def strategy_page():
    return render_template('strategy.html')

@app.route('/backtest')
def backtest_page():
    return render_template('backtest.html')

@app.route('/live')
def live_page():
    return render_template('live.html')

@app.route('/settings')
def settings_page():
    return render_template('settings.html')


# ================================================================
# API: 交易所配置
# ================================================================

@app.route('/api/exchange', methods=['GET'])
def api_get_exchange():
    return jsonify(load_exchange())

@app.route('/api/exchange', methods=['POST'])
def api_save_exchange():
    data = request.json
    save_exchange(data)
    return jsonify({'ok': True})

@app.route('/api/exchange/test', methods=['POST'])
def api_test_exchange():
    data = request.json
    try:
        client = ExchangeClient(
            data.get('exchange_id', 'binance'),
            data.get('api_key', ''),
            data.get('api_secret', ''),
            data.get('passphrase', ''),
        )
        ticker = client.fetch_ohlcv('BTC/USDT', '1h', limit=5)
        return jsonify({'ok': True, 'message': f'连接成功，获取到 {len(ticker)} 条数据'})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 400


# ================================================================
# API: 策略管理
# ================================================================

@app.route('/api/strategies', methods=['GET'])
def api_list_strategies():
    strategies = load_strategies()
    # 补充实盘状态
    for sid, s in strategies.items():
        status = live_trader.get_status(sid)
        if status:
            s['live_status'] = {
                'positions': status['positions'],
                'equity': status['equity'],
                'last_signal': status['last_signal'],
                'last_update': status['last_update'],
                'trade_count': len(status['trades']),
            }
    return jsonify(strategies)

@app.route('/api/strategies', methods=['POST'])
def api_create_strategy():
    data = request.json
    sid = str(uuid.uuid4())[:8]
    strategies = load_strategies()

    config = StrategyConfig(
        id=sid,
        name=data.get('name', f'Strategy-{sid}'),
        symbol=data.get('symbol', 'BTC/USDT'),
        timeframe=data.get('timeframe', '1h'),
        type=data.get('type', 'macd_cross'),
        params=data.get('params', {}),
        capital=float(data.get('capital', 10000)),
        leverage=int(data.get('leverage', 1)),
        position_size_pct=float(data.get('position_size_pct', 10)),
        stop_loss_pct=float(data.get('stop_loss_pct', 3)),
        take_profit_pct=float(data.get('take_profit_pct', 6)),
        trailing_stop=data.get('trailing_stop', True),
        trailing_stop_pct=float(data.get('trailing_stop_pct', 2)),
        max_drawdown_pct=float(data.get('max_drawdown_pct', 20)),
        paper=data.get('paper', True),
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )

    strategies[sid] = config.__dict__
    save_strategies(strategies)
    return jsonify({'ok': True, 'id': sid})

@app.route('/api/strategies/<sid>', methods=['DELETE'])
def api_delete_strategy(sid):
    live_trader.stop(sid)
    strategies = load_strategies()
    strategies.pop(sid, None)
    save_strategies(strategies)
    return jsonify({'ok': True})


# ================================================================
# API: 回测
# ================================================================

@app.route('/api/backtest', methods=['POST'])
def api_backtest():
    data = request.json
    try:
        exchange_cfg = load_exchange()
        client = ExchangeClient(
            exchange_cfg.get('exchange_id', 'binance'),
            exchange_cfg.get('api_key', ''),
            exchange_cfg.get('api_secret', ''),
        )

        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')
        start = data.get('start_date', '2024-01-01')
        end = data.get('end_date', '2025-01-01')

        df = client.fetch_ohlcv_range(symbol, timeframe, start, end)
        if df.empty:
            return jsonify({'error': '无法获取数据'}), 400

        result = BacktestEngine.run(
            df=df,
            strategy_type=data.get('strategy_type', 'macd_cross'),
            params=data.get('params', {}),
            capital=float(data.get('capital', 10000)),
            commission=float(data.get('commission', 0.0004)),
            slippage=float(data.get('slippage', 0.0005)),
            leverage=int(data.get('leverage', 1)),
            position_pct=float(data.get('position_size_pct', 10)),
            stop_loss_pct=float(data.get('stop_loss_pct', 3)),
            take_profit_pct=float(data.get('take_profit_pct', 6)),
        )

        return jsonify(result)

    except Exception as e:
        logger.error(f"Backtest error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ================================================================
# API: 实盘交易
# ================================================================

@app.route('/api/live/start/<sid>', methods=['POST'])
def api_live_start(sid):
    strategies = load_strategies()
    if sid not in strategies:
        return jsonify({'error': 'Strategy not found'}), 404

    s = strategies[sid]
    exchange_cfg = load_exchange()

    config = StrategyConfig(**s)
    config.exchange_id = exchange_cfg.get('exchange_id', config.exchange_id)
    config.api_key = exchange_cfg.get('api_key', config.api_key)
    config.api_secret = exchange_cfg.get('api_secret', config.api_secret)
    config.passphrase = exchange_cfg.get('passphrase', config.passphrase)

    if live_trader.start(config):
        config.status = 'running'
        strategies[sid] = config.__dict__
        save_strategies(strategies)
        return jsonify({'ok': True, 'message': f'Strategy {config.name} started'})
    else:
        return jsonify({'error': 'Already running'}), 400

@app.route('/api/live/stop/<sid>', methods=['POST'])
def api_live_stop(sid):
    live_trader.stop(sid)
    strategies = load_strategies()
    if sid in strategies:
        strategies[sid]['status'] = 'stopped'
        save_strategies(strategies)
    return jsonify({'ok': True, 'message': 'Stopped'})

@app.route('/api/live/status', methods=['GET'])
def api_live_status():
    return jsonify(live_trader.get_all_status())


# ================================================================
# API: 行情数据
# ================================================================

@app.route('/api/market/ticker/<symbol>', methods=['GET'])
def api_ticker(symbol):
    try:
        exchange_cfg = load_exchange()
        client = ExchangeClient(exchange_cfg.get('exchange_id', 'binance'))
        df = client.fetch_ohlcv(symbol.replace('_', '/'), '1h', limit=1)
        if df.empty:
            return jsonify({'error': 'No data'}), 404
        last = df.iloc[-1]
        return jsonify({
            'symbol': symbol,
            'price': float(last['close']),
            'timestamp': str(df.index[-1]),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/market/kline/<symbol>', methods=['GET'])
def api_kline(symbol):
    try:
        timeframe = request.args.get('timeframe', '1h')
        limit = int(request.args.get('limit', 100))
        exchange_cfg = load_exchange()
        client = ExchangeClient(exchange_cfg.get('exchange_id', 'binance'))
        df = client.fetch_ohlcv(symbol.replace('_', '/'), timeframe, limit=limit)
        df = Indicators.add_all(df)
        result = []
        for idx, row in df.iterrows():
            result.append({
                'time': str(idx),
                'open': round(float(row['open']), 2),
                'high': round(float(row['high']), 2),
                'low': round(float(row['low']), 2),
                'close': round(float(row['close']), 2),
                'volume': round(float(row['volume']), 2),
                'rsi': round(float(row.get('rsi', 0)), 2) if 'rsi' in row and pd.notna(row.get('rsi')) else None,
                'macd': round(float(row.get('macd', 0)), 4) if 'macd' in row and pd.notna(row.get('macd')) else None,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ================================================================
# 启动
# ================================================================

import pandas as pd

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
