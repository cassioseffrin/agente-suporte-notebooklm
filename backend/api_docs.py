"""
Documentação da API (Swagger UI / ReDoc) protegida por HTTP Basic.

  GET /docs          → Swagger UI (com exemplos curl por endpoint)
  GET /redoc         → ReDoc
  GET /openapi.json  → schema OpenAPI

Variáveis de ambiente:
  DOCS_USER        usuário do Basic Auth (padrão: admin)
  DOCS_PASSWORD    senha do Basic Auth — se vazia, a documentação fica DESABILITADA (404)
  DOCS_PUBLIC_URL  URL base usada nos exemplos curl (padrão: http://localhost:8000)
"""

import json
import os
import secrets

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

DOCS_USER       = os.environ.get("DOCS_USER", "admin")
DOCS_PASSWORD   = os.environ.get("DOCS_PASSWORD", "")
DOCS_PUBLIC_URL = os.environ.get("DOCS_PUBLIC_URL", "http://localhost:8000").rstrip("/")

_basic = HTTPBasic(realm="API Docs")

# Agrupamento dos endpoints por prefixo do path
_TAGS = {
    "chat": "Chat", "createNewThread": "Chat", "transcribe": "Chat", "login": "Chat",
    "thread": "Threads", "history": "Threads",
    "agents": "Agentes", "agent": "Agentes", "updateNotebooks": "Agentes",
    "dashboard": "Dashboard", "feedbacks": "Dashboard",
    "admin": "Admin",
    "refreshAuth": "NotebookLM Auth", "uploadAuthState": "NotebookLM Auth",
    "authStatus": "NotebookLM Auth", "renameProfile": "NotebookLM Auth",
    "notebooklm-version": "NotebookLM Auth",
    "health": "Infra",
}

_SSE_HINTS = ("stream", "events")


def _check_docs_auth(credentials: HTTPBasicCredentials = Depends(_basic)):
    if not DOCS_PASSWORD:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    ok_user = secrets.compare_digest(credentials.username.encode(), DOCS_USER.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), DOCS_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="API Docs"'},
        )


# ---------------------------------------------------------------------------
# Geração dos exemplos curl
# ---------------------------------------------------------------------------

def _resolve(schema: dict, components: dict) -> dict:
    ref = schema.get("$ref")
    if ref:
        return components.get(ref.split("/")[-1], {})
    if "anyOf" in schema:  # Optional[X] → X
        non_null = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if non_null:
            return _resolve(non_null[0], components)
    return schema


def _example_value(name: str, schema: dict, components: dict):
    if schema.get("default") is not None:
        return schema["default"]
    schema = _resolve(schema, components)
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    if "example" in schema:
        return schema["example"]
    t = schema.get("type")
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object":
        return _example_object(schema, components)
    return f"<{name}>"


def _example_object(schema: dict, components: dict) -> dict:
    schema = _resolve(schema, components)
    return {
        prop: _example_value(prop, sub, components)
        for prop, sub in schema.get("properties", {}).items()
    }


def _build_curl(method: str, path: str, op: dict, components: dict, needs_auth: bool) -> str:
    url_path = path
    query = []
    for p in op.get("parameters", []):
        if p["in"] == "path":
            url_path = url_path.replace("{" + p["name"] + "}", f"<{p['name']}>")
        elif p["in"] == "query":
            schema = p.get("schema", {})
            if p.get("required") or schema.get("default") is not None:
                query.append(f"{p['name']}={_example_value(p['name'], schema, components)}")

    url = DOCS_PUBLIC_URL + url_path + (("?" + "&".join(query)) if query else "")
    lines = [f"curl{' -N' if any(h in path for h in _SSE_HINTS) else ''} -X {method.upper()} '{url}'"]
    if needs_auth:
        lines.append('-H "Authorization: Bearer $BACKEND_API_KEY"')

    content = op.get("requestBody", {}).get("content", {})
    if "application/json" in content:
        body = _example_object(content["application/json"]["schema"], components)
        lines.append("-H 'Content-Type: application/json'")
        lines.append(f"-d '{json.dumps(body, ensure_ascii=False)}'")
    elif "multipart/form-data" in content:
        form = _resolve(content["multipart/form-data"]["schema"], components)
        for prop, sub in form.get("properties", {}).items():
            sub = _resolve(sub, components)
            if sub.get("format") == "binary" or sub.get("contentMediaType"):
                lines.append(f"-F '{prop}=@/caminho/do/arquivo'")
            else:
                lines.append(f"-F '{prop}={_example_value(prop, sub, components)}'")

    return " \\\n  ".join(lines)


def _custom_openapi(app: FastAPI):
    def openapi():
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=(
                "Endpoints marcados com cadeado exigem o header "
                "`Authorization: Bearer <BACKEND_API_KEY>` — use o botão **Authorize**.\n\n"
                "Cada endpoint traz um exemplo `curl`; o Swagger também gera o curl ao clicar em "
                "**Try it out → Execute**."
            ),
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http", "scheme": "bearer",
        }
        components = schema["components"].get("schemas", {})

        for path, methods in schema.get("paths", {}).items():
            for method, op in methods.items():
                params = op.get("parameters", [])
                # Endpoints recebem `authorization: str = Header(None)` manualmente;
                # trocamos o parâmetro pelo security scheme para o Authorize funcionar.
                needs_auth = any(p["in"] == "header" and p["name"].lower() == "authorization" for p in params)
                if needs_auth:
                    op["parameters"] = [
                        p for p in params
                        if not (p["in"] == "header" and p["name"].lower() == "authorization")
                    ]
                    op["security"] = [{"BearerAuth": []}]

                segment = path.strip("/").split("/")[0]
                op["tags"] = [_TAGS.get(segment, "Outros")]

                curl = _build_curl(method, path, op, components, needs_auth)
                op["x-codeSamples"] = [{"lang": "Shell", "label": "curl", "source": curl}]
                op["description"] = (op.get("description", "") + f"\n\n**Exemplo:**\n```bash\n{curl}\n```").strip()

        app.openapi_schema = schema
        return schema

    return openapi


def setup_docs(app: FastAPI) -> None:
    """Registra /docs, /redoc e /openapi.json protegidos. Requer app criado com
    docs_url=None, redoc_url=None, openapi_url=None."""
    app.openapi = _custom_openapi(app)
    guard = [Depends(_check_docs_auth)]

    @app.get("/openapi.json", include_in_schema=False, dependencies=guard)
    async def openapi_json():
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False, dependencies=guard)
    async def swagger_ui():
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Docs")

    @app.get("/redoc", include_in_schema=False, dependencies=guard)
    async def redoc():
        return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")
