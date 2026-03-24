"""
MyTradingPlatform — LLM 客户端
支持 OpenAI / DeepSeek / Kimi / 任意 OpenAI 兼容 API

配置方式（环境变量）：
  AI_API_KEY     — API 密钥
  AI_BASE_URL    — API 地址（默认 https://api.openai.com/v1）
  AI_MODEL       — 模型名（默认 gpt-4o-mini）

常用配置示例：
  # DeepSeek
  AI_API_KEY=sk-xxx
  AI_BASE_URL=https://api.deepseek.com/v1
  AI_MODEL=deepseek-chat

  # Kimi (Moonshot)
  AI_API_KEY=sk-xxx
  AI_BASE_URL=https://api.moonshot.cn/v1
  AI_MODEL=moonshot-v1-8k

  # OpenAI
  AI_API_KEY=sk-xxx
  AI_BASE_URL=https://api.openai.com/v1
  AI_MODEL=gpt-4o-mini
"""

import os
import json
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# 配置
AI_API_KEY = os.environ.get('AI_API_KEY', '')
AI_BASE_URL = os.environ.get('AI_BASE_URL', 'https://api.openai.com/v1')
AI_MODEL = os.environ.get('AI_MODEL', 'gpt-4o-mini')


def get_client():
    """获取 OpenAI 兼容客户端"""
    if not AI_API_KEY:
        raise ValueError(
            "未配置 AI_API_KEY，请在环境变量中设置\n"
            "支持: OpenAI / DeepSeek / Kimi 等 OpenAI 兼容 API\n"
            "示例: AI_API_KEY=sk-xxx AI_BASE_URL=https://api.deepseek.com/v1 AI_MODEL=deepseek-chat"
        )
    from openai import OpenAI
    return OpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)


def chat(
    messages: List[Dict],
    model: str = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    response_format: str = None,  # "json_object" for JSON mode
) -> str:
    """发送聊天请求，返回文本回复"""
    client = get_client()
    kwargs = {
        'model': model or AI_MODEL,
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    if response_format:
        kwargs['response_format'] = {'type': response_format}

    try:
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content
    except Exception as e:
        logger.error(f"LLM API error: {e}")
        raise


def chat_json(messages: List[Dict], **kwargs) -> Dict:
    """发送请求并解析 JSON 回复"""
    text = chat(messages, response_format='json_object', **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试提取 JSON 块
        import re
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group())
        raise ValueError(f"LLM 返回了非 JSON 格式: {text[:200]}")


def check_connection() -> Dict:
    """检查 LLM API 连通性"""
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=AI_MODEL,
            messages=[{'role': 'user', 'content': 'Reply with: ok'}],
            max_tokens=10,
            temperature=0,
        )
        return {
            'connected': True,
            'model': AI_MODEL,
            'base_url': AI_BASE_URL,
            'response': resp.choices[0].message.content,
        }
    except Exception as e:
        return {
            'connected': False,
            'model': AI_MODEL,
            'base_url': AI_BASE_URL,
            'error': str(e),
        }
