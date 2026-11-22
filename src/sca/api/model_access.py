from fastapi import Request
from fastapi.responses import JSONResponse

from sca.gateway.configuration import model_configured


class ModelNotConfiguredError(Exception):
    pass


def require_model() -> None:
    if not model_configured():
        raise ModelNotConfiguredError


async def model_not_configured(request: Request, exc: ModelNotConfiguredError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "model_not_configured", "message": "请先在 Portal 配置模型接入"}},
        status_code=409,
    )
