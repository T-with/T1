"""
MyTradingPlatform — 多 Agent 协作交易系统（LLM 版）
5 个 AI Agent 各司其职，LLM 推理，投票决策

Agent 团队：
1. 技术分析师 — K线形态 + 指标信号（LLM 分析）
2. 新闻情绪师 — 加密货币新闻 + LLM 情绪判断
3. 链上数据师 — 资金费率 + 多空比（LLM 解读）
4. 风控官 — 波动率 + 回撤风险（LLM 评估）
5. 量化模型师 — 综合数据 + LLM 预测

所有 Agent 使用同一个 LLM API（OpenAI / DeepSeek / Kimi 兼容）
"""

import json
import logging
import urllib.request
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from engine.llm import chat_json

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'
CACHE_DIR = DATA_DIR / 'agent_cache'
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ================================================================
# Agent 输出数据结构
# ================================================================

@dataclass
class AgentOpinion:
    agent_name: str
    agent_icon: str
    action: str              # buy / sell / hold
    confidence: float        # 0-100
    weight: float
    reasoning: str
    details: Dict = field(default_factory=dict)


# ================================================================
# 工具函数
# ================================================================

def _prepare_market_data(df: pd.DataFrame, lookback: int = 50) -> str:
    """将 K 线数据整理为 LLM 可读的文本摘要"""
    from engine.core import Indicators
    df = Indicators.add_all(df)
    tail = df.tail(lookback)
    curr = df.iloc[-1]
    prev = df.iloc[-2]

    # 最近 N 根 K 线统计
    close = df['close']
    ret_5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100
    ret_20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100
    vol_20 = close.pct_change().tail(20).std() * np.sqrt(365) * 100

    # 持仓量、成交额估算
    avg_vol = df['volume'].tail(20).mean()
    vol_ratio = curr['volume'] / avg_vol if avg_vol > 0 else 1

    summary = f"""当前价格: {curr['close']:.2f}
24h最高: {df['high'].tail(24).max():.2f}  最低: {df['low'].tail(24).min():.2f}

技术指标:
- RSI(14): {curr['rsi']:.1f}  RSI(7): {df.iloc[-1].get('rsi', 0):.1f}
- MACD: {curr['macd']:.4f}  Signal: {curr['macd_signal']:.4f}  柱状图: {curr['macd_hist']:.4f}
- 布林带: 上轨 {curr['bb_upper']:.2f} / 中轨 {curr['bb_middle']:.2f} / 下轨 {curr['bb_lower']:.2f}
- SMA10: {curr['sma_10']:.2f}  SMA20: {curr['sma_20']:.2f}  SMA50: {curr['sma_50']:.2f}
- ATR: {curr['atr']:.2f}

动量:
- 5日收益: {ret_5:.2f}%  20日收益: {ret_20:.2f}%
- 年化波动率: {vol_20:.1f}%

成交量:
- 当前量/20日均量: {vol_ratio:.2f}x

最近10根K线 (时间/开/高/低/收/量):"""
    for idx, row in tail.tail(10).iterrows():
        summary += f"\n  {idx} | O:{row['open']:.2f} H:{row['high']:.2f} L:{row['low']:.2f} C:{row['close']:.2f} V:{row['volume']:.0f}"

    return summary


def _fetch_news_text(symbol: str) -> str:
    """抓取新闻并整理为文本"""
    coin = symbol.split('/')[0] if '/' in symbol else symbol
    headlines = []

    # CryptoCompare
    try:
        url = f"https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories={coin}"
        req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            for item in data.get('Data', [])[:15]:
                headlines.append(f"- [{item.get('source', '')}] {item.get('title', '')}")
    except Exception as e:
        logger.warning(f"CryptoCompare news failed: {e}")

    if not headlines:
        return "暂无相关新闻"
    return f"最近 {coin} 新闻 ({len(headlines)} 条):\n" + "\n".join(headlines)


