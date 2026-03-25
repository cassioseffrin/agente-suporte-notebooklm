"""
Backend FastAPI — substitui OpenAI Assistants API
Mantém contrato com o app Flutter existente:
  GET  /createNewThread  → { threadId: str }
  POST /chat             → { content: [str], images: [] }

LLM: OpenAI gpt-4o-mini
RAG: notebooklm ask (CLI subprocess)
Sessões: isoladas por threadId, histórico em memória
"""

import asyncio
import uuid
import os
import json
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv  # Carregar variáveis do .env
load_dotenv()

from openai import AsyncOpenAI
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config — via variáveis de ambiente
# ---------------------------------------------------------------------------

OPENAI_API_KEY         = os.environ.get("OPENAI_API_KEY", "")
# NOTEBOOKLM_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID", "101c21c5-2cd9-43c0-9dac-464e14f21aa0")  $reforma
BACKEND_API_KEY        = os.environ.get("BACKEND_API_KEY", "")
NOTEBOOKLM_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID", "5a83e6e6-8105-4bc4-b371-6d38c59bcade") #smart

HISTORY_LIMIT      = 0
NOTEBOOKLM_TIMEOUT = 60

SYSTEM_PROMPT = """Você é um assistente especializado no sistema ERP da empresa.
Responda perguntas dos clientes com base nos manuais do sistema.
Seja claro, objetivo e use exemplos práticos quando possível.
Responda sempre em português brasileiro.
Formate suas respostas em Markdown quando isso ajudar a clareza."""

# ---------------------------------------------------------------------------
# Estado em memória
# ---------------------------------------------------------------------------

sessions: dict[str, list[dict]] = defaultdict(list)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ERP Assistant Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    threadId: str
    message: str
    assistantName: Optional[str] = "SMART"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_api_key(authorization: str = Header(None)):
    if not authorization or authorization != f"Bearer {BACKEND_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


async def query_notebooklm(user_message: str) -> str:
    if not NOTEBOOKLM_NOTEBOOK_ID:
        return ""

    cmd = [
        "notebooklm", "ask",
        user_message,
        "-n", NOTEBOOKLM_NOTEBOOK_ID,
        "--json",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=NOTEBOOKLM_TIMEOUT,
        )

        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            return data.get("answer", "")

        print(f"[notebooklm] stderr: {stderr.decode()[:300]}")
        return ""

    except asyncio.TimeoutError:
        print("[notebooklm] timeout")
        return ""
    except Exception as e:
        print(f"[notebooklm] erro: {e}")
        return ""


def build_messages(thread_id: str, user_message: str, notebooklm_context: str) -> list[dict]:
    history = sessions[thread_id][-HISTORY_LIMIT:]

    if notebooklm_context:
        user_content = (
            f"Contexto encontrado nos manuais do sistema:\n"
            f"---\n{notebooklm_context}\n---\n\n"
            f"Pergunta do cliente: {user_message}"
        )
    else:
        user_content = user_message

    return history + [{"role": "user", "content": user_content}]


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.get("/createNewThread")
async def create_new_thread():
    thread_id = str(uuid.uuid4())
    sessions[thread_id] = []
    return {"threadId": thread_id}


@app.post("/chat")
async def chat(request: ChatRequest, authorization: str = Header(None)):
    verify_api_key(authorization)

    thread_id    = request.threadId
    user_message = request.message.strip()

    if not user_message:
        raise HTTPException(status_code=400, detail="Mensagem vazia")

    if thread_id not in sessions:
        sessions[thread_id] = []

    # 1. NotebookLM — busca contexto nos manuais
    notebooklm_context = await query_notebooklm(user_message)

    # 2. Monta histórico + contexto injetado
    messages = build_messages(thread_id, user_message, notebooklm_context)

    # 3. OpenAI gpt-4o-mini
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=10240,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        )
        assistant_text = response.choices[0].message.content

    except Exception as e:
        print(f"[openai] erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao gerar resposta")

    # 4. Histórico — salva mensagem original (sem contexto injetado)
    sessions[thread_id].append({"role": "user",      "content": user_message})
    sessions[thread_id].append({"role": "assistant", "content": assistant_text})

    # 5. Retorna no formato que o Flutter já espera
    return {
        "content": [assistant_text],
        "images":  []
    }


@app.get("/health")
async def health():
    return {"status": "ok", "sessions_ativas": len(sessions)}


# ---------------------------------------------------------------------------
# Limpeza de sessões ociosas (> 2h)
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    asyncio.create_task(_cleanup_loop())

async def _cleanup_loop():
    import time
    last_activity: dict[str, float] = {}
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        expiradas = [tid for tid, t in last_activity.items() if now - t > 7200]
        for tid in expiradas:
            sessions.pop(tid, None)
            last_activity.pop(tid, None)
        if expiradas:
            print(f"[cleanup] {len(expiradas)} sessões removidas")