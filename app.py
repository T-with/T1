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
from engine.data import data_manager
from engine.execution import (
    SmartOrderRouter, OrderRequest, OrderType, SlippageEstimator
)
from engine.risk import risk_manager, VolatilityEngine
from engine.sentiment import sentiment_engine
from engine.models import model_manager
from engine.rl import rl_manager

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

live_trader = LiveTrader(risk_mgr=risk_manager)


def _make_exchange_client(exchange_id: str = None):
    """工厂函数：创建 ExchangeClient（供 SmartOrderRouter 使用）"""
    cfg = load_exchange()
    return ExchangeClient(
        exchange_id or cfg.get('exchange_id', 'binance'),
        cfg.get('api_key', ''),
        cfg.get('api_secret', ''),
        cfg.get('passphrase', ''),
    )


order_router = SmartOrderRouter(_make_exchange_client, data_manager)


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
# API: 实时数据层
# ================================================================

@app.route('/api/data/market-snapshot/<symbol>', methods=['GET'])
def api_market_snapshot(symbol):
    """获取交易对完整市场快照（K线+OrderBook+成交统计）"""
    try:
        exchange_id = request.args.get('exchange', 'binance')
        symbol_fmt = symbol.replace('_', '/')
        # 确保数据源已订阅
        data_manager.subscribe_symbol(exchange_id, symbol_fmt, '1h', ['kline', 'orderbook', 'trades'])
        import time; time.sleep(1)  # 等待首帧数据
        snapshot = data_manager.get_market_snapshot(exchange_id, symbol_fmt)
        return jsonify(snapshot)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/data/orderbook/<symbol>', methods=['GET'])
