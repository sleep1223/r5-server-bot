from pydantic import BaseModel


class Config(BaseModel):
    r5_api_base: str = "http://127.0.0.1:8000/v1/r5"
    r5_api_token: str = ""