def _fetch_onchain_data(symbol: str) -> str:
    """抓取链上数据并整理为文本"""
    coin = symbol.replace('/', '') if '/' in symbol else symbol
    parts = []

    # 资金费率
    try:
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={coin}&limit=3"
        req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            for item in data:
                ts = datetime.fromtimestamp(item['fundingTime'] / 1000).strftime('%m-%d %H:%M')
                parts.append(f"- 资金费率 ({ts}): {float(item['fundingRate'])*100:.4f}%")
    except Exception as e:
        logger.warning(f"Funding rate failed: {e}")

    # 多空比
    try:
        url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={coin}&period=1h&limit=5"
        req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            for item in data:
                ts = datetime.fromtimestamp(int(item['timestamp']) / 1000).strftime('%m-%d %H:%M')
                parts.append(f"- 多空比 ({ts}): {float(item['longShortRatio']):.3f}")
    except Exception as e:
        logger.warning(f"Long/short ratio failed: {e}")

    # 未平仓合约
    try:
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={coin}"
        req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            parts.append(f"- 未平仓合约: {data.get('openInterest', 'N/A')}")
    except Exception as e:
        logger.warning(f"OI failed: {e}")

    if not parts:
        return "链上数据不可用（可能非 Binance 交易对）"
    return f"链上数据 ({coin}):\n" + "\n".join(parts)


# ================================================================
# Agent 1: 技术分析师（LLM）
# ================================================================

