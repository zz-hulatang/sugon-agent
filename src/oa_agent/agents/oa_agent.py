"""创建模型和 Agent，并注册当前用户可调用的工具。"""
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from oa_agent.agents.prompts import SYSTEM_PROMPT
from oa_agent.config import ModelConfig


def build_agent(tools: list, config: ModelConfig):
    """用当前会话绑定的工具创建 Agent，登录功能本身不依赖模型初始化。"""
    if not config.configured:
        raise ValueError("请配置 OPENAI_API_KEY 和 OPENAI_MODEL")
    model = ChatOpenAI(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url or None,
        timeout=40,
        max_retries=1,
    )
    # 只注册明确支持的能力；模型通过工具接收业务结果，不直接接触浏览器或凭据仓库。
    return create_agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT)
