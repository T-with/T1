"""
MyTradingPlatform — NLP 情绪分析引擎
抓取 Twitter/Reddit/新闻 → LLM 分析 → 市场情绪指数

Phase 2: AI 增强层
- 多源新闻抓取 (CryptoPanic, Twitter/X, Reddit)
- LLM 情绪分析 (支持 DeepSeek/OpenAI/Kimi)
- 情绪指数聚合与趋势
- 鲸鱼钱包监控
- 链上数据整合
"""

import json
import time
import logging
import threading
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


# ================================================================
# 数据结构
# ================================================================

@dataclass
class SentimentItem:
    source: str = ""          # twitter / reddit / news / cryptopanic
    title: str = ""
    content: str = ""
    url: str = ""
    timestamp: float = 0.0
    sentiment: str = ""       # positive / negative / neutral
    score: float = 0.0        # -1.0 (极度悲观) to 1.0 (极度乐观)
    relevance: float = 0.0   # 0-1 相关度
    symbols: List[str] = field(default_factory=list)
    raw_data: Dict = field(default_factory=dict)


@dataclass
class SentimentSnapshot:
    symbol: str = ""
    timestamp: float = 0.0
    overall_score: float = 0.0       # -1 to 1
    overall_label: str = ""          # extreme_fear / fear / neutral / greed / extreme_greed
    news_score: float = 0.0
    social_score: float = 0.0
    onchain_score: float = 0.0
    item_count: int = 0
    top_positive: List[Dict] = field(default_factory=list)
    top_negative: List[Dict] = field(default_factory=list)
    trend_1h: float = 0.0            # 1 小时情绪变化
    trend_24h: float = 0.0           # 24 小时情绪变化


# ================================================================
# 新闻抓取器
# ================================================================

