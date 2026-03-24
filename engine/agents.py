"""
MyTradingPlatform — 多 Agent 协作交易系统
5 个 AI Agent 各司其职，讨论投票决定买卖

Agent 团队：
1. 技术分析师 — K线形态 + 指标信号
2. 新闻情绪师 — 加密货币新闻抓取 + NLP 情绪分析
3. 链上数据师 — 资金费率 + 未平仓合约 + 大户动向
4. 风控官 — 波动率 + 回撤 + 相关性风险
5. 量化模型师 — XGBoost 多因子预测

最终由投票系统加权决策
"""

import json
import logging
import time
import hashlib
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

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
    agent_name: str          # Agent 名称
    agent_icon: str          # 图标
    action: str              # buy / sell / hold
    confidence: float        # 0-100 置信度
    weight: float            # 投票权重 0-1
    reasoning: str           # 推理过程
    details: Dict = field(default_factory=dict)  # 详细数据


# ================================================================
# 基础 Agent 类
# ================================================================

class BaseAgent:
    """所有 Agent 的基类"""
    name: str = "Base"
    icon: str = "🤖"
    vote_weight: float = 0.2

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        raise NotImplementedError


# ================================================================
# Agent 1: 技术分析师
# ================================================================

class TechnicalAgent(BaseAgent):
    """
    技术分析师
    综合 RSI/MACD/布林带/均线/成交量/K线形态 多维度打分
    """
    name = "技术分析师"
    icon = "📊"
    vote_weight = 0.25

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        from engine.core import Indicators

        df = Indicators.add_all(df)
        n = len(df)
        if n < 50:
            return AgentOpinion(self.name, self.icon, 'hold', 0, self.vote_weight, '数据不足')

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        scores = []
        reasons = []

        # 1. RSI
        rsi = curr['rsi']
        if rsi < 30:
            scores.append(1)
            reasons.append(f'RSI({rsi:.0f}) 超卖')
        elif rsi > 70:
            scores.append(-1)
            reasons.append(f'RSI({rsi:.0f}) 超买')
        else:
            scores.append(0)
            reasons.append(f'RSI({rsi:.0f}) 中性')

        # 2. MACD 金叉/死叉
        if prev['macd'] <= prev['macd_signal'] and curr['macd'] > curr['macd_signal']:
            scores.append(1)
            reasons.append('MACD 金叉')
        elif prev['macd'] >= prev['macd_signal'] and curr['macd'] < curr['macd_signal']:
            scores.append(-1)
            reasons.append('MACD 死叉')
        elif curr['macd'] > curr['macd_signal']:
            scores.append(0.3)
            reasons.append('MACD 多头排列')
        else:
            scores.append(-0.3)
            reasons.append('MACD 空头排列')

        # 3. 布林带位置
        bb_pos = (curr['close'] - curr['bb_lower']) / (curr['bb_upper'] - curr['bb_lower']) if (curr['bb_upper'] - curr['bb_lower']) != 0 else 0.5
        if bb_pos > 0.95:
            scores.append(-0.8)
            reasons.append(f'触及布林上轨 (位置{bb_pos:.0%})')
        elif bb_pos < 0.05:
            scores.append(0.8)
            reasons.append(f'触及布林下轨 (位置{bb_pos:.0%})')
        elif bb_pos > 0.8:
            scores.append(-0.3)
            reasons.append(f'靠近布林上轨 (位置{bb_pos:.0%})')
        elif bb_pos < 0.2:
            scores.append(0.3)
            reasons.append(f'靠近布林下轨 (位置{bb_pos:.0%})')

        # 4. 均线趋势
        sma_dist = (curr['close'] - curr['sma_20']) / curr['sma_20'] * 100
        if curr['close'] > curr['sma_10'] > curr['sma_20'] > curr['sma_50']:
            scores.append(0.7)
            reasons.append('多头排列 (价格>SMA10>SMA20>SMA50)')
        elif curr['close'] < curr['sma_10'] < curr['sma_20'] < curr['sma_50']:
            scores.append(-0.7)
            reasons.append('空头排列')

        # 5. 成交量确认
        vol_ratio = curr['volume'] / df['volume'].rolling(20).mean().iloc[-1] if df['volume'].rolling(20).mean().iloc[-1] > 0 else 1
        if vol_ratio > 1.5:
            reasons.append(f'放量 {vol_ratio:.1f}x')
            # 放量跟随趋势
            if scores and scores[-1] > 0:
                scores.append(0.3)
            elif scores and scores[-1] < 0:
                scores.append(-0.3)

        # 6. 动量
        mom_5 = (curr['close'] / df['close'].iloc[-6] - 1) * 100 if n >= 6 else 0
        mom_20 = (curr['close'] / df['close'].iloc[-21] - 1) * 100 if n >= 21 else 0
        if mom_5 > 3 and mom_20 > 5:
            scores.append(0.5)
            reasons.append(f'强劲动量 (5日{mom_5:.1f}%, 20日{mom_20:.1f}%)')
        elif mom_5 < -3 and mom_20 < -5:
            scores.append(-0.5)
            reasons.append(f'弱势动量 (5日{mom_5:.1f}%, 20日{mom_20:.1f}%)')

        # 综合
        total = sum(scores) / max(len(scores), 1)
        if total > 0.2:
            action = 'buy'
        elif total < -0.2:
            action = 'sell'
        else:
            action = 'hold'

        confidence = min(abs(total) * 100, 100)

        return AgentOpinion(
            agent_name=self.name, agent_icon=self.icon,
            action=action, confidence=confidence,
            weight=self.vote_weight,
            reasoning=' | '.join(reasons) if reasons else '无明确信号',
            details={
                'rsi': round(rsi, 1),
                'macd': round(float(curr['macd']), 4),
                'bb_position': round(bb_pos, 2),
                'sma_trend': round(sma_dist, 2),
                'vol_ratio': round(vol_ratio, 2),
                'mom_5d': round(mom_5, 2),
                'mom_20d': round(mom_20, 2),
                'scores': [round(s, 2) for s in scores],
            },
        )


