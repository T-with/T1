"""
MyTradingPlatform — 强化学习交易代理
PPO (Proximal Policy Optimization) Actor-Critic

Phase 6: AI 增强层
- 自定义交易环境 (Gymnasium 兼容)
- PPO Actor-Critic 网络
- 奖励函数: 风险调整收益 (Sharpe-like)
- Walk-Forward 训练
- 模型持久化
"""

import os
import json
import time
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime

import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions import Categorical

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
RL_MODEL_DIR = BASE_DIR / 'data' / 'rl_models'
RL_MODEL_DIR.mkdir(parents=True, exist_ok=True)

DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# ================================================================
# 交易环境
# ================================================================

class TradingEnvironment:
    """
    自定义交易环境

    State (观测空间):
    - 市场特征 (N 维): 收益率/RSI/MACD/BB/波动率/量比 等
    - 持仓状态 (3 维): [是否有仓位, 当前盈亏%, 持仓时长占比]

    Action (动作空间):
    - 0: Hold (持有/观望)
    - 1: Buy (开多/加仓)
    - 2: Sell (平仓/开空)

    Reward:
    - 每步对数收益 + 夏普惩罚 + 回撤惩罚
    """

    def __init__(self, df: pd.DataFrame, feature_cols: List[str],
                 initial_capital: float = 10000,
                 commission: float = 0.0004,
                 slippage: float = 0.0005,
                 max_steps: int = 0,
                 reward_type: str = 'sharpe'):
        """
        Args:
            df: 带有特征的 DataFrame
            feature_cols: 特征列名列表
            initial_capital: 初始资金
            commission: 手续费率
            slippage: 滑点率
            max_steps: 最大步数 (0=全部)
            reward_type: reward 计算方式 ('simple', 'sharpe', 'risk_adjusted')
        """
        self.df = df.reset_index(drop=True)
        self.feature_cols = feature_cols
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        self.reward_type = reward_type

        # 预计算特征矩阵
        feat_data = self.df[feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0).values
        # Z-score 标准化
        self.feat_mean = np.nanmean(feat_data, axis=0)
        self.feat_std = np.nanstd(feat_data, axis=0)
        self.feat_std[self.feat_std == 0] = 1
        self.features = (feat_data - self.feat_mean) / self.feat_std

        self.prices = self.df['close'].values.astype(float)
        self.max_steps = max_steps or (len(self.df) - 1)

        # 状态维度: 特征数 + 3 (持仓状态)
        self.state_dim = len(feature_cols) + 3
        self.action_dim = 3  # hold, buy, sell

        # 运行时状态
        self.reset()

    def reset(self) -> np.ndarray:
        """重置环境"""
        self.step_idx = 0
        self.cash = self.initial_capital
        self.position = 0.0        # 持仓数量
        self.position_side = 0      # 0=无仓位, 1=多头, -1=空头
        self.entry_price = 0.0
        self.equity = self.initial_capital
        self.peak_equity = self.initial_capital
        self.max_drawdown = 0.0

        # 历史记录
        self.trade_log = []
        self.equity_curve = [self.initial_capital]
        self.returns = []
        self.action_history = []

        return self._get_state()

    def step(self, action: int) -> Tuple[np.ndarray, float, bool, Dict]:
        """
        执行一步

        Returns:
            state, reward, done, info
        """
        if self.step_idx >= self.max_steps:
            return self._get_state(), 0.0, True, self._get_info()

        current_price = self.prices[self.step_idx]
        prev_equity = self.equity

        # 执行动作
        self._execute_action(action, current_price)

        # 推进一步
        self.step_idx += 1

        # 更新权益
        if self.step_idx < len(self.prices):
            new_price = self.prices[self.step_idx]
            self._update_equity(new_price)

        # 计算奖励
        reward = self._compute_reward(prev_equity, action)

        # 记录
        self.equity_curve.append(self.equity)
        ret = (self.equity / prev_equity - 1) if prev_equity > 0 else 0
        self.returns.append(ret)
        self.action_history.append(action)

        # 是否结束
        done = (self.step_idx >= self.max_steps or
                self.equity <= self.initial_capital * 0.5)  # 亏 50% 强制结束

        return self._get_state(), reward, done, self._get_info()

    def _get_state(self) -> np.ndarray:
        """获取当前状态向量"""
        if self.step_idx >= len(self.features):
            idx = len(self.features) - 1
        else:
            idx = self.step_idx

        market = self.features[idx]

        # 持仓状态
        has_pos = float(self.position_side != 0)
        if self.entry_price > 0 and self.position_side != 0:
            pnl_pct = (self.prices[min(self.step_idx, len(self.prices)-1)] / self.entry_price - 1)
            if self.position_side == -1:
                pnl_pct = -pnl_pct
        else:
            pnl_pct = 0.0
        hold_duration = min(self.step_idx / self.max_steps, 1.0)

        pos_state = np.array([has_pos, pnl_pct, hold_duration], dtype=np.float32)
        state = np.concatenate([market, pos_state]).astype(np.float32)
        return state

    def _execute_action(self, action: int, price: float):
        """执行交易动作"""
        if action == 1:  # Buy
            if self.position_side == 0:
                # 开多
                buy_price = price * (1 + self.slippage)
                size = self.cash / buy_price * 0.95  # 留 5% 余量
                cost = size * buy_price * (1 + self.commission)
                if cost <= self.cash:
                    self.cash -= cost
                    self.position = size
                    self.position_side = 1
                    self.entry_price = buy_price
                    self.trade_log.append({
                        'action': 'buy', 'price': buy_price,
                        'size': size, 'step': self.step_idx,
                    })

        elif action == 2:  # Sell
            if self.position_side == 1:
                # 平多
                sell_price = price * (1 - self.slippage)
                revenue = self.position * sell_price * (1 - self.commission)
                self.cash += revenue
                pnl = revenue - self.position * self.entry_price
                self.trade_log.append({
                    'action': 'sell', 'price': sell_price,
                    'size': self.position, 'pnl': pnl, 'step': self.step_idx,
                })
                self.position = 0.0
                self.position_side = 0
                self.entry_price = 0.0

    def _update_equity(self, price: float):
        """更新权益"""
        self.equity = self.cash
        if self.position_side == 1:
            self.equity += self.position * price
        elif self.position_side == -1:
            self.equity += self.position * (2 * self.entry_price - price)

        self.peak_equity = max(self.peak_equity, self.equity)
        dd = (self.peak_equity - self.equity) / self.peak_equity if self.peak_equity > 0 else 0
        self.max_drawdown = max(self.max_drawdown, dd)

    def _compute_reward(self, prev_equity: float, action: int) -> float:
        """计算奖励"""
        if prev_equity <= 0:
            return 0.0

        step_return = (self.equity / prev_equity - 1)

        if self.reward_type == 'simple':
            return step_return * 100  # 缩放到合理范围

        elif self.reward_type == 'sharpe':
            # Sharpe-like reward
            if len(self.returns) > 1:
                recent = self.returns[-20:]
                mean_ret = np.mean(recent)
                std_ret = np.std(recent) + 1e-8
                sharpe = mean_ret / std_ret
            else:
                sharpe = 0

            # 组合奖励
            reward = step_return * 100 + sharpe * 0.1

            # 回撤惩罚
            if self.equity < self.peak_equity:
                dd = (self.peak_equity - self.equity) / self.peak_equity
                reward -= dd * 5

            return reward

        else:  # risk_adjusted
            reward = step_return * 100

            # 大幅亏损惩罚
            if step_return < -0.01:
                reward -= abs(step_return) * 50

            # 频繁交易惩罚
            if action != 0:
                reward -= 0.01  # 小额交易成本

            return reward

    def _get_info(self) -> Dict:
        """获取环境信息"""
        wins = [t for t in self.trade_log if t.get('pnl', 0) > 0]
        losses = [t for t in self.trade_log if t.get('pnl', 0) <= 0]

        return {
            'equity': round(self.equity, 2),
            'total_return_pct': round((self.equity / self.initial_capital - 1) * 100, 2),
            'max_drawdown_pct': round(self.max_drawdown * 100, 2),
            'total_trades': len([t for t in self.trade_log if t['action'] == 'sell']),
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'win_rate': round(len(wins) / max(len(wins) + len(losses), 1) * 100, 1),
            'steps': self.step_idx,
        }