def api_orderbook(symbol):
    """获取 OrderBook 深度"""
    try:
        exchange_id = request.args.get('exchange', 'binance')
        symbol_fmt = symbol.replace('_', '/')
        ob = data_manager.get_orderbook(exchange_id, symbol_fmt)
        if ob is None:
            return jsonify({'error': 'OrderBook not available'}), 404
        return jsonify({
            'symbol': symbol_fmt,
            'exchange': exchange_id,
            'bids': [{'price': lv.price, 'amount': lv.amount} for lv in ob.bids[:20]],
            'asks': [{'price': lv.price, 'amount': lv.amount} for lv in ob.asks[:20]],
            'spread': round(ob.spread, 8),
            'spread_pct': round(ob.spread_pct, 4),
            'mid_price': round(ob.mid_price, 2),
            'imbalance': round(ob.imbalance_ratio(10), 3),
            'timestamp': ob.timestamp,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/data/trades/<symbol>', methods=['GET'])
def api_trade_stats(symbol):
    """获取逐笔成交统计"""
    try:
        exchange_id = request.args.get('exchange', 'binance')
        symbol_fmt = symbol.replace('_', '/')
        stats = data_manager.get_trade_stats(exchange_id, symbol_fmt)
        if stats is None:
            return jsonify({'error': 'Trade data not available'}), 404
        feed = data_manager.get_feed(exchange_id)
        whales = []
        if feed:
            whales = feed.trades.detect_whale_activity(exchange_id, symbol_fmt)
        return jsonify({
            'symbol': symbol_fmt,
            'stats': {k: round(v, 4) if isinstance(v, float) else v for k, v in stats.items()},
            'whale_alerts': whales[:10],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/data/status', methods=['GET'])
def api_data_status():
    """获取所有数据源连接状态"""
    return jsonify(data_manager.get_all_status())


@app.route('/api/data/subscribe', methods=['POST'])
def api_data_subscribe():
    """订阅交易对数据流"""
    data = request.json or {}
    exchange_id = data.get('exchange', 'binance')
    symbol = data.get('symbol', 'BTC/USDT')
    timeframe = data.get('timeframe', '1h')
    streams = data.get('streams', ['kline', 'orderbook', 'trades'])
    try:
        data_manager.subscribe_symbol(exchange_id, symbol, timeframe, streams)
        return jsonify({'ok': True, 'message': f'Subscribed {symbol} on {exchange_id}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ================================================================
# API: 智能订单执行
# ================================================================

@app.route('/api/execute/order', methods=['POST'])
def api_execute_order():
    """智能订单执行（支持市价/TWAP/VWAP）"""
    data = request.json or {}
    try:
        order = OrderRequest(
            id=str(uuid.uuid4())[:8],
            symbol=data.get('symbol', 'BTC/USDT'),
            exchange=data.get('exchange', 'binance'),
            side=data.get('side', 'buy'),
            type=OrderType(data.get('type', 'market')),
            total_amount=float(data.get('amount', 0)),
            price=float(data.get('price', 0)),
            twap_duration_sec=int(data.get('twap_duration', 60)),
            twap_slices=int(data.get('twap_slices', 10)),
            vwap_window=int(data.get('vwap_window', 20)),
            max_slippage_pct=float(data.get('max_slippage', 0.5)),
            strategy_id=data.get('strategy_id', ''),
        )

        if order.total_amount <= 0:
            return jsonify({'error': 'Amount must be > 0'}), 400

        result = order_router.route_order(order)
        return jsonify({
            'order_id': result.order_id,
            'status': result.status.value,
            'filled_amount': result.filled_amount,
            'avg_price': round(result.avg_price, 2),
            'total_cost': round(result.total_cost, 2),
            'slippage_pct': round(result.slippage_pct, 4),
            'duration_sec': round(result.duration_sec, 2),
            'fills': len(result.fills),
        })
    except Exception as e:
        logger.error(f"Execute order error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/execute/slippage-estimate', methods=['POST'])
def api_slippage_estimate():
    """预估滑点"""
    data = request.json or {}
    try:
        exchange_id = data.get('exchange', 'binance')
        symbol = data.get('symbol', 'BTC/USDT')
        side = data.get('side', 'buy')
        amount = float(data.get('amount', 0))

        ob = data_manager.get_orderbook(exchange_id, symbol)
        if ob is None:
            # fallback: 从 REST 获取
            client = ExchangeClient(exchange_id)
            raw = client.exchange.fetch_order_book(symbol, 20)
            data_manager.get_feed(exchange_id).orderbook.update_snapshot(
                exchange_id, symbol, raw.get('bids', []), raw.get('asks', [])
            )
            ob = data_manager.get_orderbook(exchange_id, symbol)

        estimate = SlippageEstimator.estimate(ob, side, amount)
        return jsonify(estimate)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/execute/stats', methods=['GET'])
def api_execute_stats():
    """获取执行层统计"""
    return jsonify(order_router.get_stats())


# ================================================================
# API: 风险管理
# ================================================================

@app.route('/api/risk/dashboard', methods=['GET'])
def api_risk_dashboard():
    """风控仪表盘"""
    return jsonify(risk_manager.get_risk_dashboard())


@app.route('/api/risk/check-position', methods=['POST'])
def api_risk_check_position():
    """检查仓位风险"""
    data = request.json or {}
    try:
        result = risk_manager.check_position(
            strategy_id=data.get('strategy_id', ''),
            symbol=data.get('symbol', ''),
            entry_price=float(data.get('entry_price', 0)),
            current_price=float(data.get('current_price', 0)),
            highest_price=float(data.get('highest_price', 0)),
            lowest_price=float(data.get('lowest_price', 0)),
            side=data.get('side', 'long'),
            atr=float(data.get('atr', 0)),
            equity=float(data.get('equity', 0)),
            capital=float(data.get('capital', 0)),
        )
        # 转换 RiskEvent 为可序列化格式
        result['events'] = [
            {'type': e.type.value, 'level': e.level.value, 'message': e.message}
            for e in result.get('events', [])
        ]
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/risk/volatility/<symbol>', methods=['GET'])
def api_risk_volatility(symbol):
    """波动率分析"""
    try:
        timeframe = request.args.get('timeframe', '1h')
        exchange_id = request.args.get('exchange', 'binance')
        symbol_fmt = symbol.replace('_', '/')

        client = ExchangeClient(exchange_id)
        df = client.fetch_ohlcv(symbol_fmt, timeframe, limit=100)
        if df.empty:
            return jsonify({'error': 'No data'}), 400

        prices = df['close'].values
        vol_engine = VolatilityEngine()

        result = {
            'symbol': symbol_fmt,
            'current_price': round(float(prices[-1]), 2),
            'volatility': {
                'historical_20': round(vol_engine.historical_volatility(prices, 20), 4),
                'historical_60': round(vol_engine.historical_volatility(prices, 60), 4),
                'ewma_20': round(vol_engine.ewma_volatility(prices, 20), 4),
                'parkinson_20': round(vol_engine.parkinson_volatility(df, 20), 4),
            },
            'regime': vol_engine.implied_regime(prices),
            'spike_detection': vol_engine.detect_volatility_spike(prices),
        }

        # 市场风险检查
        market_risk = risk_manager.check_market_conditions(prices, symbol_fmt)
        result['risk_level'] = market_risk['risk_level']

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/risk/kelly/<strategy_id>', methods=['GET'])
def api_risk_kelly(strategy_id):
    """凯利公式仓位建议"""
    try:
        result = risk_manager.kelly.calculate(strategy_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/risk/circuit-breaker/release', methods=['POST'])
def api_risk_release_breaker():
    """手动解除熔断"""
    risk_manager.release_circuit_breaker()
    return jsonify({'ok': True, 'message': 'Circuit breaker released'})


@app.route('/api/risk/resume/<strategy_id>', methods=['POST'])
def api_risk_resume_strategy(strategy_id):
    """手动恢复被暂停的策略"""
    risk_manager.resume_strategy(strategy_id)
    return jsonify({'ok': True, 'message': f'Strategy {strategy_id} resumed'})


# ================================================================
# API: 情绪分析
# ================================================================

@app.route('/api/sentiment/<symbol>', methods=['GET'])
def api_sentiment(symbol):
    """获取市场情绪指数"""
    try:
        symbol_fmt = symbol.replace('_', '/')
        index = sentiment_engine.get_sentiment_index(symbol_fmt)
        return jsonify(index)
    except Exception as e:
        logger.error(f"Sentiment error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentiment/<symbol>/detail', methods=['GET'])
def api_sentiment_detail(symbol):
    """获取完整情绪分析报告"""
    try:
        symbol_fmt = symbol.replace('_', '/')
        snapshot = sentiment_engine.analyze(symbol_fmt)
        return jsonify({
            'symbol': snapshot.symbol,
            'overall_score': round(snapshot.overall_score, 3),
            'overall_label': snapshot.overall_label,
            'news_score': round(snapshot.news_score, 3),
            'social_score': round(snapshot.social_score, 3),
            'onchain_score': round(snapshot.onchain_score, 3),
            'item_count': snapshot.item_count,
            'top_positive': snapshot.top_positive,
            'top_negative': snapshot.top_negative,
            'trend_1h': round(snapshot.trend_1h, 3),
            'trend_24h': round(snapshot.trend_24h, 3),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentiment/<symbol>/onchain', methods=['GET'])
def api_onchain(symbol):
    """获取链上数据"""
    try:
        symbol_fmt = symbol.replace('_', '/')
        report = sentiment_engine.onchain.get_full_report(symbol_fmt)
        return jsonify(report)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ================================================================
# API: 深度学习模型
# ================================================================

@app.route('/api/models/train', methods=['POST'])
def api_model_train():
    """训练 LSTM / Transformer 模型"""
    data = request.json or {}
    try:
        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')
        model_type = data.get('model_type', 'lstm')
        params = data.get('params', {})

        if model_type not in ('lstm', 'transformer'):
            return jsonify({'error': 'model_type must be lstm or transformer'}), 400

        # 获取数据
        client = ExchangeClient('binance')
        limit = max(params.get('train_window', 1000), 800)
        df = client.fetch_ohlcv(symbol, timeframe, limit=limit)

        if df.empty or len(df) < 200:
            return jsonify({'error': f'Insufficient data: {len(df)} bars'}), 400

        result = model_manager.train(
            df=df,
            model_type=model_type,
            symbol=symbol,
            timeframe=timeframe,
            seq_len=params.get('seq_len', 60),
            epochs=params.get('epochs', 50),
            batch_size=params.get('batch_size', 64),
            lr=params.get('lr', 0.001),
            target_return=params.get('target_return', 0.002),
            **{k: v for k, v in params.items() if k not in
               ('seq_len', 'epochs', 'batch_size', 'lr', 'target_return', 'train_window')}
        )
        return jsonify(result)

    except Exception as e:
        logger.error(f"Model train error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/models/predict', methods=['POST'])
def api_model_predict():
    """使用训练好的模型预测"""
    data = request.json or {}
    try:
        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')
        model_type = data.get('model_type', 'lstm')

        client = ExchangeClient('binance')
        df = client.fetch_ohlcv(symbol, timeframe, limit=200)

        if df.empty:
            return jsonify({'error': 'No data'}), 400

        if model_type == 'ensemble':
            result = model_manager.predict_both(df, symbol, timeframe)
        else:
            result = model_manager.predict(df, model_type, symbol, timeframe)

        return jsonify(result)

    except Exception as e:
        logger.error(f"Model predict error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/models/list', methods=['GET'])
def api_model_list():
    """列出所有已训练模型"""
    models = model_manager.list_models()
    rl_models = rl_manager.list_models()
    return jsonify({'dl_models': models, 'rl_models': rl_models})


# ================================================================
# API: 强化学习
# ================================================================

@app.route('/api/rl/train', methods=['POST'])
def api_rl_train():
    """训练 RL PPO 交易代理"""
    data = request.json or {}
    try:
        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')
        params = data.get('params', {})

        client = ExchangeClient('binance')
        limit = max(params.get('train_window', 1000), 500)
        df = client.fetch_ohlcv(symbol, timeframe, limit=limit)

        if df.empty or len(df) < 200:
            return jsonify({'error': f'Insufficient data: {len(df)} bars'}), 400

        result = rl_manager.train(
            df=df, symbol=symbol, timeframe=timeframe,
            n_episodes=params.get('n_episodes', 100),
            hidden_size=params.get('hidden_size', 128),
            lr=params.get('lr', 3e-4),
            commission=params.get('commission', 0.0004),
            reward_type=params.get('reward_type', 'sharpe'),
        )
        return jsonify(result)

    except Exception as e:
        logger.error(f"RL train error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/rl/predict', methods=['POST'])
def api_rl_predict():
    """使用 RL 代理预测"""
    data = request.json or {}
    try:
        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')

        client = ExchangeClient('binance')
        df = client.fetch_ohlcv(symbol, timeframe, limit=200)

        if df.empty:
            return jsonify({'error': 'No data'}), 400

        result = rl_manager.predict(df, symbol, timeframe)
        return jsonify(result)

    except Exception as e:
        logger.error(f"RL predict error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ================================================================
# API: 网格 & DCA
# ================================================================

@app.route('/api/grid/config', methods=['POST'])
def api_grid_config():
    """获取 AI 动态网格参数"""
    data = request.json or {}
    try:
        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')
        params = data.get('params', {})

        client = ExchangeClient('binance')
        df = client.fetch_ohlcv(symbol, timeframe, limit=100)

        if df.empty:
            return jsonify({'error': 'No data'}), 400

        from engine.core import AIGridStrategy, Indicators
        df = Indicators.add_all(df)
        config = AIGridStrategy._compute_grid_params(df, params)
        levels = AIGridStrategy.compute_grid_levels(
            float(df['close'].iloc[-1]), config
        )

        return jsonify({
            'symbol': symbol,
            'current_price': round(float(df['close'].iloc[-1]), 2),
            'config': config,
            'grid_levels': levels,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/dca/weight', methods=['POST'])
def api_dca_weight():
    """获取智能 DCA 本期投资权重"""
    data = request.json or {}
    try:
        symbol = data.get('symbol', 'BTC/USDT')
        timeframe = data.get('timeframe', '1h')
        params = data.get('params', {})

        client = ExchangeClient('binance')
        df = client.fetch_ohlcv(symbol, timeframe, limit=100)

        if df.empty:
            return jsonify({'error': 'No data'}), 400

        from engine.core import SmartDCAStrategy, Indicators
        df = Indicators.add_all(df)
        weight_info = SmartDCAStrategy._compute_weight(df, params)

        return jsonify({
            'symbol': symbol,
            'current_price': round(float(df['close'].iloc[-1]), 2),
            **weight_info,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ================================================================
# 启动
# ================================================================


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