class NewsFetcher:
    """多源新闻/社交媒体抓取"""

    def __init__(self):
        self._cache: Dict[str, List[SentimentItem]] = {}
        self._last_fetch: Dict[str, float] = {}
        self._fetch_interval = 300  # 5分钟

    def fetch_all(self, symbol: str) -> List[SentimentItem]:
        """从所有源抓取"""
        items = []
        items.extend(self._fetch_cryptopanic(symbol))
        items.extend(self._fetch_reddit(symbol))
        items.extend(self._fetch_twitter_proxy(symbol))
        return items

    def _fetch_cryptopanic(self, symbol: str) -> List[SentimentItem]:
        """CryptoPanic API（免费）"""
        key = f"cryptopanic:{symbol}"
        if not self._should_fetch(key):
            return self._cache.get(key, [])

        coin = symbol.split('/')[0].upper()
        items = []

        try:
            import urllib.request
            url = f"https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories={coin}&sortOrder=popular"
            req = urllib.request.Request(url, headers={'User-Agent': 'MyTradingPlatform/2.0'})

            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            for article in data.get('Data', [])[:20]:
                item = SentimentItem(
                    source='news',
                    title=article.get('title', ''),
                    content=article.get('body', '')[:500],
                    url=article.get('url', ''),
                    timestamp=article.get('published_on', 0),
                    symbols=[coin],
                    raw_data={'source_name': article.get('source', ''), 'categories': article.get('categories', '')},
                )
                items.append(item)

        except Exception as e:
            logger.warning(f"CryptoCompare news fetch failed: {e}")

        self._cache[key] = items
        self._last_fetch[key] = time.time()
        return items

    def _fetch_reddit(self, symbol: str) -> List[SentimentItem]:
        """Reddit 公开 API"""
        key = f"reddit:{symbol}"
        if not self._should_fetch(key):
            return self._cache.get(key, [])

        coin = symbol.split('/')[0].lower()
        items = []

        try:
            import urllib.request
            subreddits = ['cryptocurrency', f'{coin}', 'CryptoMarkets']
            for sub in subreddits[:2]:
                url = f"https://www.reddit.com/r/{sub}/new.json?limit=10"
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'MyTradingPlatform/2.0 (crypto analysis)',
                })
                try:
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read())

                    for post in data.get('data', {}).get('children', []):
                        pd = post.get('data', {})
                        title = pd.get('title', '')
                        selftext = pd.get('selftext', '')[:300]

                        # 简单过滤：标题或内容包含币种名
                        if coin in title.lower() or coin in selftext.lower():
                            item = SentimentItem(
                                source='reddit',
                                title=title,
                                content=selftext,
                                url=f"https://reddit.com{pd.get('permalink', '')}",
                                timestamp=pd.get('created_utc', 0),
                                symbols=[coin.upper()],
                                raw_data={
                                    'subreddit': pd.get('subreddit', ''),
                                    'score': pd.get('score', 0),
                                    'num_comments': pd.get('num_comments', 0),
                                    'upvote_ratio': pd.get('upvote_ratio', 0),
                                },
                            )
                            items.append(item)
                except Exception:
                    continue

        except Exception as e:
            logger.warning(f"Reddit fetch failed: {e}")

        self._cache[key] = items
        self._last_fetch[key] = time.time()
        return items

    def _fetch_twitter_proxy(self, symbol: str) -> List[SentimentItem]:
        """
        Twitter/X 数据（通过公开聚合 API 获取）
        注意：直接 Twitter API 需要付费，这里用 CryptoCompare 的社交数据替代
        """
        key = f"social:{symbol}"
        if not self._should_fetch(key):
            return self._cache.get(key, [])

        coin = symbol.split('/')[0].upper()
        items = []

        try:
            import urllib.request
            # CryptoCompare 社交统计
            url = f"https://min-api.cryptocompare.com/data/social/coin/latest?coinId={coin}"
            req = urllib.request.Request(url, headers={'User-Agent': 'MyTradingPlatform/2.0'})

            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())

            if data and 'Data' in data:
                general = data['Data'].get('General', {})
                item = SentimentItem(
                    source='social_stats',
                    title=f"{coin} Social Stats",
                    content=json.dumps({
                        'twitter_followers': general.get('Twitter', {}).get('followers', 0),
                        'reddit_subscribers': general.get('Reddit', {}).get('subscribers', 0),
                        'reddit_active': general.get('Reddit', {}).get('active_accounts', 0),
                        'code_repo_stars': general.get('CodeRepository', {}).get('Stars', 0),
                        'code_repo_issues': general.get('CodeRepository', {}).get('Issues', 0),
                    }),
                    timestamp=time.time(),
                    symbols=[coin],
                    raw_data=data['Data'],
                )
                items.append(item)

        except Exception as e:
            logger.warning(f"Social stats fetch failed: {e}")

        self._cache[key] = items
        self._last_fetch[key] = time.time()
        return items

    def _should_fetch(self, key: str) -> bool:
        last = self._last_fetch.get(key, 0)
        return time.time() - last > self._fetch_interval


# ================================================================
# LLM 情绪分析器
# ================================================================