# ================================================================
# Agent 2: 新闻情绪师
# ================================================================

class NewsSentimentAgent(BaseAgent):
    """
    新闻情绪师
    抓取加密货币新闻，NLP 情绪分析
    使用 CryptoCompare / CoinGecko 公开 API 获取新闻
    """
    name = "新闻情绪师"
    icon = "📰"
    vote_weight = 0.20

    # 正面/负面关键词（中英双语）
    POSITIVE_WORDS = {
        'bullish', 'surge', 'soar', 'rally', 'breakout', 'adoption', 'partnership',
        'approval', 'upgrade', 'record', 'milestone', 'growth', 'institutional',
        'etf', 'approved', 'listing', 'integration', 'innovation', 'milestone',
        '牛市', '暴涨', '突破', '利好', '合作', '上线', '批准', '采用', '增长',
    }
    NEGATIVE_WORDS = {
        'bearish', 'crash', 'plunge', 'hack', 'scam', 'ban', 'regulation',
        'lawsuit', 'sec', 'warning', 'fraud', 'collapse', 'liquidation',
        'sell-off', 'dump', 'risk', 'fear', 'outflow',
        '熊市', '暴跌', '黑客', '诈骗', '监管', '禁止', '起诉', '清算', '抛售', '恐慌',
    }

    def _fetch_news(self, symbol: str) -> List[Dict]:
        """从多个来源抓取加密货币新闻"""
        import urllib.request
        import urllib.error

        coin = symbol.split('/')[0] if '/' in symbol else symbol
        news_items = []

        # 来源 1: CryptoCompare News API (免费)
        try:
            url = f"https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories={coin}"
            req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                for item in data.get('Data', [])[:20]:
                    news_items.append({
                        'title': item.get('title', ''),
                        'body': item.get('body', '')[:200],
                        'source': item.get('source', ''),
                        'time': item.get('published_on', 0),
                        'url': item.get('url', ''),
                    })
        except Exception as e:
            logger.warning(f"CryptoCompare news fetch failed: {e}")

        # 来源 2: CoinGecko 事件/状态 (免费)
        try:
            url = f"https://api.coingecko.com/api/v3/coins/{coin.lower()}/status_updates"
            req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                for item in data.get('status_updates', [])[:10]:
                    news_items.append({
                        'title': item.get('user_title', ''),
                        'body': item.get('description', '')[:200],
                        'source': 'CoinGecko',
                        'time': 0,
                    })
        except Exception as e:
            logger.warning(f"CoinGecko status fetch failed: {e}")

        return news_items

    def _sentiment_score(self, text: str) -> float:
        """简单关键词情绪打分"""
        text_lower = text.lower()
        pos = sum(1 for w in self.POSITIVE_WORDS if w in text_lower)
        neg = sum(1 for w in self.NEGATIVE_WORDS if w in text_lower)
        total = pos + neg
        if total == 0:
            return 0
        return (pos - neg) / total  # -1 到 1

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        try:
            news = self._fetch_news(symbol)
        except Exception as e:
            return AgentOpinion(
                self.name, self.icon, 'hold', 0, self.vote_weight,
                f'新闻抓取失败: {e}',
            )

        if not news:
            return AgentOpinion(
                self.name, self.icon, 'hold', 30, self.vote_weight,
                '未获取到相关新闻',
            )

        # 每条新闻打分
        scores = []
        for item in news:
            text = f"{item['title']} {item.get('body', '')}"
            score = self._sentiment_score(text)
            scores.append(score)

        avg_score = np.mean(scores)
        # 归一化到 -100 ~ 100
        sentiment_pct = round(avg_score * 100, 1)

        positive_count = sum(1 for s in scores if s > 0)
        negative_count = sum(1 for s in scores if s < 0)
        neutral_count = len(scores) - positive_count - negative_count

        if avg_score > 0.15:
            action = 'buy'
        elif avg_score < -0.15:
            action = 'sell'
        else:
            action = 'hold'

        confidence = min(abs(sentiment_pct), 100)

        # 关键词统计
        all_text = ' '.join(f"{n['title']} {n.get('body','')}" for n in news).lower()
        hot_words = []
        for w in self.POSITIVE_WORDS | self.NEGATIVE_WORDS:
            count = all_text.count(w)
            if count > 0:
                hot_words.append((w, count))
        hot_words.sort(key=lambda x: x[1], reverse=True)

        return AgentOpinion(
            agent_name=self.name, agent_icon=self.icon,
            action=action, confidence=confidence,
            weight=self.vote_weight,
            reasoning=f'分析 {len(news)} 条新闻，正面 {positive_count} / 中性 {neutral_count} / 负面 {negative_count}，情绪值 {sentiment_pct}',
            details={
                'news_count': len(news),
                'positive': positive_count,
                'neutral': neutral_count,
                'negative': negative_count,
                'sentiment_score': sentiment_pct,
                'hot_words': hot_words[:8],
                'top_headlines': [n['title'][:60] for n in news[:5]],
            },
        )


