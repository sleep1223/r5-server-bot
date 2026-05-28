from pydantic import BaseModel


class Config(BaseModel):
    r5_api_base: str = "http://127.0.0.1:8000/v1/r5"
    r5_api_token: str = ""
    r5_group_join_question: str = "一句话证明你玩过apex"
    r5_group_join_llm_api_key: str = ""
    r5_group_join_llm_base_url: str = "https://api.openai.com/v1"
    r5_group_join_llm_model: str = "gpt-4o-mini"
    r5_group_join_llm_timeout: float = 10.0
    r5_group_join_llm_reject_on_fail: bool = False