class SentimentAnalyzer:
    """使用 LLM 进行情绪分析"""

    def __init__(self, llm_client=None):
        """
        llm_client: 可选自定义 LLM 客户端，否则使用 engine.llm
        """
        self._llm = llm_client
        self._fallback_sentiment = self._rule_based_sentiment

    def _get_llm(self):
        if self._llm is None:
            from engine.llm import chat_json
            self._llm = chat_json
        return self._llm

    def analyze_batch(self, items: List[SentimentItem],
                      symbol: str = "") -> List[SentimentItem]:
        """批量分析情绪（使用 LLM）"""
        if not items:
            return items

        # 构造批量分析 prompt
        texts = []
        for i, item in enumerate(items[:20]):  # 最多 20 条
            texts.append(f"[{i}] ({item.source}) {item.title}: {item.content[:200]}")

        prompt = f"""分析以下关于 {symbol} 的新闻/社交媒体内容的情绪。每条给出:
- sentiment: "positive" / "negative" / "neutral"
- score: -1.0 (极度悲观) 到 1.0 (极度乐观)

内容:
{chr(10).join(texts)}

以 JSON 格式回复:
{{
  "analyses": [
    {{"index": 0, "sentiment": "positive", "score": 0.7, "reasoning": "简短理由"}},
    ...
  ]
}}"""

        try:
            chat_fn = self._get_llm()
            result = chat_fn(
                messages=[
                    {'role': 'system', 'content': '你是加密货币市场情绪分析师。分析新闻和社交媒体内容的市场情绪。只回复 JSON。'},
                    {'role': 'user', 'content': prompt},
                ],
                temperature=0.1,
            )

            analyses = result.get('analyses', [])
            for a in analyses:
                idx = a.get('index', -1)
                if 0 <= idx < len(items):
                    items[idx].sentiment = a.get('sentiment', 'neutral')
                    items[idx].score = float(a.get('score', 0))

        except Exception as e:
            logger.warning(f"LLM sentiment analysis failed, using rule-based: {e}")
            # Fallback: 基于规则的情绪分析
            for item in items:
                item.sentiment, item.score = self._rule_based_sentiment(item)

        return items

    @staticmethod
    def _rule_based_sentiment(item: SentimentItem) -> Tuple[str, float]:
        """基于关键词的简单情绪判断（LLM 不可用时的 fallback）"""
        text = (item.title + ' ' + item.content).lower()

        positive_words = ['bull', 'surge', 'rally', 'breakout', 'moon', 'all-time high',
                         'adoption', 'partnership', 'upgrade', 'launch', 'approval',
                         '上涨', '突破', '利好', '牛市', '买入', '看涨']
        negative_words = ['bear', 'crash', 'dump', 'hack', 'scam', 'ban', 'regulation',
                         'sell-off', 'liquidation', 'hack', 'exploit', 'rug',
                         '下跌', '暴跌', '崩盘', '利空', '熊市', '卖出', '看跌']

        pos_count = sum(1 for w in positive_words if w in text)
        neg_count = sum(1 for w in negative_words if w in text)

        total = pos_count + neg_count
        if total == 0:
            return 'neutral', 0.0

        score = (pos_count - neg_count) / total
        if score > 0.2:
            return 'positive', min(score, 1.0)
        elif score < -0.2:
            return 'negative', max(score, -1.0)
        else:
            return 'neutral', 0.0


# ================================================================
# 链上数据监控
# ================================================================