# ================================================================
# PPO Agent
# ================================================================

class ActorCritic(nn.Module):
    """PPO Actor-Critic 网络"""

    def __init__(self, state_dim: int, action_dim: int, hidden: int = 128):
        super().__init__()
        # 共享特征层
        self.shared = nn.Sequential(
            nn.Linear(state_dim, hidden),
            nn.Tanh(),
            nn.Linear(hidden, hidden),
            nn.Tanh(),
        )
        # Actor (策略头)
        self.actor = nn.Sequential(
            nn.Linear(hidden, hidden // 2),
            nn.Tanh(),
            nn.Linear(hidden // 2, action_dim),
        )
        # Critic (价值头)
        self.critic = nn.Sequential(
            nn.Linear(hidden, hidden // 2),
            nn.Tanh(),
            nn.Linear(hidden // 2, 1),
        )

    def forward(self, state):
        features = self.shared(state)
        action_logits = self.actor(features)
        value = self.critic(features)
        return action_logits, value

    def act(self, state):
        """选择动作 (带探索)"""
        if state.dim() == 1:
            state = state.unsqueeze(0)
        logits, value = self.forward(state)
        dist = Categorical(logits=logits)
        action = dist.sample()
        return action.item(), dist.log_prob(action), value

    def evaluate(self, states, actions):
        """评估动作 (训练用)"""
        logits, values = self.forward(states)
        dist = Categorical(logits=logits)
        log_probs = dist.log_prob(actions)
        entropy = dist.entropy()
        return log_probs, values.squeeze(-1), entropy


class RolloutBuffer:
    """PPO 经验回放缓冲区"""

    def __init__(self):
        self.states = []
        self.actions = []
        self.rewards = []
        self.log_probs = []
        self.values = []
        self.dones = []

    def push(self, state, action, reward, log_prob, value, done):
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.log_probs.append(log_prob)
        self.values.append(value)
        self.dones.append(done)

    def clear(self):
        self.states.clear()
        self.actions.clear()
        self.rewards.clear()
        self.log_probs.clear()
        self.values.clear()
        self.dones.clear()

    def compute_returns(self, gamma: float = 0.99, lam: float = 0.95):
        """计算 GAE (Generalized Advantage Estimation)"""
        rewards = np.array(self.rewards)
        values = np.array([v.item() if hasattr(v, 'item') else v for v in self.values])
        dones = np.array(self.dones, dtype=float)

        # GAE
        advantages = np.zeros_like(rewards)
        gae = 0
        for t in reversed(range(len(rewards))):
            if t == len(rewards) - 1:
                next_value = 0
            else:
                next_value = values[t + 1]
            delta = rewards[t] + gamma * next_value * (1 - dones[t]) - values[t]
            gae = delta + gamma * lam * (1 - dones[t]) * gae
            advantages[t] = gae

        returns = advantages + values
        return returns, advantages

    def get_tensors(self):
        """转换为 tensor"""
        states = torch.FloatTensor(np.array(self.states)).to(DEVICE)
        actions = torch.LongTensor(self.actions).to(DEVICE)
        log_probs = torch.FloatTensor([lp.item() if hasattr(lp, 'item') else lp
                                        for lp in self.log_probs]).to(DEVICE)
        return states, actions, log_probs


class PPOAgent:
    """
    PPO (Proximal Policy Optimization) 交易代理

    特点:
    - Clipped surrogate objective
    - GAE 优势估计
    - 熵正则化 (鼓励探索)
    - 多 epoch 小批量更新
    """

    def __init__(self, state_dim: int, action_dim: int = 3,
                 hidden_size: int = 128,
                 lr: float = 3e-4,
                 gamma: float = 0.99,
                 gae_lambda: float = 0.95,
                 clip_eps: float = 0.2,
                 entropy_coef: float = 0.01,
                 value_coef: float = 0.5,
                 max_grad_norm: float = 0.5,
                 ppo_epochs: int = 4,
                 batch_size: int = 64):

        self.state_dim = state_dim
        self.action_dim = action_dim
        self.gamma = gamma
        self.gae_lambda = gae_lambda
        self.clip_eps = clip_eps
        self.entropy_coef = entropy_coef
        self.value_coef = value_coef
        self.max_grad_norm = max_grad_norm
        self.ppo_epochs = ppo_epochs
        self.batch_size = batch_size

        self.actor_critic = ActorCritic(state_dim, action_dim, hidden_size).to(DEVICE)
        self.optimizer = optim.Adam(self.actor_critic.parameters(), lr=lr)
        self.buffer = RolloutBuffer()

        # 训练统计
        self.training_stats = {
            'episodes': 0,
            'total_steps': 0,
            'avg_reward': 0,
            'avg_return': 0,
        }

    def select_action(self, state: np.ndarray) -> Tuple[int, Any, Any]:
        """选择动作"""
        state_tensor = torch.FloatTensor(state).to(DEVICE)
        with torch.no_grad():
            action, log_prob, value = self.actor_critic.act(state_tensor)
        return action, log_prob, value

    def store_transition(self, state, action, reward, log_prob, value, done):
        """存储经验"""
        self.buffer.push(state, action, reward, log_prob, value, done)

    def update(self) -> Dict:
        """PPO 更新"""
        if len(self.buffer.states) < self.batch_size:
            return {'loss': 0, 'policy_loss': 0, 'value_loss': 0}

        returns, advantages = self.buffer.compute_returns(self.gamma, self.gae_lambda)
        states, actions, old_log_probs = self.buffer.get_tensors()

        returns = torch.FloatTensor(returns).to(DEVICE)
        advantages = torch.FloatTensor(advantages).to(DEVICE)

        # 标准化优势
        if advantages.std() > 0:
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

        total_loss = 0
        total_policy_loss = 0
        total_value_loss = 0
        n_updates = 0

        # PPO 多 epoch 更新
        dataset_size = len(states)
        for _ in range(self.ppo_epochs):
            indices = np.random.permutation(dataset_size)
            for start in range(0, dataset_size, self.batch_size):
                end = min(start + self.batch_size, dataset_size)
                batch_idx = indices[start:end]

                batch_states = states[batch_idx]
                batch_actions = actions[batch_idx]
                batch_old_lp = old_log_probs[batch_idx]
                batch_returns = returns[batch_idx]
                batch_adv = advantages[batch_idx]

                # 评估
                new_log_probs, new_values, entropy = self.actor_critic.evaluate(
                    batch_states, batch_actions
                )

                # PPO Clipped Loss
                ratio = torch.exp(new_log_probs - batch_old_lp)
                surr1 = ratio * batch_adv
                surr2 = torch.clamp(ratio, 1 - self.clip_eps, 1 + self.clip_eps) * batch_adv
                policy_loss = -torch.min(surr1, surr2).mean()

                # Value Loss
                value_loss = nn.functional.mse_loss(new_values, batch_returns)

                # Entropy Bonus
                entropy_loss = -entropy.mean()

                # 总损失
                loss = (policy_loss +
                        self.value_coef * value_loss +
                        self.entropy_coef * entropy_loss)

                self.optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.actor_critic.parameters(), self.max_grad_norm)
                self.optimizer.step()

                total_loss += loss.item()
                total_policy_loss += policy_loss.item()
                total_value_loss += value_loss.item()
                n_updates += 1

        self.buffer.clear()

        return {
            'loss': round(total_loss / max(n_updates, 1), 4),
            'policy_loss': round(total_policy_loss / max(n_updates, 1), 4),
            'value_loss': round(total_value_loss / max(n_updates, 1), 4),
        }

    def train_on_env(self, env: TradingEnvironment,
                     n_episodes: int = 100,
                     steps_per_episode: int = 0,
                     verbose: bool = True) -> Dict:
        """
        在环境中训练

        Returns: {episodes, avg_reward, avg_return, best_return, ...}
        """
        episode_rewards = []
        episode_returns = []
        episode_infos = []
        best_return = -float('inf')
        best_state = None

        steps_per_episode = steps_per_episode or env.max_steps

        for ep in range(n_episodes):
            state = env.reset()
            episode_reward = 0
            done = False
            steps = 0

            while not done and steps < steps_per_episode:
                action, log_prob, value = self.select_action(state)
                next_state, reward, done, info = env.step(action)
                self.store_transition(state, action, reward, log_prob, value, done)
                state = next_state
                episode_reward += reward
                steps += 1

            # PPO 更新
            update_info = self.update()

            total_return = info.get('total_return_pct', 0)
            episode_rewards.append(episode_reward)
            episode_returns.append(total_return)
            episode_infos.append(info)

            # 保存最佳模型
            if total_return > best_return:
                best_return = total_return
                best_state = {k: v.cpu().clone() for k, v in
                             self.actor_critic.state_dict().items()}

            if verbose and (ep + 1) % max(1, n_episodes // 10) == 0:
                avg_ret = np.mean(episode_returns[-10:])
                avg_reward = np.mean(episode_rewards[-10:])
                logger.info(f"  Episode {ep+1}/{n_episodes} "
                           f"avg_reward={avg_reward:.2f} avg_return={avg_ret:.2f}% "
                           f"loss={update_info['loss']:.4f}")

        # 恢复最佳模型
        if best_state:
            self.actor_critic.load_state_dict(best_state)

        self.training_stats['episodes'] += n_episodes
        self.training_stats['avg_reward'] = round(float(np.mean(episode_rewards)), 2)
        self.training_stats['avg_return'] = round(float(np.mean(episode_returns)), 2)

        final_info = episode_infos[-1] if episode_infos else {}

        return {
            'status': 'ok',
            'episodes': n_episodes,
            'avg_reward': round(float(np.mean(episode_rewards)), 2),
            'avg_return_pct': round(float(np.mean(episode_returns)), 2),
            'best_return_pct': round(best_return, 2),
            'final_info': final_info,
            'state_dim': self.state_dim,
            'action_dim': self.action_dim,
        }

    def predict(self, state: np.ndarray) -> Dict:
        """在给定状态下预测最优动作"""
        state_tensor = torch.FloatTensor(state).to(DEVICE)
        self.actor_critic.eval()
        with torch.no_grad():
            logits, value = self.actor_critic(state_tensor.unsqueeze(0))
            probs = torch.softmax(logits, dim=-1).squeeze()
            action = torch.argmax(probs).item()

        action_names = {0: 'hold', 1: 'buy', 2: 'sell'}
        return {
            'action': action,
            'action_name': action_names[action],
            'probabilities': {
                'hold': round(probs[0].item(), 3),
                'buy': round(probs[1].item(), 3),
                'sell': round(probs[2].item(), 3),
            },
            'value': round(value.item(), 4),
        }

    # ---- 持久化 ----

    def save(self, key: str):
        safe_key = key.replace('/', '_').replace(':', '_')
        path = RL_MODEL_DIR / f"{safe_key}.pt"
        meta_path = RL_MODEL_DIR / f"{safe_key}.json"

        torch.save(self.actor_critic.state_dict(), path)
        meta = {
            'state_dim': self.state_dim,
            'action_dim': self.action_dim,
            'training_stats': self.training_stats,
            'saved_at': datetime.now().isoformat(),
        }
        meta_path.write_text(json.dumps(meta, indent=2))
        logger.info(f"RL model saved: {key}")

    def load(self, key: str) -> bool:
        safe_key = key.replace('/', '_').replace(':', '_')
        path = RL_MODEL_DIR / f"{safe_key}.pt"
        if not path.exists():
            return False
        try:
            self.actor_critic.load_state_dict(
                torch.load(path, map_location=DEVICE, weights_only=True)
            )
            self.actor_critic.eval()
            meta_path = RL_MODEL_DIR / f"{safe_key}.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
                self.training_stats = meta.get('training_stats', {})
            logger.info(f"RL model loaded: {key}")
            return True
        except Exception as e:
            logger.error(f"RL model load failed: {e}")
            return False


# ================================================================
# RL 策略管理器
# ================================================================

class RLStrategyManager:
    """
    RL 策略管理器
    管理训练、预测、模型持久化
    """

    def __init__(self):
        self._agents: Dict[str, PPOAgent] = {}
        self._feature_cols: Dict[str, List[str]] = {}

    def _key(self, symbol: str, timeframe: str) -> str:
        return f"rl:{symbol}:{timeframe}"

    def train(self, df: pd.DataFrame, symbol: str = 'BTC/USDT',
              timeframe: str = '1h', n_episodes: int = 100,
              hidden_size: int = 128, lr: float = 3e-4,
              commission: float = 0.0004, **kwargs) -> Dict:
        """训练 RL 代理"""
        from engine.models import FeatureEngineer

        # 特征工程
        fdf, feature_cols = FeatureEngineer.compute_features(df)
        key = self._key(symbol, timeframe)

        # 过滤掉 NaN 太多的行
        valid_mask = fdf[feature_cols].notna().sum(axis=1) > len(feature_cols) * 0.5
        fdf = fdf[valid_mask].reset_index(drop=True)

        if len(fdf) < 200:
            return {'status': 'insufficient_data', 'bars': len(fdf)}

        # 创建环境
        env = TradingEnvironment(
            df=fdf, feature_cols=feature_cols,
            commission=commission,
            reward_type=kwargs.get('reward_type', 'sharpe'),
        )

        # 创建代理
        agent = PPOAgent(
            state_dim=env.state_dim,
            action_dim=env.action_dim,
            hidden_size=hidden_size,
            lr=lr,
            gamma=kwargs.get('gamma', 0.99),
            gae_lambda=kwargs.get('gae_lambda', 0.95),
            clip_eps=kwargs.get('clip_eps', 0.2),
            ppo_epochs=kwargs.get('ppo_epochs', 4),
        )

        logger.info(f"Training RL agent: {symbol} {timeframe} "
                    f"state_dim={env.state_dim} episodes={n_episodes}")

        result = agent.train_on_env(env, n_episodes=n_episodes)

        # 保存
        self._agents[key] = agent
        self._feature_cols[key] = feature_cols
        agent.save(key)

        result['symbol'] = symbol
        result['timeframe'] = timeframe
        result['state_dim'] = env.state_dim
        result['n_features'] = len(feature_cols)

        return result

    def predict(self, df: pd.DataFrame, symbol: str = 'BTC/USDT',
                timeframe: str = '1h') -> Dict:
        """使用训练好的 RL 代理预测"""
        key = self._key(symbol, timeframe)

        if key not in self._agents:
            # 尝试加载
            from engine.models import FeatureEngineer
            fdf, feature_cols = FeatureEngineer.compute_features(df)
            env = TradingEnvironment(df=fdf, feature_cols=feature_cols)
            agent = PPOAgent(state_dim=env.state_dim, action_dim=env.action_dim)
            if agent.load(key):
                self._agents[key] = agent
                self._feature_cols[key] = feature_cols
            else:
                return {'status': 'no_model', 'key': key}

        agent = self._agents[key]
        feature_cols = self._feature_cols[key]

        from engine.models import FeatureEngineer
        fdf, _ = FeatureEngineer.compute_features(df)
        env = TradingEnvironment(df=fdf, feature_cols=feature_cols)

        # 用最新状态预测
        state = env.reset()
        # 推进到最后一步前
        for _ in range(len(df) - 2):
            state, _, done, _ = env.step(0)
            if done:
                break

        result = agent.predict(state)
        current_price = float(df['close'].iloc[-1])

        return {
            'status': 'ok',
            'symbol': symbol,
            'action': result['action'],
            'action_name': result['action_name'],
            'probabilities': result['probabilities'],
            'current_price': round(current_price, 2),
            'model_info': agent.training_stats,
        }

    def generate_signal(self, df: pd.DataFrame, params: Dict) -> List[Dict]:
        """生成交易信号 (供 StrategyEngine 调用)"""
        symbol = params.get('symbol', 'BTC/USDT')
        timeframe = params.get('timeframe', '1h')
        result = self.predict(df, symbol, timeframe)

        signals = []
        if result.get('status') == 'ok':
            curr = df.iloc[-1]
            if result['action'] == 1:  # buy
                signals.append({
                    'type': 'buy',
                    'price': curr['close'],
                    'confidence': result['probabilities']['buy'],
                })
            elif result['action'] == 2:  # sell
                signals.append({
                    'type': 'sell',
                    'price': curr['close'],
                    'confidence': result['probabilities']['sell'],
                })
        return signals

    def list_models(self) -> List[Dict]:
        """列出已训练的 RL 模型"""
        models = []
        for pt_file in RL_MODEL_DIR.glob("*.pt"):
            safe_key = pt_file.stem
            meta_file = RL_MODEL_DIR / f"{safe_key}.json"
            meta = {}
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                except Exception:
                    pass
            models.append({
                'key': safe_key.replace('_', ':', 1).replace('_', '/', 1),
                'stats': meta.get('training_stats', {}),
                'saved_at': meta.get('saved_at', ''),
            })
        return models


# ================================================================
# 全局单例
# ================================================================

rl_manager = RLStrategyManager()
