import json
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from sca.api.authz import ForbiddenError
from sca.config import get_settings
from sca.core.errors import AuthError, NotFoundError
from sca.core.models import TaskState
from sca.db.repo import Store
from sca.knowledge.models import ReadInput, SearchInput
from sca.knowledge.service import read_document, search
from sca.tools.models import KNOWLEDGE
from sca.tools.settings import get_tool_settings
from sca.tools.tokens import PATH, verify_knowledge_token

router = APIRouter()
_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
_TOOLS = [
    {"name": "search_knowledge", "description": "检索企业知识库，返回文档标题和分块引用。",
     "inputSchema": SearchInput.model_json_schema()},
    {"name": "read_document", "description": "按 ID 读取已授权的知识库文档及其分块引用。",
     "inputSchema": ReadInput.model_json_schema()},
]


async def _authorize_task(request: Request) -> tuple[Store, list[str], UUID | None]:
    settings = get_tool_settings()
    origin = request.headers.get("origin")
    if origin is not None:
        try:
            url = urlsplit(settings.knowledge_mcp_url or "")
        except ValueError:
            raise ForbiddenError("MCP 请求来源无效") from None
        if not url.netloc or origin != f"{url.scheme}://{url.netloc}":
            raise ForbiddenError("MCP 请求来源无效")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("需要知识库任务令牌")
    task_id, tenant = verify_knowledge_token(settings, token)
    store = request.app.state.store.for_tenant(tenant)
    try:
        task = await store.get_task(task_id, tenant)
    except NotFoundError:
        raise AuthError("知识库 MCP 令牌无效或已过期") from None
    if task.state != TaskState.RUNNING or KNOWLEDGE not in task.spec.get("tools", []):
        raise ForbiddenError("任务目前无法使用知识库工具")
    actor = await store.task_submitter(task_id)
    teams = settings.teams_for(tenant, actor)
    if not actor or not await store.available_tools(actor, teams, names=[KNOWLEDGE]):
        raise ForbiddenError("知识库工具未授权")
    return store, teams, task.project_id


def _result(id_: int | str, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_: int | str | None, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}},
                        status_code=status)


async def _call_tool(store: Store, teams: list[str], project_id: UUID | None, params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments", {})
    try:
        if name == "search_knowledge":
            body = SearchInput.model_validate(args)
            hits = await search(store, teams, body.query, body.k, get_settings(), project_id=project_id)
            value = {"results": [hit.model_dump(mode="json") for hit in hits]}
        elif name == "read_document":
            body = ReadInput.model_validate(args)
            value = (await read_document(store, teams, body.id, project_id=project_id)).model_dump(mode="json")
        else:
            raise ValueError
    except NotFoundError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "structuredContent": value}


@router.post(PATH, include_in_schema=False)
async def knowledge_mcp(request: Request) -> Response:
    store, teams, project_id = await _authorize_task(request)
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        return _error(None, -32600, "MCP 请求须使用 application/json", 415)
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version not in _VERSIONS:
        return _error(None, -32600, "不支持的 MCP 协议版本", 400)
    try:
        message = await request.json()
    except ValueError:
        return _error(None, -32700, "JSON 解析失败", 400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "无效的 JSON-RPC 请求", 400)
    method, id_ = message.get("method"), message.get("id")
    if not isinstance(method, str) or ("id" in message and type(id_) not in (str, int)):
        return _error(None, -32600, "无效的 JSON-RPC 请求", 400)
    if "id" not in message:
        return Response(status_code=202)
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(id_, -32602, "无效的 MCP 参数")
    if method == "initialize":
        proposed = params.get("protocolVersion")
        return _result(id_, {"protocolVersion": proposed if proposed in _VERSIONS else _VERSIONS[0],
                             "capabilities": {"tools": {}},
                             "serverInfo": {"name": "sca-knowledge", "version": "1.0.0"}})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": _TOOLS})
    if method == "tools/call":
        try:
            return _result(id_, await _call_tool(store, teams, project_id, params))
        except (ValidationError, ValueError):
            return _error(id_, -32602, "无效的 MCP 工具参数")
    return _error(id_, -32601, "不支持的 MCP 方法")


@router.get(PATH, include_in_schema=False)
async def knowledge_stream(request: Request) -> Response:
    await _authorize_task(request)
    return Response(status_code=405, headers={"Allow": "POST"})
