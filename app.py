"""MyTradingPlatform Flask App"""
from flask import Flask, render_template, jsonify, request
from functools import wraps
import json
import uuid
import logging
import os
import pandas as pd
from datetime import datetime
from pathlib import Path

from engine.core import (
    StrategyConfig, ExchangeClient, BacktestEngine,
    LiveTrader, Indicators, StrategyEngine,
    filter_strategy_config, encrypt_exchange_config,
    decrypt_exchange_config, redact_secrets,
    decrypt_strategy_secrets,
    AIMultiFactorStrategy,
)
from engine.agents import orchestrator

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================================================================
# Basic Auth
# ================================================================
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'changeme')

def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASS

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return jsonify({'error': 'Unauthorized'}), 401, {
                'WWW-Authenticate': 'Basic realm="MyTradingPlatform"'
            }
        return f(*args, **kwargs)
    return decorated

# 对所有非静态路由添加认证
@app.before_request
def auth_check():
    # 放行静态资源（如果有）和健康检查
    if request.endpoint and 'static' in request.endpoint:
        return None
    if request.path == '/health':
        return None
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({'error': 'Unauthorized'}), 401, {
            'WWW-Authenticate': 'Basic realm="MyTradingPlatform"'
        }


# ================================================================
# 数据存储
# ================================================================
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
        raw = json.loads(EXCHANGE_FILE.read_text())
        # 返回解密版本供内部使用
        return decrypt_exchange_config(raw)
    return {}

def load_exchange_raw() -> dict:
    """加载原始（含加密标记）的交易所配置"""
    if EXCHANGE_FILE.exists():
        return json.loads(EXCHANGE_FILE.read_text())
    return {}


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

@app.route('/ai')
def ai_page():
    return render_template('ai.html')

@app.route('/agents')
def agents_page():
    return render_template('agents.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


# ================================================================
# API: 交易所配置
# ================================================================

@app.route('/api/exchange', methods=['GET'])
def api_get_exchange():
    raw = load_exchange_raw()
    # 脱敏后返回给前端
    return jsonify(redact_secrets(raw))

@app.route('/api/exchange', methods=['POST'])
def api_save_exchange():
    data = request.json
    # 加载现有配置，只更新用户实际修改的字段
    existing = load_exchange_raw()
    for field in ('api_key', 'api_secret', 'passphrase'):
        val = data.get(field, '')
        # 如果前端传来的值包含 ****（脱敏标记），说明用户没有修改，保留原值
        if val and '****' in val:
            data[field] = existing.get(field, '')
    # 加密后存储
    encrypted = encrypt_exchange_config(data)
    EXCHANGE_FILE.write_text(json.dumps(encrypted, ensure_ascii=False, indent=2))
    logger.info("Exchange config saved (secrets encrypted)")
    return jsonify({'ok': True})

@app.route('/api/exchange/test', methods=['POST'])
def api_test_exchange():
    """测试连接 — 前端提交的是明文，直接用"""
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
    logger.info(f"Strategy created: {sid} ({config.name})")
    return jsonify({'ok': True, 'id': sid})

@app.route('/api/strategies/<sid>', methods=['DELETE'])
def api_delete_strategy(sid):
    live_trader.stop(sid)
    strategies = load_strategies()
    strategies.pop(sid, None)
    save_strategies(strategies)
    logger.info(f"Strategy deleted: {sid}")
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

    # ✅ FIX: 过滤合法字段，避免未知字段导致 TypeError
    raw_config = filter_strategy_config(strategies[sid])
    # ✅ FIX: 解密策略中存储的加密密钥
    raw_config = decrypt_strategy_secrets(raw_config)

    exchange_cfg = load_exchange()
    raw_config['exchange_id'] = exchange_cfg.get('exchange_id', raw_config.get('exchange_id', 'binance'))
    raw_config['api_key'] = exchange_cfg.get('api_key', raw_config.get('api_key', ''))
    raw_config['api_secret'] = exchange_cfg.get('api_secret', raw_config.get('api_secret', ''))
    raw_config['passphrase'] = exchange_cfg.get('passphrase', raw_config.get('passphrase', ''))

    config = StrategyConfig(**raw_config)

    if live_trader.start(config):
        config.status = 'running'
        strategies[sid] = config.__dict__
        save_strategies(strategies)
        logger.info(f"Strategy started: {sid} ({config.name})")
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
    logger.info(f"Strategy stopped: {sid}")
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
# API: AI 分析
# ================================================================

@app.route('/api/ai/analyze', methods=['POST'])
def api_ai_analyze():
    """AI 多因子分析 — 自动使用公共 API 获取数据"""
    data = request.json or {}
    try:
        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')
        params = data.get('params', {})

        # 始终用 Binance 公共 API
        client = ExchangeClient('binance')

        train_window = params.get('train_window', 500)
        limit = train_window + 100
        df = client.fetch_ohlcv(symbol, timeframe, limit=limit)

        if df.empty:
            return jsonify({'error': '无法获取数据，请检查网络连接', 'status': 'error'}), 400

        result = AIMultiFactorStrategy.analyze(df, params)
        return jsonify(result)

    except Exception as e:
        logger.error(f"AI analyze error: {e}", exc_info=True)
        return jsonify({'error': str(e), 'status': 'error'}), 500


@app.route('/api/agents/debate', methods=['POST'])
def api_agents_debate():
    """多 Agent 协作投票分析 — 自动使用公共 API 获取数据"""
    data = request.json or {}
    try:
        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')
        params = data.get('params', {})

        # 始终用 Binance 公共 API 获取 K 线（不需要 API Key）
        client = ExchangeClient('binance')

        limit = max(params.get('train_window', 500) + 100, 600)
        df = client.fetch_ohlcv(symbol, timeframe, limit=limit)

        if df.empty:
            return jsonify({'error': '无法获取数据，请检查网络连接', 'status': 'error'}), 400

        result = orchestrator.run_debate(df, symbol, params)
        return jsonify(result)

    except Exception as e:
        logger.error(f"Agents debate error: {e}", exc_info=True)
        return jsonify({'error': str(e), 'status': 'error'}), 500


@app.route('/api/agents/status', methods=['GET'])
def api_agents_status():
    """检查 Agent 系统状态"""
    from engine.agents import orchestrator
    from engine.llm import check_connection

    status = {
        'agents': [{'name': a.name, 'icon': a.icon, 'weight': a.vote_weight} for a in orchestrator.agents],
        'total_agents': len(orchestrator.agents),
        'status': 'ready',
    }

    # 检查 LLM API 连通性
    llm_status = check_connection()
    status['llm'] = llm_status

    # 测试 Binance 公共 API 连通性
    try:
        client = ExchangeClient('binance')
        df = client.fetch_ohlcv('BTC/USDT', '1h', limit=2)
        status['exchange_connected'] = not df.empty
        status['latest_price'] = float(df.iloc[-1]['close']) if not df.empty else None
    except Exception as e:
        status['exchange_connected'] = False
        status['exchange_error'] = str(e)

    return jsonify(status)


# ================================================================
# 启动
# ================================================================


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
