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

HISTORY_LIMIT      = 10   # últimas N mensagens enviadas ao OpenAI (5 turnos)
NOTEBOOKLM_TIMEOUT = 60

SYSTEM_PROMPT = """
PERSONAGEM: Você é o 'ASSISTENTE para Smart Força de Vendas', um chatbot especializado em responder dúvidas com base exclusivamente nos manuais e documentos do sistema.

REGRAS OBRIGATÓRIAS (não podem ser ignoradas):
1. Você SOMENTE pode responder com informações presentes no [CONTEXTO DOS MANUAIS] fornecido em cada mensagem.
2. Se o contexto estiver ausente ou não contiver informação relevante para a pergunta, responda APENAS: "Não encontrei informações sobre esse assunto nos manuais do sistema. Por favor, consulte o suporte técnico ou reformule sua pergunta."
3. NUNCA invente, suponha ou utilize conhecimento próprio para preencher lacunas não cobertas pelo contexto fornecido.
4. Você pode ajustar a gramática, concordância e formatação do texto extraído do contexto para tornar a resposta mais clara e natural.
5. Não mencione nem cite os documentos ou fontes pelo nome — apresente a informação como uma explicação direta.
6. Sempre responda em português do Brasil.
7. Formate suas respostas em Markdown quando isso ajudar a clareza."""

# Prompt exclusivo para reescrita de consulta — NÃO responde ao usuário,
# apenas produz uma pergunta autocontida para ser enviada ao NotebookLM.
QUERY_REWRITE_PROMPT = """
Você é um assistente especializado em preparar consultas para um sistema de busca em manuais.

Sua ÚNICA tarefa é reescrever a última pergunta do usuário em uma consulta AUTOCONTIDA e ESPECÍFICA,
incorporando o contexto necessário do histórico da conversa quando a pergunta for vaga, incompleta
ou fizer referência a algo mencionado antes (ex: "isso", "o passo 2", "aquele campo", "pode detalhar?").

REGRAS OBRIGATÓRIAS:
1. Retorne APENAS a pergunta reescrita — sem explicações, sem prefixos, sem aspas.
2. Se a pergunta já for clara e autocontida, retorne-a exatamente como está.
3. A pergunta reescrita deve ser em português do Brasil.
4. Jamais invente informações — use somente o que está no histórico fornecido.
5. A pergunta deve ser objetiva e adequada para busca em manuais técnicos."""

# ---------------------------------------------------------------------------
# Estado em memória
# ---------------------------------------------------------------------------

sessions: dict[str, list[dict[str, str]]] = defaultdict(list)

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


async def rewrite_query_with_context(thread_id: str, user_message: str) -> str:
    """Usa o OpenAI + histórico da thread para expandir perguntas ambíguas
    em consultas autocontidas antes de enviar ao NotebookLM.
    Retorna a pergunta original se não houver histórico ou se já for clara."""
    history = sessions[thread_id][-HISTORY_LIMIT:]

    # Sem histórico = pergunta já é autocontida, não precisa reescrever
    if not history:
        return user_message

    # Formata o histórico recente como contexto para o rewriter
    history_text = "\n".join(
        f"{msg['role'].upper()}: {msg['content']}"
        for msg in history
    )

    try:
        result = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=256,
            temperature=0,
            messages=[
                {"role": "system", "content": QUERY_REWRITE_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"[HISTÓRICO DA CONVERSA]\n{history_text}\n\n"
                        f"[PERGUNTA ATUAL DO USUÁRIO]\n{user_message}\n\n"
                        f"Reescreva a pergunta atual de forma autocontida:"
                    )
                }
            ]
        )
        rewritten = result.choices[0].message.content.strip()
        if rewritten and rewritten != user_message:
            print(f"[query-rewrite] original: {user_message!r}")
            print(f"[query-rewrite] reescrita: {rewritten!r}")
        return rewritten or user_message

    except Exception as e:
        print(f"[query-rewrite] erro (usando original): {e}")
        return user_message


def build_messages(thread_id: str, user_message: str, notebooklm_context: str) -> list[dict]:
    history = sessions[thread_id][-HISTORY_LIMIT:]

    if notebooklm_context:
        user_content = (
            f"[CONTEXTO DOS MANUAIS]\n"
            f"---\n{notebooklm_context}\n---\n\n"
            f"Com base APENAS no contexto acima, responda a seguinte pergunta do cliente:\n"
            f"{user_message}"
        )
    else:
        # Sem contexto: instrui explicitamente o modelo a não inventar
        user_content = (
            f"[CONTEXTO DOS MANUAIS]\n"
            f"---\nNenhuma informação relevante foi encontrada nos manuais para esta consulta.\n---\n\n"
            f"Pergunta do cliente: {user_message}\n\n"
            f"INSTRUÇÃO: Como não há contexto disponível nos manuais, informe ao usuário que não foi possível encontrar essa informação e sugira que ele consulte o suporte técnico."
        )

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

    # 1. Query Rewriting — expande perguntas vagas usando histórico da thread
    search_query = await rewrite_query_with_context(thread_id, user_message)

    # 2. NotebookLM — busca contexto nos manuais com a query expandida
    notebooklm_context = await query_notebooklm(search_query)

    # 3. Monta histórico + contexto injetado (usa mensagem original do usuário)
    messages = build_messages(thread_id, user_message, notebooklm_context)

    # 4. OpenAI gpt-4o-mini — formata resposta com base no contexto do NotebookLM
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

    # 5. Histórico — salva mensagem original do usuário (não a query reescrita)
    sessions[thread_id].append({"role": "user",      "content": user_message})
    sessions[thread_id].append({"role": "assistant", "content": assistant_text})

    # 6. Retorna no formato que o Flutter já espera
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