class TechnicalAgent:
    name = "技术分析师"
    icon = "📊"
    vote_weight = 0.25

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        market_data = _prepare_market_data(df)

        prompt = f"""你是一位资深加密货币技术分析师。请根据以下 K 线数据和技术指标，给出交易判断。

{market_data}

请以 JSON 格式回复：
{{
  "action": "buy" 或 "sell" 或 "hold",
  "confidence": 0-100 的置信度,
  "reasoning": "简要分析理由（1-2句话）",
  "signals": ["信号1", "信号2", ...]
}}"""

        try:
            result = chat_json(
                messages=[
                    {'role': 'system', 'content': '你是专业的加密货币技术分析师，擅长 K 线形态、技术指标分析。只回复 JSON。'},
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.2,
            )
            return AgentOpinion(
                agent_name=self.name, agent_icon=self.icon,
                action=result.get('action', 'hold'),
                confidence=float(result.get('confidence', 50)),
                weight=self.vote_weight,
                reasoning=result.get('reasoning', ''),
                details={'signals': result.get('signals', [])},
            )
        except Exception as e:
            logger.error(f"TechnicalAgent LLM error: {e}")
            return AgentOpinion(self.name, self.icon, 'hold', 0, self.vote_weight, f'LLM 调用失败: {e}')


# ================================================================
# Agent 2: 新闻情绪师（LLM）
# ================================================================

class NewsSentimentAgent:
    name = "新闻情绪师"
    icon = "📰"
    vote_weight = 0.20

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        news_text = _fetch_news_text(symbol)

        prompt = f"""你是一位加密货币新闻分析师。请根据以下新闻，分析市场情绪和对 {symbol} 价格的潜在影响。

{news_text}

请以 JSON 格式回复：
{{
  "action": "buy" 或 "sell" 或 "hold",
  "confidence": 0-100 的置信度,
  "reasoning": "情绪分析结论（1-2句话）",
  "sentiment": "positive" 或 "negative" 或 "neutral",
  "key_events": ["关键事件1", "关键事件2", ...]
}}"""

        try:
            result = chat_json(
                messages=[
                    {'role': 'system', 'content': '你是加密货币新闻分析师，擅长从新闻中提取市场情绪信号。只回复 JSON。'},
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.2,
            )
            return AgentOpinion(
                agent_name=self.name, agent_icon=self.icon,
                action=result.get('action', 'hold'),
                confidence=float(result.get('confidence', 50)),
                weight=self.vote_weight,
                reasoning=result.get('reasoning', ''),
                details={
                    'sentiment': result.get('sentiment', ''),
                    'key_events': result.get('key_events', []),
                },
            )
        except Exception as e:
            logger.error(f"NewsSentimentAgent LLM error: {e}")
            return AgentOpinion(self.name, self.icon, 'hold', 0, self.vote_weight, f'LLM 调用失败: {e}')


# ================================================================
# Agent 3: 链上数据师（LLM）
# ================================================================

class OnChainAgent:
    name = "链上数据师"
    icon = "⛓️"
    vote_weight = 0.20

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        onchain = _fetch_onchain_data(symbol)

        prompt = f"""你是一位链上数据分析师。请根据以下链上数据，分析 {symbol} 的市场结构和资金流向。

{onchain}

请以 JSON 格式回复：
{{
  "action": "buy" 或 "sell" 或 "hold",
  "confidence": 0-100 的置信度,
  "reasoning": "链上分析结论（1-2句话）",
  "market_structure": "描述当前市场结构",
  "risk_level": "low" 或 "medium" 或 "high"
}}"""

        try:
            result = chat_json(
                messages=[
                    {'role': 'system', 'content': '你是链上数据分析师，擅长解读资金费率、多空比、未平仓合约等衍生品数据。只回复 JSON。'},
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.2,
            )
            return AgentOpinion(
                agent_name=self.name, agent_icon=self.icon,
                action=result.get('action', 'hold'),
                confidence=float(result.get('confidence', 50)),
                weight=self.vote_weight,
                reasoning=result.get('reasoning', ''),
                details={
                    'market_structure': result.get('market_structure', ''),
                    'risk_level': result.get('risk_level', ''),
                },
            )
        except Exception as e:
            logger.error(f"OnChainAgent LLM error: {e}")
            return AgentOpinion(self.name, self.icon, 'hold', 0, self.vote_weight, f'LLM 调用失败: {e}')


# ================================================================
# Agent 4: 风控官（LLM）
# ================================================================

class RiskAgent:
    name = "风控官"
    icon = "🛡️"
    vote_weight = 0.15

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        close = df['close']
        ret = close.pct_change().dropna()

        # 计算风险指标
        vol_20 = ret.tail(20).std() * np.sqrt(365) * 100
        peak = close.tail(20).cummax()
        drawdown = ((close.tail(20) - peak) / peak * 100).min()
        var_95 = np.percentile(ret.tail(100), 5) * 100

        high_60 = close.tail(60).max()
        low_60 = close.tail(60).min()
        pos_in_range = (close.iloc[-1] - low_60) / (high_60 - low_60) if (high_60 - low_60) > 0 else 0.5

        risk_data = f"""风险指标:
- 年化波动率: {vol_20:.1f}%
- 20日最大回撤: {drawdown:.2f}%
- VaR (95%): {var_95:.2f}%
- 价格在60日区间位置: {pos_in_range:.1%} (0%=最低, 100%=最高)
- 当前价格: {close.iloc[-1]:.2f}
- 60日最高: {high_60:.2f}  最低: {low_60:.2f}
- 连续涨跌: {self._count_consecutive(ret.tail(10))}"""

        prompt = f"""你是一位风险管理专家。请根据以下风险指标，评估 {symbol} 的风险等级和仓位建议。

{risk_data}

请以 JSON 格式回复：
{{
  "action": "buy" 或 "sell" 或 "hold",
  "confidence": 0-100 的置信度,
  "reasoning": "风险评估结论（1-2句话）",
  "risk_level": "low" 或 "medium" 或 "high" 或 "extreme",
  "position_advice": "仓位建议（如：建议轻仓/重仓/观望）"
}}"""

        try:
            result = chat_json(
                messages=[
                    {'role': 'system', 'content': '你是风险管理专家，擅长波动率分析、回撤控制、VaR 计算。从风控角度给建议，保守优先。只回复 JSON。'},
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.2,
            )
            return AgentOpinion(
                agent_name=self.name, agent_icon=self.icon,
                action=result.get('action', 'hold'),
                confidence=float(result.get('confidence', 50)),
                weight=self.vote_weight,
                reasoning=result.get('reasoning', ''),
                details={
                    'risk_level': result.get('risk_level', ''),
                    'position_advice': result.get('position_advice', ''),
                    'volatility': round(vol_20, 1),
                    'max_drawdown': round(drawdown, 2),
                },
            )
        except Exception as e:
            logger.error(f"RiskAgent LLM error: {e}")
            return AgentOpinion(self.name, self.icon, 'hold', 0, self.vote_weight, f'LLM 调用失败: {e}')

    @staticmethod
    def _count_consecutive(ret):
        up, down = 0, 0
        for r in reversed(ret):
            if r > 0:
                up += 1; down = 0
            elif r < 0:
                down += 1; up = 0
            else:
                break
        if up > 0: return f"连续{up}天上涨"
        if down > 0: return f"连续{down}天下跌"
        return "横盘"


# ================================================================
# Agent 5: 量化模型师（LLM 综合分析）
# ================================================================

class QuantModelAgent:
    name = "量化模型师"
    icon = "🧮"
    vote_weight = 0.20

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        market_data = _prepare_market_data(df, lookback=30)

        prompt = f"""你是一位量化交易专家。请综合分析以下 {symbol} 市场数据，给出量化交易信号。

{market_data}

请以 JSON 格式回复：
{{
  "action": "buy" 或 "sell" 或 "hold",
  "confidence": 0-100 的置信度,
  "reasoning": "量化分析结论（1-2句话）",
  "entry_price": 建议入场价,
  "stop_loss": 建议止损价,
  "take_profit": 建议止盈价
}}"""

        try:
            result = chat_json(
                messages=[
                    {'role': 'system', 'content': '你是量化交易专家，综合运用技术分析、统计套利、动量策略。只回复 JSON。'},
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.2,
            )
            return AgentOpinion(
                agent_name=self.name, agent_icon=self.icon,
                action=result.get('action', 'hold'),
                confidence=float(result.get('confidence', 50)),
                weight=self.vote_weight,
                reasoning=result.get('reasoning', ''),
                details={
                    'entry_price': result.get('entry_price'),
                    'stop_loss': result.get('stop_loss'),
                    'take_profit': result.get('take_profit'),
                },
            )
        except Exception as e:
            logger.error(f"QuantModelAgent LLM error: {e}")
            return AgentOpinion(self.name, self.icon, 'hold', 0, self.vote_weight, f'LLM 调用失败: {e}')


# ================================================================
# Agent 编排器 — 讨论投票系统
# ================================================================

class AgentOrchestrator:
    def __init__(self):
        self.agents = [
            TechnicalAgent(),
            NewsSentimentAgent(),
            OnChainAgent(),
            RiskAgent(),
            QuantModelAgent(),
        ]

    def run_debate(self, df: pd.DataFrame, symbol: str, params: Dict) -> Dict:
        """运行完整分析投票流程"""
        opinions = []
        errors = []

        for agent in self.agents:
            try:
                opinion = agent.analyze(df, symbol, params)
                opinions.append(opinion)
            except Exception as e:
                logger.error(f"Agent {agent.name} error: {e}")
                errors.append(f"{agent.icon} {agent.name}: {str(e)}")

        if not opinions:
            return {
                'status': 'error',
                'message': '所有 Agent 均无法分析',
                'errors': errors,
            }

        vote_result = self._weighted_vote(opinions)

        return {
            'status': 'ok',
            'timestamp': datetime.now().isoformat(),
            'symbol': symbol,
            'final_decision': vote_result,
            'agents': [
                {
                    'name': op.agent_name,
                    'icon': op.agent_icon,
                    'action': op.action,
                    'confidence': round(op.confidence, 1),
                    'weight': op.weight,
                    'reasoning': op.reasoning,
                    'details': op.details,
                }
                for op in opinions
            ],
            'errors': errors,
            'consensus': self._calc_consensus(opinions),
        }

    def _weighted_vote(self, opinions: List[AgentOpinion]) -> Dict:
        buy_score = 0
        sell_score = 0
        hold_score = 0
        total_weight = 0

        for op in opinions:
            w = op.weight * (op.confidence / 100)
            if op.action == 'buy':
                buy_score += w
            elif op.action == 'sell':
                sell_score += w
            else:
                hold_score += w
            total_weight += op.weight

        if total_weight > 0:
            buy_score /= total_weight
            sell_score /= total_weight
            hold_score /= total_weight

        scores = {'buy': buy_score, 'sell': sell_score, 'hold': hold_score}
        decision = max(scores, key=scores.get)
        confidence = scores[decision] * 100
        action_text = {'buy': '买入', 'sell': '卖出', 'hold': '观望'}

        return {
            'action': decision,
            'text': action_text[decision],
            'confidence': round(confidence, 1),
            'scores': {
                'buy': round(buy_score * 100, 1),
                'sell': round(sell_score * 100, 1),
                'hold': round(hold_score * 100, 1),
            },
            'vote_breakdown': [
                {
                    'agent': f'{op.agent_icon} {op.agent_name}',
                    'vote': op.action,
                    'confidence': round(op.confidence, 1),
                    'effective_weight': round(op.weight * (op.confidence / 100) * 100, 1),
                }
                for op in opinions
            ],
        }

    def _calc_consensus(self, opinions: List[AgentOpinion]) -> Dict:
        actions = [op.action for op in opinions]
        total = len(actions)
        max_agreement = max(actions.count(a) for a in set(actions)) / total * 100

        if max_agreement >= 80:
            level = '强共识'
        elif max_agreement >= 60:
            level = '中等共识'
        else:
            level = '分歧'

        return {
            'level': level,
            'agreement_pct': round(max_agreement, 1),
            'buy_votes': actions.count('buy'),
            'sell_votes': actions.count('sell'),
            'hold_votes': actions.count('hold'),
            'total_agents': total,
        }


# 单例
orchestrator = AgentOrchestrator()