class OnChainMonitor:
    """链上数据监控：资金费率、多空比、大额转账"""

    def __init__(self):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._fetch_interval = 120  # 2分钟

    def get_funding_rate(self, symbol: str) -> Optional[Dict]:
        """获取资金费率"""
        key = f"funding:{symbol}"
        cached = self._get_cache(key)
        if cached:
            return cached

        coin = symbol.replace('/', '').upper()
        try:
            import urllib.request
            url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={coin}&limit=5"
            req = urllib.request.Request(url, headers={'User-Agent': 'MyTradingPlatform/2.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())

            result = {
                'symbol': symbol,
                'rates': [],
                'latest_rate': 0,
                'avg_rate_24h': 0,
            }
            for item in data:
                rate = float(item['fundingRate'])
                ts = datetime.fromtimestamp(item['fundingTime'] / 1000)
                result['rates'].append({
                    'rate': rate,
                    'rate_pct': round(rate * 100, 4),
                    'time': ts.strftime('%m-%d %H:%M'),
                })

            if result['rates']:
                result['latest_rate'] = result['rates'][-1]['rate']
                result['avg_rate_24h'] = np.mean([r['rate'] for r in result['rates']])

            self._set_cache(key, result)
            return result

        except Exception as e:
            logger.warning(f"Funding rate fetch failed: {e}")
            return None

    def get_long_short_ratio(self, symbol: str) -> Optional[Dict]:
        """获取多空比"""
        key = f"lsratio:{symbol}"
        cached = self._get_cache(key)
        if cached:
            return cached

        coin = symbol.replace('/', '').upper()
        try:
            import urllib.request
            url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={coin}&period=1h&limit=10"
            req = urllib.request.Request(url, headers={'User-Agent': 'MyTradingPlatform/2.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())

            ratios = []
            for item in data:
                ts = datetime.fromtimestamp(int(item['timestamp']) / 1000)
                ratios.append({
                    'ratio': float(item['longShortRatio']),
                    'long_pct': float(item['longAccount']) * 100,
                    'short_pct': float(item['shortAccount']) * 100,
                    'time': ts.strftime('%m-%d %H:%M'),
                })

            result = {
                'symbol': symbol,
                'ratios': ratios,
                'latest_ratio': ratios[-1]['ratio'] if ratios else 0,
                'trend': 'long_heavy' if (ratios[-1]['ratio'] if ratios else 1) > 1.2
                         else 'short_heavy' if (ratios[-1]['ratio'] if ratios else 1) < 0.8
                         else 'balanced',
            }

            self._set_cache(key, result)
            return result

        except Exception as e:
            logger.warning(f"Long/short ratio fetch failed: {e}")
            return None

    def get_open_interest(self, symbol: str) -> Optional[Dict]:
        """获取未平仓合约"""
        key = f"oi:{symbol}"
        cached = self._get_cache(key)
        if cached:
            return cached

        coin = symbol.replace('/', '').upper()
        try:
            import urllib.request
            url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={coin}"
            req = urllib.request.Request(url, headers={'User-Agent': 'MyTradingPlatform/2.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())

            result = {
                'symbol': symbol,
                'open_interest': float(data.get('openInterest', 0)),
                'timestamp': data.get('timestamp', 0),
            }

            self._set_cache(key, result)
            return result

        except Exception as e:
            logger.warning(f"Open interest fetch failed: {e}")
            return None

    def get_full_report(self, symbol: str) -> Dict:
        """获取完整的链上数据报告"""
        return {
            'funding_rate': self.get_funding_rate(symbol),
            'long_short_ratio': self.get_long_short_ratio(symbol),
            'open_interest': self.get_open_interest(symbol),
            'timestamp': datetime.now().isoformat(),
        }

    def _get_cache(self, key: str):
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < self._fetch_interval:
                return data
        return None

    def _set_cache(self, key: str, data):
        self._cache[key] = (time.time(), data)


# ================================================================
# 情绪指数管理器
# ================================================================

class SentimentEngine:
    """
    情绪指数管理器
    整合新闻 + 社交 + 链上数据，生成综合情绪指数
    """

    def __init__(self, llm_client=None):
        self.news_fetcher = NewsFetcher()
        self.analyzer = SentimentAnalyzer(llm_client)
        self.onchain = OnChainMonitor()

        # 历史快照（用于趋势计算）
        self._history: Dict[str, deque] = {}  # symbol -> deque of snapshots
        self._lock = threading.Lock()

    def analyze(self, symbol: str) -> SentimentSnapshot:
        """完整情绪分析"""
        snapshot = SentimentSnapshot(
            symbol=symbol,
            timestamp=time.time(),
        )

        # 1. 抓取新闻/社交数据
        items = self.news_fetcher.fetch_all(symbol)
        snapshot.item_count = len(items)

        # 2. LLM 情绪分析
        items = self.analyzer.analyze_batch(items, symbol)

        # 3. 计算各维度分数
        news_items = [i for i in items if i.source in ('news', 'cryptopanic')]
        social_items = [i for i in items if i.source in ('reddit', 'twitter', 'social_stats')]

        snapshot.news_score = self._avg_score(news_items)
        snapshot.social_score = self._avg_score(social_items)

        # 4. 链上数据评分
        onchain_report = self.onchain.get_full_report(symbol)
        snapshot.onchain_score = self._score_onchain(onchain_report)

        # 5. 综合评分
        weights = {'news': 0.35, 'social': 0.35, 'onchain': 0.30}
        snapshot.overall_score = (
            snapshot.news_score * weights['news'] +
            snapshot.social_score * weights['social'] +
            snapshot.onchain_score * weights['onchain']
        )

        # 6. 标签
        snapshot.overall_label = self._score_to_label(snapshot.overall_score)

        # 7. Top positive/negative
        sorted_items = sorted(items, key=lambda x: x.score, reverse=True)
        snapshot.top_positive = [
            {'title': i.title, 'source': i.source, 'score': round(i.score, 2)}
            for i in sorted_items[:3] if i.score > 0
        ]
        snapshot.top_negative = [
            {'title': i.title, 'source': i.source, 'score': round(i.score, 2)}
            for i in sorted_items[-3:] if i.score < 0
        ]
        snapshot.top_negative.reverse()

        # 8. 趋势计算
        self._update_history(symbol, snapshot)
        snapshot.trend_1h = self._calc_trend(symbol, hours=1)
        snapshot.trend_24h = self._calc_trend(symbol, hours=24)

        return snapshot

    def get_sentiment_index(self, symbol: str) -> Dict:
        """获取简化的恐惧贪婪指数"""
        snapshot = self.analyze(symbol) if hasattr(self, 'analyze') else None
        if snapshot is None:
            snapshot = self.analyze(symbol)

        # 转换为 0-100 指数
        index = int((snapshot.overall_score + 1) * 50)

        return {
            'symbol': symbol,
            'index': max(0, min(100, index)),
            'label': snapshot.overall_label,
            'label_cn': {
                'extreme_fear': '极度恐惧',
                'fear': '恐惧',
                'neutral': '中性',
                'greed': '贪婪',
                'extreme_greed': '极度贪婪',
            }.get(snapshot.overall_label, '未知'),
            'components': {
                'news': round(snapshot.news_score, 2),
                'social': round(snapshot.social_score, 2),
                'onchain': round(snapshot.onchain_score, 2),
            },
            'trend': {
                '1h': round(snapshot.trend_1h, 3),
                '24h': round(snapshot.trend_24h, 3),
            },
            'item_count': snapshot.item_count,
        }

    def _avg_score(self, items: List[SentimentItem]) -> float:
        if not items:
            return 0.0
        scores = [i.score for i in items if i.score != 0]
        return float(np.mean(scores)) if scores else 0.0

    def _score_onchain(self, report: Dict) -> float:
        """从链上数据计算情绪分数"""
        score = 0.0
        factors = 0

        # 资金费率：正 = 多头拥挤（偏负面），负 = 空头拥挤（偏正面）
        fr = report.get('funding_rate')
        if fr and fr.get('latest_rate') is not None:
            rate = fr['latest_rate']
            # 极端费率通常预示反转
            if abs(rate) > 0.001:  # >0.1%
                score -= np.sign(rate) * 0.3  # 反向信号
            factors += 1

        # 多空比：>1.2 偏多（可能转负面），<0.8 偏空（可能转正面）
        ls = report.get('long_short_ratio')
        if ls and ls.get('latest_ratio') is not None:
            ratio = ls['latest_ratio']
            if ratio > 1.5:
                score -= 0.3  # 过度看多，反转信号
            elif ratio < 0.6:
                score += 0.3  # 过度看空，反转信号
            factors += 1

        return score / max(factors, 1)

    @staticmethod
    def _score_to_label(score: float) -> str:
        if score >= 0.5:
            return 'extreme_greed'
        elif score >= 0.2:
            return 'greed'
        elif score >= -0.2:
            return 'neutral'
        elif score >= -0.5:
            return 'fear'
        else:
            return 'extreme_fear'

    def _update_history(self, symbol: str, snapshot: SentimentSnapshot):
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=1440)  # 保留最多 24h（每分钟一个）
        self._history[symbol].append({
            'time': snapshot.timestamp,
            'score': snapshot.overall_score,
        })

    def _calc_trend(self, symbol: str, hours: int = 1) -> float:
        history = self._history.get(symbol, deque())
        if len(history) < 2:
            return 0.0

        now = time.time()
        cutoff = now - hours * 3600
        recent = [h for h in history if h['time'] >= cutoff]

        if len(recent) < 2:
            return 0.0

        return recent[-1]['score'] - recent[0]['score']


# ================================================================
# 全局单例
# ================================================================

sentiment_engine = SentimentEngine()