# ================================================================
# Agent 3: 链上数据师
# ================================================================

class OnChainAgent(BaseAgent):
    """
    链上数据师
    分析资金费率、未平仓合约、大户动向、交易所流量
    使用免费公开 API
    """
    name = "链上数据师"
    icon = "⛓️"
    vote_weight = 0.20

    def _fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """获取 Binance 资金费率"""
        import urllib.request
        try:
            coin = symbol.replace('/', '')
            url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={coin}&limit=1"
            req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if data:
                    return float(data[-1]['fundingRate'])
        except Exception as e:
            logger.warning(f"Funding rate fetch failed: {e}")
        return None

    def _fetch_oi(self, symbol: str) -> Optional[Dict]:
        """获取未平仓合约"""
        import urllib.request
        try:
            coin = symbol.replace('/', '')
            url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={coin}"
            req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.warning(f"OI fetch failed: {e}")
        return None

    def _fetch_long_short_ratio(self, symbol: str) -> Optional[Dict]:
        """获取多空比"""
        import urllib.request
        try:
            coin = symbol.replace('/', '')
            url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={coin}&period=1h&limit=1"
            req = urllib.request.Request(url, headers={'User-Agent': 'TradingPlatform/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if data:
                    return data[-1]
        except Exception as e:
            logger.warning(f"Long/short ratio fetch failed: {e}")
        return None

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        scores = []
        reasons = []
        details = {}

        # 资金费率
        funding = self._fetch_funding_rate(symbol)
        if funding is not None:
            details['funding_rate'] = funding
            if funding > 0.001:  # > 0.1%
                scores.append(-0.5)
                reasons.append(f'资金费率偏高 ({funding*100:.3f}%)，多头拥挤')
            elif funding < -0.001:
                scores.append(0.5)
                reasons.append(f'资金费率偏负 ({funding*100:.3f}%)，空头拥挤')
            else:
                reasons.append(f'资金费率正常 ({funding*100:.3f}%)')

        # 多空比
        ls_ratio = self._fetch_long_short_ratio(symbol)
        if ls_ratio:
            ratio = float(ls_ratio.get('longShortRatio', 1))
            details['long_short_ratio'] = ratio
            if ratio > 2:
                scores.append(-0.6)
                reasons.append(f'多空比 {ratio:.2f}，多头过于拥挤')
            elif ratio < 0.5:
                scores.append(0.6)
                reasons.append(f'多空比 {ratio:.2f}，空头拥挤（潜在反弹）')
            elif ratio > 1.5:
                scores.append(-0.3)
                reasons.append(f'多空比 {ratio:.2f}，偏多')
            elif ratio < 0.7:
                scores.append(0.3)
                reasons.append(f'多空比 {ratio:.2f}，偏空')
            else:
                reasons.append(f'多空比 {ratio:.2f}，均衡')

        # 未平仓合约
        oi = self._fetch_oi(symbol)
        if oi:
            details['open_interest'] = oi.get('openInterest', 'N/A')

        # 如果什么都没拿到
        if not scores:
            return AgentOpinion(
                self.name, self.icon, 'hold', 30, self.vote_weight,
                '链上数据不可用（可能非 Binance 交易对）',
                details,
            )

        total = sum(scores) / len(scores)
        if total > 0.2:
            action = 'buy'
        elif total < -0.2:
            action = 'sell'
        else:
            action = 'hold'

        confidence = min(abs(total) * 100, 100)

        return AgentOpinion(
            agent_name=self.name, agent_icon=self.icon,
            action=action, confidence=confidence,
            weight=self.vote_weight,
            reasoning=' | '.join(reasons),
            details=details,
        )


# ================================================================
# Agent 4: 风控官
# ================================================================

class RiskAgent(BaseAgent):
    """
    风控官
    评估波动率、回撤、VaR、趋势强度
    反向思维：别人贪婪我恐惧
    """
    name = "风控官"
    icon = "🛡️"
    vote_weight = 0.15

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        n = len(df)
        if n < 30:
            return AgentOpinion(self.name, self.icon, 'hold', 0, self.vote_weight, '数据不足')

        scores = []
        reasons = []
        details = {}

        close = df['close']
        ret = close.pct_change().dropna()

        # 1. 波动率 (20日)
        vol_20 = ret.tail(20).std() * np.sqrt(365) * 100
        details['annual_volatility'] = round(vol_20, 1)

        vol_history = ret.rolling(20).std() * np.sqrt(365) * 100
        vol_percentile = (vol_history < vol_20).sum() / len(vol_history.dropna()) * 100

        if vol_20 > 100:
            scores.append(-0.5)
            reasons.append(f'年化波动率 {vol_20:.0f}% 极高（风险警示）')
        elif vol_20 > 60:
            scores.append(-0.2)
            reasons.append(f'年化波动率 {vol_20:.0f}% 偏高')
        elif vol_20 < 30:
            reasons.append(f'年化波动率 {vol_20:.0f}% 低波动')

        # 2. 最大回撤 (20日)
        peak = close.tail(20).cummax()
        drawdown = ((close.tail(20) - peak) / peak * 100).min()
        details['max_drawdown_20d'] = round(drawdown, 2)

        if drawdown < -15:
            scores.append(0.5)
            reasons.append(f'20日回撤 {drawdown:.1f}%，深跌后可能反弹')
        elif drawdown < -8:
            scores.append(0.2)
            reasons.append(f'20日回撤 {drawdown:.1f}%')

        # 3. VaR (95%)
        var_95 = np.percentile(ret.tail(100), 5) * 100
        details['VaR_95'] = round(var_95, 2)

        # 4. 趋势强度 (ADX 替代)
        high = df['high'].tail(20)
        low = df['low'].tail(20)
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        atr_20 = (high - low).rolling(14).mean().iloc[-1]
        plus_di = (plus_dm / atr_20 * 100).rolling(14).mean().iloc[-1] if atr_20 > 0 else 0
        minus_di = (minus_dm / atr_20 * 100).rolling(14).mean().iloc[-1] if atr_20 > 0 else 0
        trend_strength = abs(plus_di - minus_di)
        details['trend_strength'] = round(trend_strength, 1)

        if trend_strength < 15:
            scores.append(0)
            reasons.append(f'趋势弱 (强度{trend_strength:.0f})，建议观望')
        elif trend_strength > 40:
            reasons.append(f'趋势强 (强度{trend_strength:.0f})')

        # 5. 价格位置（是否在极端位置）
        high_60 = close.tail(60).max()
        low_60 = close.tail(60).min()
        pos_in_range = (close.iloc[-1] - low_60) / (high_60 - low_60) if (high_60 - low_60) > 0 else 0.5
        details['price_position_60d'] = round(pos_in_range, 2)

        if pos_in_range > 0.95:
            scores.append(-0.4)
            reasons.append(f'价格处于60日高位 ({pos_in_range:.0%})，追高风险大')
        elif pos_in_range < 0.05:
            scores.append(0.4)
            reasons.append(f'价格处于60日低位 ({pos_in_range:.0%})，超跌')

        if not scores:
            scores.append(0)
            reasons.append('风险指标均在正常范围')

        total = sum(scores) / len(scores)
        if total > 0.15:
            action = 'buy'
        elif total < -0.15:
            action = 'sell'
        else:
            action = 'hold'

        confidence = min(abs(total) * 100, 100)

        return AgentOpinion(
            agent_name=self.name, agent_icon=self.icon,
            action=action, confidence=confidence,
            weight=self.vote_weight,
            reasoning=' | '.join(reasons),
            details=details,
        )


# ================================================================
# Agent 5: 量化模型师 (XGBoost 多因子)
# ================================================================

class QuantModelAgent(BaseAgent):
    """
    量化模型师
    使用 XGBoost 多因子模型进行预测
    """
    name = "量化模型师"
    icon = "🧮"
    vote_weight = 0.20

    def analyze(self, df: pd.DataFrame, symbol: str, params: Dict) -> AgentOpinion:
        from engine.core import AIMultiFactorStrategy

        try:
            signals = AIMultiFactorStrategy.generate_signals(df, params)
        except Exception as e:
            return AgentOpinion(
                self.name, self.icon, 'hold', 0, self.vote_weight,
                f'模型运行失败: {e}',
            )

        if not signals:
            return AgentOpinion(
                self.name, self.icon, 'hold', 40, self.vote_weight,
                '模型无明确信号（概率在阈值之间）',
            )

        sig = signals[0]
        action = sig['type']  # buy/sell
        confidence = sig.get('confidence', 0.5) * 100

        return AgentOpinion(
            agent_name=self.name, agent_icon=self.icon,
            action=action, confidence=confidence,
            weight=self.vote_weight,
            reasoning=f'XGBoost 预测 {action}，置信度 {confidence:.0f}%',
            details={
                'model': 'XGBClassifier',
                'signal': action,
                'confidence': round(confidence, 1),
            },
        )


# ================================================================
# Agent 编排器 — 讨论投票系统
# ================================================================

class AgentOrchestrator:
    """
    多 Agent 协作编排器

    流程：
    1. 各 Agent 独立分析 → 生成意见
    2. 汇总投票 → 加权打分
    3. 生成最终决策 + 详细讨论记录
    """

    def __init__(self):
        self.agents: List[BaseAgent] = [
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

        # 1. 收集所有 Agent 意见
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

        # 2. 加权投票
        vote_result = self._weighted_vote(opinions)

        # 3. 构造完整报告
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
        """加权投票计算最终决策"""
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

        # 归一化
        if total_weight > 0:
            buy_score /= total_weight
            sell_score /= total_weight
            hold_score /= total_weight

        # 最终决策
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
            'vote_breakdown': self._vote_breakdown(opinions),
        }

    def _vote_breakdown(self, opinions: List[AgentOpinion]) -> List[Dict]:
        """投票明细"""
        return [
            {
                'agent': f'{op.agent_icon} {op.agent_name}',
                'vote': op.action,
                'confidence': round(op.confidence, 1),
                'effective_weight': round(op.weight * (op.confidence / 100) * 100, 1),
            }
            for op in opinions
        ]

    def _calc_consensus(self, opinions: List[AgentOpinion]) -> Dict:
        """共识度计算"""
        actions = [op.action for op in opinions]
        buy_count = actions.count('buy')
        sell_count = actions.count('sell')
        hold_count = actions.count('hold')
        total = len(actions)

        # 共识度：最多票数 / 总票数
        max_agreement = max(buy_count, sell_count, hold_count) / total * 100

        if max_agreement >= 80:
            level = '强共识'
        elif max_agreement >= 60:
            level = '中等共识'
        else:
            level = '分歧'

        return {
            'level': level,
            'agreement_pct': round(max_agreement, 1),
            'buy_votes': buy_count,
            'sell_votes': sell_count,
            'hold_votes': hold_count,
            'total_agents': total,
        }


# 单例
orchestrator = AgentOrchestrator()
