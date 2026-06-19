"""
Backend FastAPI - substitui OpenAI Assistants API
Mantém contrato com o app Flutter existente:
  GET  /createNewThread  → { threadId: str }
  POST /chat             → { content: [str], images: [] }
  GET  /updateNotebooks  → sincroniza notebooks do NotebookLM com tabela agent no Postgres

LLM: OpenAI gpt-4o-mini
RAG: notebooklm ask (CLI subprocess)
Sessões: isoladas por threadId, histórico em memória
"""

import asyncio
import uuid
import os
import json
import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Optional
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

from dotenv import load_dotenv  # Carregar variáveis do .env
load_dotenv()

from openai import AsyncOpenAI
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config - via variáveis de ambiente
# ---------------------------------------------------------------------------

OPENAI_API_KEY         = os.environ.get("OPENAI_API_KEY", "")
BACKEND_API_KEY        = os.environ.get("BACKEND_API_KEY", "")

HISTORY_LIMIT      = 10   # últimas N mensagens enviadas ao OpenAI (5 turnos)
NOTEBOOKLM_TIMEOUT = 240

# ---------------------------------------------------------------------------
# PostgreSQL - conexão e helpers
# ---------------------------------------------------------------------------

DB_HOST   = os.environ.get("DB_HOST",   "192.168.50.21")
DB_PORT   = os.environ.get("DB_PORT",   "5432")
DB_NAME   = os.environ.get("DB_NAME",   "agente_suporte")
DB_USER   = os.environ.get("DB_USER",   "postgres")
DB_PASS   = os.environ.get("DB_PASS",   "Arpa@2010")


def get_db_connection():
    """Retorna uma conexão psycopg2. Fechar após o uso."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
    )


def ensure_tables():
    """Cria as tabelas agent, user, auditor, thread, chat e chat_thread se não existirem, mantendo apenas a última versão."""
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # 1. Tabela agent
                cur.execute("""
                CREATE TABLE IF NOT EXISTS agent (
                    id            VARCHAR(36) PRIMARY KEY,
                    title         TEXT        NOT NULL,
                    name          TEXT,
                    system_prompt TEXT,
                    email         TEXT,
                    overview      TEXT,
                    creation      TIMESTAMP   NOT NULL DEFAULT NOW(),
                    modification  TIMESTAMP   NOT NULL DEFAULT NOW(),
                    sort_order    INTEGER     DEFAULT 0,
                    active        BOOLEAN     DEFAULT TRUE,
                    faq_content   TEXT        DEFAULT '',
                    notebooklm_profile VARCHAR(100) DEFAULT 'default',
                    hide          BOOLEAN     DEFAULT FALSE
                );
                """)

                # Migration: add notebooklm_profile column if missing (existing DBs)
                cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE agent ADD COLUMN notebooklm_profile VARCHAR(100) DEFAULT 'default';
                EXCEPTION
                    WHEN duplicate_column THEN NULL;
                END $$;
                """)

                # Migration: add hide column if missing (existing DBs)
                cur.execute("""
                DO $$ BEGIN
                    ALTER TABLE agent ADD COLUMN hide BOOLEAN DEFAULT FALSE;
                EXCEPTION
                    WHEN duplicate_column THEN NULL;
                END $$;
                """)

                # 2. Tabela auditor
                cur.execute("""
                CREATE TABLE IF NOT EXISTS auditor (
                    id       SERIAL       PRIMARY KEY,
                    login    VARCHAR(255) UNIQUE NOT NULL,
                    senha    VARCHAR(255) NOT NULL,
                    name     VARCHAR(255),
                    nickname VARCHAR(100),
                    icon_svg TEXT,
                    email    VARCHAR(255)
                );
                """)

                # 3. Tabela thread
                cur.execute("""
                CREATE TABLE IF NOT EXISTS thread (
                    id        VARCHAR(36) PRIMARY KEY,
                    subject   TEXT,
                    faq_added BOOLEAN     DEFAULT FALSE
                );
                """)

                # 4. Tabela user
                cur.execute("""
                CREATE TABLE IF NOT EXISTS "user" (
                    id    SERIAL       PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    name  VARCHAR(255),
                    cnpj  VARCHAR(255)
                );
                """)

                # 5. Tabela chat
                cur.execute("""
                CREATE TABLE IF NOT EXISTS chat (
                    id             SERIAL      PRIMARY KEY,
                    user_id        INTEGER     REFERENCES "user"(id) ON DELETE CASCADE,
                    agent_id       VARCHAR(36) REFERENCES agent(id) ON DELETE CASCADE,
                    message        TEXT,
                    created_at     TIMESTAMP   NOT NULL DEFAULT NOW(),
                    origem         VARCHAR(10) DEFAULT 'sistema',
                    feedback_thumb INTEGER,
                    feedback_text  TEXT,
                    auditor_id     INTEGER     REFERENCES auditor(id) ON DELETE SET NULL
                );
                """)

                # 6. Tabela chat_thread
                cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_thread (
                    id              SERIAL      PRIMARY KEY,
                    thread_id       VARCHAR(36) REFERENCES thread(id) ON DELETE CASCADE,
                    chat_id         INTEGER     REFERENCES chat(id) ON DELETE CASCADE,
                    feedback_rating INTEGER     CHECK (feedback_rating >= 1 AND feedback_rating <= 5)
                );
                """)
        print("[DB] Tabelas agent, user, auditor, chat, thread e chat_thread verificadas/criadas com sucesso.")
    except Exception as e:
        print("[DB] Erro ao criar tabelas:", e)
    finally:
        conn.close()


def get_agent_info_by_name(name: str):
    """Busca id (notebook_id), system_prompt e notebooklm_profile pelo nome do agente."""
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, title, system_prompt, COALESCE(notebooklm_profile, 'default') as notebooklm_profile FROM agent WHERE name = %s LIMIT 1", (name,))
                return cur.fetchone()
    finally:
        conn.close()


# Prompt exclusivo para reescrita de consulta - NÃO responde ao usuário,
# apenas produz uma pergunta autocontida para ser enviada ao NotebookLM.
QUERY_REWRITE_PROMPT = """
Você é um assistente especializado em preparar consultas para um sistema de busca em manuais.

Sua ÚNICA tarefa é reescrever a última pergunta do usuário em uma consulta AUTOCONTIDA e ESPECÍFICA,
incorporando o contexto necessário do histórico da conversa quando a pergunta for vaga, incompleta
ou fizer referência a algo mencionado antes (ex: "isso", "o passo 2", "aquele campo", "pode detalhar?").

REGRAS OBRIGATÓRIAS:
1. Retorne APENAS a pergunta reescrita - sem explicações, sem prefixos, sem aspas.
2. Se a pergunta já for clara e autocontida, retorne-a exatamente como está.
3. A pergunta reescrita deve ser em português do Brasil.
4. Jamais invente informações - use somente o que está no histórico fornecido.
5. A pergunta deve ser objetiva e adequada para busca em manuais técnicos.
6. Se houver uma [CORREÇÃO DO SUPORTE HUMANO] no histórico, incorpore essa correção como fato verdadeiro na reescrita da consulta."""

# ---------------------------------------------------------------------------
# Estado em memória
# ---------------------------------------------------------------------------

sessions: dict[str, list[dict[str, str]]] = defaultdict(list)
pending_threads: dict[str, dict] = {}

# Presença: mapeia thread_id -> timestamp do último heartbeat do usuário
user_presence: dict[str, float] = {}

# SSE queues: mapeia thread_id -> lista de asyncio.Queue (cada auditor conectado recebe uma)
# Usado para notificar auditores sobre presença e enviar eventos para o chat do usuário
auditor_queues: dict[str, list[asyncio.Queue]] = defaultdict(list)
user_queues: dict[str, list[asyncio.Queue]] = defaultdict(list)

# Global admin broadcast: lista de asyncio.Queue para SSE /admin/events
admin_broadcast_queues: list[asyncio.Queue] = []

PRESENCE_TIMEOUT = 30  # segundos sem heartbeat = offline

# TTS cache directory
TTS_CACHE_DIR = Path("/tmp/tts_cache")
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _broadcast_admin_event(event: dict):
    """Envia evento para todos os dashboards conectados via SSE /admin/events."""
    for q in admin_broadcast_queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _broadcast_thread_update(thread_id: str):
    """Envia evento de atualização de thread para todos os dashboards conectados."""
    _broadcast_admin_event({
        "type": "thread_updated",
        "thread_id": thread_id,
    })


def _get_user_name_for_thread(thread_id: str) -> str:
    """Busca o nome do usuário pelo thread_id no banco."""
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT u.name FROM "user" u
                    JOIN chat c ON c.user_id = u.id
                    JOIN chat_thread ct ON ct.chat_id = c.id
                    WHERE ct.thread_id = %s
                    LIMIT 1;
                """, (thread_id,))
                row = cur.fetchone()
                return row[0] if row else "Desconhecido"
    except Exception:
        return "Desconhecido"
    finally:
        if 'conn' in locals() and conn:
            conn.close()

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

class AuditorMessageRequest(BaseModel):
    message: str
    auditor_id: Optional[int] = None

class LoginRequest(BaseModel):
    login: str
    senha: str

class FAQRequest(BaseModel):
    faq_text: Optional[str] = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_api_key(authorization: str = Header(None)):
    if not authorization or authorization != f"Bearer {BACKEND_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def _get_notebooklm_cmd(profile: str, *args) -> list[str]:
    """
    Retorna a lista de argumentos para executar o notebooklm usando o arquivo de sessão correspondente.
    Usa '--storage <path>' para isolar as contas.
    """
    from pathlib import Path as _Path
    profile = profile.strip() or "default"
    
    profile_file = _Path.home() / ".notebooklm" / "profiles" / profile / "storage_state.json"
    legacy_file = _Path.home() / ".notebooklm" / "storage_state.json"
    
    if profile_file.exists():
        session_file = profile_file
    elif profile == "default" and legacy_file.exists():
        session_file = legacy_file
    else:
        session_file = profile_file
        
    return ["notebooklm", "--storage", str(session_file)] + list(args)


async def query_notebooklm(user_message: str, notebook_id: str, profile: str = "default", max_retries: int = 3) -> str:
    """Consulta o NotebookLM CLI com retry para falhas rápidas.
    NÃO faz retry em timeout (240s já consome quase todo o budget).
    Orçamento total: ~400s máx para caber no proxy_read_timeout do nginx (600s)."""
    if not notebook_id:
        return ""

    import time
    TIME_BUDGET = 400  # segundos máx para todas as tentativas (nginx=600s, sobra p/ rewrite+openai)
    t0 = time.monotonic()

    cmd = _get_notebooklm_cmd(profile, "ask", user_message, "-n", notebook_id, "--json")
    print(f"[notebooklm] profile={profile!r} | notebook={notebook_id!r} | cmd={' '.join(cmd[:5])}...")

    for attempt in range(1, max_retries + 1):
        elapsed = time.monotonic() - t0
        if elapsed > TIME_BUDGET:
            print(f"[notebooklm] orçamento de tempo esgotado ({elapsed:.0f}s/{TIME_BUDGET}s) — abortando")
            break

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
                raw = stdout.decode()
                data = json.loads(raw)
                answer = data.get("answer", "")
                if answer.strip():
                    if attempt > 1:
                        print(f"[notebooklm] OK na tentativa {attempt}/{max_retries}")
                    return answer
                # answer vazio mesmo com rc=0 → tratar como falha e tentar de novo
                print(f"[notebooklm] tentativa {attempt}/{max_retries}: rc=0 mas answer vazio")
            else:
                stderr_text = stderr.decode()[:300]
                stdout_text = stdout.decode()[:300]
                print(
                    f"[notebooklm] tentativa {attempt}/{max_retries}: "
                    f"rc={proc.returncode} | stderr={stderr_text!r} | stdout={stdout_text!r}"
                )

            # Espera antes de tentar de novo (exceto na última tentativa)
            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)  # backoff: 2s, 4s

        except asyncio.TimeoutError:
            # Timeout = 240s já consumidos → NÃO faz retry (estoura nginx)
            print(f"[notebooklm] TIMEOUT ({NOTEBOOKLM_TIMEOUT}s) — sem retry")
            return ""
        except Exception as e:
            print(f"[notebooklm] tentativa {attempt}/{max_retries}: erro: {e}")
            if attempt < max_retries:
                await asyncio.sleep(2)

    print(f"[notebooklm] FALHOU após {max_retries} tentativas ({time.monotonic() - t0:.0f}s)")
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
            timeout=60.0,
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

    # Extrair correções do auditor do histórico para injetar como contexto prioritário
    auditor_corrections = [
        msg['content'] for msg in history
        if msg.get('role') == 'system' and '[CORREÇÃO DO SUPORTE HUMANO]' in msg.get('content', '')
    ]

    auditor_block = ""
    if auditor_corrections:
        auditor_block = (
            "\n\n[CORREÇÕES DO SUPORTE HUMANO - VERDADE ABSOLUTA]\n"
            + "\n".join(auditor_corrections)
            + "\n[FIM DAS CORREÇÕES]\n\n"
            "REGRA CRÍTICA: As correções acima foram feitas por um especialista humano e "
            "DEVEM ser tratadas como verdade absoluta. Se houver conflito entre o contexto "
            "dos manuais e as correções do suporte humano, PRIORIZE as correções do suporte.\n"
        )

    if notebooklm_context:
        user_content = (
            f"[CONTEXTO DOS MANUAIS]\n"
            f"---\n{notebooklm_context}\n---\n"
            f"{auditor_block}"
            f"INSTRUÇÃO OBRIGATÓRIA: O contexto acima foi encontrado nos manuais do sistema e é RELEVANTE para a pergunta. "
            f"Use EXCLUSIVAMENTE este contexto para formular sua resposta. "
            f"Se o contexto indicar que uma funcionalidade NÃO existe ou NÃO está disponível, "
            f"informe isso claramente ao cliente — isso É uma resposta válida. "
            f"NUNCA diga que 'não encontrou informações' quando há contexto acima.\n\n"
            f"Pergunta do cliente: {user_message}"
        )
    else:
        user_content = (
            f"[CONTEXTO DOS MANUAIS]\n"
            f"---\nNenhuma informação relevante foi encontrada nos manuais para esta consulta.\n---\n"
            f"{auditor_block}"
            f"Pergunta do cliente: {user_message}\n\n"
            f"INSTRUÇÃO: Como não há contexto disponível nos manuais, informe ao usuário que não foi possível encontrar essa informação e sugira que ele consulte o suporte técnico."
        )

    # Filtrar mensagens system do auditor do histórico enviado ao OpenAI
    # (elas já foram injetadas como bloco de correção no user_content)
    filtered_history = [msg for msg in history if msg.get('role') != 'system']

    return filtered_history + [{"role": "user", "content": user_content}]


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.post("/login")
async def login(request: LoginRequest):
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, login, name, nickname, icon_svg, email FROM auditor WHERE login = %s AND senha = %s",
                    (request.login, request.senha)
                )
                auditor = cur.fetchone()
                if auditor:
                    return {
                        "status": "ok",
                        "user": {
                            "id": auditor["id"],
                            "login": auditor["login"],
                            "name": auditor["name"],
                            "nickname": auditor["nickname"],
                            "icon_svg": auditor["icon_svg"],
                            "email": auditor["email"],
                        }
                    }
                else:
                    raise HTTPException(status_code=401, detail="Credenciais incorretas")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro no servidor: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

@app.get("/createNewThread")
async def create_new_thread(
    email: str | None = None,
    name: str | None = None,
    cnpj: str | None = None,
    agentName: str | None = None
):
    thread_id = str(uuid.uuid4())
    sessions[thread_id] = []

    # Fallbacks para Usuário Genérico
    email = email or "usuario.generico@arpasistemas.com.br"
    name = name or "Usuario Generico"
    cnpj = cnpj or "03.600.477/0001-04"
    agentName = agentName or "SMART"

    # Store metadata in pending_threads to defer DB creation until first message
    pending_threads[thread_id] = {
        "email": email,
        "name": name,
        "cnpj": cnpj,
        "agentName": agentName
    }

    return {"threadId": thread_id, "userId": None}


class FeedbackRequest(BaseModel):
    rating: int


@app.put("/thread/{thread_id}/feedback")
async def update_thread_feedback(thread_id: str, request: FeedbackRequest, authorization: str = Header(None)):
    """Atualiza o feedback_rating do chat_thread associado à thread (1 a 5)."""
    verify_api_key(authorization)

    if request.rating < 1 or request.rating > 5:
        raise HTTPException(status_code=400, detail="Rating deve ser de 1 a 5")

    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE chat_thread SET feedback_rating = %s WHERE thread_id = %s RETURNING id;
                """, (request.rating, thread_id))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Thread não encontrada")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao atualizar feedback: {e}")
        raise HTTPException(status_code=500, detail="Erro ao atualizar feedback")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    _broadcast_thread_update(thread_id)
    return {"status": "ok", "thread_id": thread_id, "feedback_rating": request.rating}


class SubjectRequest(BaseModel):
    subject: str

@app.put("/thread/{thread_id}/subject")
async def update_thread_subject(thread_id: str, request: SubjectRequest, authorization: str = Header(None)):
    """Atualiza o assunto de uma thread."""
    verify_api_key(authorization)
    
    subject = request.subject[:200]
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE thread SET subject = %s WHERE id = %s RETURNING id;
                """, (subject, thread_id))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Thread não encontrada")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao atualizar subject: {e}")
        raise HTTPException(status_code=500, detail="Erro ao atualizar subject")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    _broadcast_thread_update(thread_id)
    return {"status": "ok", "thread_id": thread_id, "subject": subject}


async def generate_and_update_subject(thread_id: str, user_message: str, assistant_message: str):
    """Gera um subject curto baseado na primeira mensagem e salva no banco."""
    prompt = f"Gere um título conciso (máximo 60 caracteres) para a seguinte interação de suporte. Apenas retorne o título, sem aspas e sem explicações.\nUsuário: {user_message}\nAssistente: {assistant_message}"
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=60,
            temperature=0.3,
            timeout=10.0,
            messages=[{"role": "user", "content": prompt}]
        )
        new_subject = response.choices[0].message.content.strip()[:200]
        
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE thread SET subject = %s WHERE id = %s;", (new_subject, thread_id))
        
        _broadcast_thread_update(thread_id)
    except Exception as e:
        print(f"[subject thread] Erro ao compor subject automatico: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.get("/agents")
async def get_agents():
    """Retorna a lista de todos os agentes configurados no sistema."""
    try:
        conn = get_db_connection()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha na conexão com o banco: {e}")

    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, name, title, sort_order FROM agent WHERE active = TRUE AND hide = FALSE ORDER BY sort_order ASC;")
                rows = cur.fetchall()
                # Removemos datetime objectos caso existissem, mas select é só id, name, title
                return {"agents": rows}
    finally:
        conn.close()


@app.get("/updateNotebooks")
async def update_notebooks(profile: str = "default"):
    """
    1. Executa `notebooklm -p <profile> list --json` para obter todos os notebooks.
    2. Para cada notebook, faz INSERT ... ON CONFLICT (id) DO UPDATE na tabela agent.
    3. Notebooks que não estão mais na lista têm active=false (nunca são deletados).
    4. Retorna resumo: notebooks encontrados, inseridos, atualizados e desativados.
    """
    profile = profile.strip() or "default"

    # --- 1. Chamar CLI ---
    try:
        cmd = _get_notebooklm_cmd(profile, "list", "--json")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout ao executar notebooklm list")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="CLI 'notebooklm' não encontrado no PATH")

    if proc.returncode != 0:
        err = stderr.decode()[:500]
        raise HTTPException(status_code=500, detail=f"notebooklm list falhou: {err}")

    # --- 2. Parsear JSON ---
    try:
        raw = stdout.decode()
        notebooks = json.loads(raw)
        # Aceita lista direta ou { "notebooks": [...] }
        if isinstance(notebooks, dict):
            notebooks = notebooks.get("notebooks", [])
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"JSON inválido da CLI: {e}")

    if not isinstance(notebooks, list):
        raise HTTPException(status_code=500, detail="Formato inesperado da CLI notebooklm")

    # --- 3. Upsert no Postgres ---
    upsert_sql = """
        INSERT INTO agent (id, title, name, creation, modification, active, notebooklm_profile)
        VALUES (%(id)s, %(title)s, %(name)s, %(creation)s, %(modification)s, FALSE, %(profile)s)
        ON CONFLICT (id) DO UPDATE
            SET title        = EXCLUDED.title,
                modification = EXCLUDED.modification,
                notebooklm_profile = COALESCE(agent.notebooklm_profile, EXCLUDED.notebooklm_profile)
        RETURNING (xmax = 0) AS inserted;
    """

    inserted_count    = 0
    updated_count     = 0
    deactivated_count = 0
    errors            = []
    valid_ids      = []
    now            = datetime.utcnow()

    try:
        conn = get_db_connection()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha na conexão com o banco: {e}")

    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for nb in notebooks:
                    nb_id    = nb.get("id") or nb.get("notebook_id")
                    nb_title = nb.get("title", "")
                    # 'name' pode ser o próprio título até que o usuário defina
                    nb_name  = nb.get("name") or nb_title
                    # Data de criação vinda da CLI (campo 'created_at')
                    raw_date = nb.get("created_at") or nb.get("created") or nb.get("creation")
                    try:
                        nb_created = datetime.fromisoformat(raw_date) if raw_date else now
                    except (ValueError, TypeError):
                        nb_created = now

                    if not nb_id:
                        errors.append({"notebook": nb, "erro": "ID ausente"})
                        continue

                    valid_ids.append(nb_id)

                    try:
                        cur.execute(upsert_sql, {
                            "id":           nb_id,
                            "title":        nb_title,
                            "name":         nb_name,
                            "creation":     nb_created,
                            "modification": now,
                            "profile":      profile,
                        })
                        row = cur.fetchone()
                        if row and row["inserted"]:
                            inserted_count += 1
                        else:
                            updated_count += 1
                    except Exception as e:
                        errors.append({"id": nb_id, "erro": str(e)})
                
                # --- 4. Desativar removidos (soft delete) ---
                if valid_ids:
                    # Marca como inativo os agentes pertencentes a este profile que não vieram na lista atual
                    cur.execute(
                        "UPDATE agent SET active = FALSE, modification = %s WHERE id != ALL(%s) AND active = TRUE AND COALESCE(notebooklm_profile, 'default') = %s RETURNING id;",
                        (now, valid_ids, profile)
                    )
                    deactivated_rows = cur.fetchall()
                    deactivated_count = len(deactivated_rows)
                # Se valid_ids estiver vazio não desativamos nada (segurança)
    finally:
        conn.close()

    return {
        "status":      "ok",
        "total":       len(notebooks),
        "inseridos":   inserted_count,
        "atualizados": updated_count,
        "desativados": deactivated_count,
        "erros":       errors,
    }


async def run_in_thread(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)

def _ensure_thread_in_db_and_get_ids(cur, thread_id: str) -> tuple[int | None, int | None]:
    """
    Garante que a thread e seu usuário estejam registrados no banco
    no momento em que a primeira mensagem é enviada (lazy creation).
    Retorna (user_id, agent_id).
    """
    # 1. Verifica se a thread já possui alguma mensagem no chat
    cur.execute("""
        SELECT c.user_id, c.agent_id FROM chat c
        JOIN chat_thread ct ON ct.chat_id = c.id
        WHERE ct.thread_id = %s
        LIMIT 1;
    """, (thread_id,))
    row = cur.fetchone()
    if row:
        return row[0], row[1]

    # 2. Se não possuir mensagens, verifica se a thread em si existe
    cur.execute("SELECT 1 FROM thread WHERE id = %s;", (thread_id,))
    thread_exists = cur.fetchone() is not None

    # Recupera metadados da memória (do createNewThread) ou usa fallbacks
    meta = pending_threads.get(thread_id, {})
    email = meta.get("email") or "usuario.generico@arpasistemas.com.br"
    name = meta.get("name") or "Usuario Generico"
    cnpj = meta.get("cnpj") or "03.600.477/0001-04"
    agentName = meta.get("agentName") or "SMART"

    # Upsert do usuário
    cur.execute("""
        INSERT INTO "user" (email, name, cnpj)
        VALUES (%s, %s, %s)
        ON CONFLICT (email) DO UPDATE 
        SET name = EXCLUDED.name, cnpj = EXCLUDED.cnpj
        RETURNING id;
    """, (email, name, cnpj))
    user_id = cur.fetchone()[0]

    # Localiza o agente pelo agentName
    cur.execute("SELECT id, title FROM agent WHERE name = %s;", (agentName,))
    agent_row = cur.fetchone()
    agent_id = None
    subject_title = "indefinido"
    if agent_row:
        agent_id = agent_row[0]
        subject_title = f"Nova conversa com {agent_row[1]}"

    # Cria a thread no banco de dados se não existir
    if not thread_exists:
        cur.execute("""
            INSERT INTO thread (id, subject)
            VALUES (%s, %s);
        """, (thread_id, subject_title))

    # Limpa os metadados pendentes
    pending_threads.pop(thread_id, None)

    return user_id, agent_id


def save_user_message_sync(thread_id: str, user_message: str) -> int | None:
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                uid, aid = _ensure_thread_in_db_and_get_ids(cur, thread_id)
                if uid and aid:
                    cur.execute("""
                        INSERT INTO chat (user_id, agent_id, message, origem)
                        VALUES (%s, %s, %s, 'usuario')
                        RETURNING id;
                    """, (uid, aid, user_message))
                    user_chat_id = cur.fetchone()[0]
                    cur.execute("""
                        INSERT INTO chat_thread (thread_id, chat_id)
                        VALUES (%s, %s);
                    """, (thread_id, user_chat_id))
                    return user_chat_id
    except Exception as e:
        print(f"[DB-save-user] Erro: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
    return None

def save_agent_message_sync(thread_id: str, assistant_text: str) -> int | None:
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.user_id, c.agent_id FROM chat c
                    JOIN chat_thread ct ON ct.chat_id = c.id
                    WHERE ct.thread_id = %s
                    LIMIT 1;
                """, (thread_id,))
                row = cur.fetchone()
                if row:
                    uid, aid = row
                    cur.execute("""
                        INSERT INTO chat (user_id, agent_id, message, origem)
                        VALUES (%s, %s, %s, 'agente')
                        RETURNING id;
                    """, (uid, aid, assistant_text))
                    assistant_chat_id = cur.fetchone()[0]
                    cur.execute("""
                        INSERT INTO chat_thread (thread_id, chat_id)
                        VALUES (%s, %s);
                    """, (thread_id, assistant_chat_id))
                    return assistant_chat_id
    except Exception as e:
        print(f"[DB-save-agent] Erro: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
    return None

@app.post("/chat")
async def chat(request: ChatRequest, authorization: str = Header(None)):
    verify_api_key(authorization)

    thread_id    = request.threadId
    user_message = request.message.strip()
    assistant_name = request.assistantName or "SMART"

    if not user_message:
        raise HTTPException(status_code=400, detail="Mensagem vazia")

    # Busca o agente no banco de dados para pegar notebook ID, system prompt e profile.
    agent_info = get_agent_info_by_name(assistant_name)
    if not agent_info:
        raise HTTPException(status_code=404, detail=f"Agent '{assistant_name}' não encontrado.")
    
    agent_notebook_id = agent_info["id"]
    agent_system_prompt = agent_info.get("system_prompt", "Você é um assistente útil.")
    agent_profile = agent_info.get("notebooklm_profile", "default")

    if thread_id not in sessions:
        sessions[thread_id] = []
        
    is_first_message = len(sessions[thread_id]) == 0

    # 0. Persiste no banco a mensagem do usuário imediatamente para registrar o timestamp correto
    user_chat_id = await run_in_thread(save_user_message_sync, thread_id, user_message)

    # Notifica o auditor assim que a mensagem chega (antes da IA pensar)
    _notify_auditor_new_message(thread_id, user_chat_id or 0, user_message, 'usuario')

    # Broadcast para dashboards se for primeira mensagem
    if is_first_message:
        agent_title = agent_info.get("title", assistant_name)
        user_name = _get_user_name_for_thread(thread_id)
        _broadcast_admin_event({
            "type": "new_chat",
            "thread_id": thread_id,
            "user_name": user_name,
            "agent_name": agent_title,
            "first_message": user_message[:300],
        })

    # 1. Query Rewriting - expande perguntas vagas usando histórico da thread
    search_query = await rewrite_query_with_context(thread_id, user_message)

    print(f"\n{'='*50}")
    print(f"[DEBUG Chat] Thread: {thread_id}")
    print(f"[DEBUG Chat] Assistant: {assistant_name}")
    print(f"[DEBUG Chat] Original : {user_message!r}")
    print(f"[DEBUG Chat] Rewritten: {search_query!r}")

    # 2. NotebookLM - busca contexto nos manuais com a query expandida usando o ID dinâmico
    notebooklm_context = await query_notebooklm(search_query, agent_notebook_id, profile=agent_profile)

    context_preview = notebooklm_context[:200].replace('\n', ' ') + "..." if notebooklm_context else "VAZIO"
    print(f"[DEBUG Chat] Contexto : {context_preview}")
    print(f"{'='*50}\n")

    # 3. Monta histórico + contexto injetado (usa mensagem original do usuário)
    messages = build_messages(thread_id, user_message, notebooklm_context)

    # 4. OpenAI gpt-4o-mini - formata resposta com base no contexto do NotebookLM
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=10240,
            messages=[{"role": "system", "content": agent_system_prompt or "Você é um assistente útil."}] + messages,
            timeout=120.0
        )
        assistant_text = response.choices[0].message.content

    except Exception as e:
        print(f"[openai] erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao gerar resposta")

    # 5. Histórico - salva mensagem original do usuário (não a query reescrita)
    sessions[thread_id].append({"role": "user",      "content": user_message})
    sessions[thread_id].append({"role": "assistant", "content": assistant_text})

    # 5b. Persiste no banco a mensagem do agente
    assistant_chat_id = await run_in_thread(save_agent_message_sync, thread_id, assistant_text)

    # 5b2. Notificar auditores sobre a resposta da IA
    if assistant_chat_id:
        _notify_auditor_new_message(thread_id, assistant_chat_id, assistant_text, 'agente')

    # 5c. Atualiza subject automaticamente caso seja a primeira mensagem
    if is_first_message:
        asyncio.create_task(generate_and_update_subject(thread_id, user_message, assistant_text))

    # 6. Retorna no formato que o Flutter já espera
    return {
        "content": [assistant_text],
        "images":  [],
        "chat_id": assistant_chat_id
    }


async def _run_stream_processing(
    event_queue: asyncio.Queue,
    thread_id: str,
    user_message: str,
    assistant_name: str,
    agent_notebook_id: str,
    agent_system_prompt: str,
    agent_profile: str,
    is_first_message: bool,
):
    """
    Runs the full chat processing pipeline: query rewrite → NotebookLM → OpenAI.

    DECOUPLED from the SSE connection — this task always runs to completion and
    persists results to the database and session, even if the client disconnects
    mid-stream. Events are pushed to event_queue for the SSE generator to consume.

    Fixes the bug where a client disconnect during NotebookLM processing caused
    the response to be silently lost (never saved to DB or session).
    """
    def _push(event_type: str, data: dict):
        """Push an event to the SSE queue. Non-blocking, safe to call even if
        the generator has been abandoned (queue is unbounded)."""
        try:
            event_queue.put_nowait({"type": event_type, "data": data})
        except Exception:
            pass

    try:
        # --- Etapa 1: Query Rewriting ---
        _push("status", {"stage": "rewriting", "detail": "Reescrevendo consulta..."})
        search_query = await rewrite_query_with_context(thread_id, user_message)

        print(f"\n{'='*50}")
        print(f"[STREAM] Thread: {thread_id}")
        print(f"[STREAM] Assistant: {assistant_name}")
        print(f"[STREAM] Profile: {agent_profile!r}")
        print(f"[STREAM] Original : {user_message!r}")
        print(f"[STREAM] Rewritten: {search_query!r}")

        # --- Etapa 2: NotebookLM RAG ---
        _push("status", {"stage": "searching", "detail": "Buscando nos manuais..."})
        notebooklm_context = await query_notebooklm(search_query, agent_notebook_id, profile=agent_profile)

        context_preview = notebooklm_context[:200].replace('\n', ' ') + "..." if notebooklm_context else "VAZIO"
        print(f"[STREAM] Contexto : {context_preview}")
        print(f"{'='*50}\n")

        has_notebooklm_context = bool(notebooklm_context and notebooklm_context.strip())

        # --- Etapa 3: OpenAI generation (with streaming) ---
        _push("status", {"stage": "generating", "detail": "Gerando resposta com IA..."})

        messages = build_messages(thread_id, user_message, notebooklm_context)
        full_messages = [{"role": "system", "content": agent_system_prompt or "Você é um assistente útil."}] + messages

        assistant_text = ""
        openai_ok = False

        try:
            stream = await openai_client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=10240,
                messages=full_messages,
                timeout=120.0,
                stream=True,
            )

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    token_text = delta.content
                    assistant_text += token_text
                    _push("token", {"text": token_text})

            openai_ok = True

            # Log diagnóstico
            response_preview = assistant_text[:200].replace('\n', ' ') if assistant_text else "VAZIO"
            print(f"[STREAM] Resposta OpenAI: {response_preview}...")

        except Exception as e:
            print(f"[STREAM openai] erro: {e}")
            if has_notebooklm_context:
                assistant_text = notebooklm_context
                _push("fallback", {
                    "content": notebooklm_context,
                    "reason": "Não foi possível refinar a resposta com IA. Exibindo resposta direta dos manuais."
                })
            else:
                _push("error", {"detail": "Erro ao gerar resposta. Tente novamente."})
                return

        # --- Etapa 4: Persistence (ALWAYS runs, even if client disconnected) ---
        _push("status", {"stage": "saving", "detail": "Salvando..."})

        sessions[thread_id].append({"role": "user",      "content": user_message})
        sessions[thread_id].append({"role": "assistant", "content": assistant_text})

        assistant_chat_id = await run_in_thread(save_agent_message_sync, thread_id, assistant_text)

        # Notificar auditores sobre a resposta da IA
        if assistant_chat_id:
            _notify_auditor_new_message(thread_id, assistant_chat_id, assistant_text, 'agente')

        if is_first_message:
            asyncio.create_task(generate_and_update_subject(thread_id, user_message, assistant_text))

        # --- Final done event ---
        _push("done", {
            "chat_id": assistant_chat_id,
            "content": assistant_text,
            "was_fallback": not openai_ok,
        })

    except Exception as e:
        print(f"[STREAM] Erro inesperado no processamento — thread={thread_id}: {e}")
        _push("error", {"detail": "Erro interno ao processar. Tente novamente."})

    finally:
        # Sentinel: signal generator that processing is complete
        try:
            event_queue.put_nowait(None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# POST /chat/stream - SSE streaming version for the dashboard
# ---------------------------------------------------------------------------

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest, authorization: str = Header(None)):
    """
    SSE streaming version of /chat.
    Emits events: status, token, done, fallback, error.
    Falls back to raw NotebookLM response if OpenAI generation fails.
    """
    verify_api_key(authorization)

    thread_id      = request.threadId
    user_message   = request.message.strip()
    assistant_name = request.assistantName or "SMART"

    if not user_message:
        raise HTTPException(status_code=400, detail="Mensagem vazia")

    agent_info = get_agent_info_by_name(assistant_name)
    if not agent_info:
        raise HTTPException(status_code=404, detail=f"Agent '{assistant_name}' não encontrado.")

    agent_notebook_id    = agent_info["id"]
    agent_system_prompt  = agent_info.get("system_prompt", "Você é um assistente útil.")
    agent_profile        = agent_info.get("notebooklm_profile", "default")

    if thread_id not in sessions:
        sessions[thread_id] = []

    is_first_message = len(sessions[thread_id]) == 0

    # 0. Persiste no banco a mensagem do usuário imediatamente para registrar o timestamp correto
    user_chat_id = await run_in_thread(save_user_message_sync, thread_id, user_message)

    # Notifica o auditor assim que a mensagem chega (antes da IA pensar)
    _notify_auditor_new_message(thread_id, user_chat_id or 0, user_message, 'usuario')

    # Broadcast para dashboards se for primeira mensagem
    if is_first_message:
        agent_title = agent_info.get("title", assistant_name)
        user_name = _get_user_name_for_thread(thread_id)
        _broadcast_admin_event({
            "type": "new_chat",
            "thread_id": thread_id,
            "user_name": user_name,
            "agent_name": agent_title,
            "first_message": user_message[:300],
        })

    def _sse(event: str, data: dict) -> str:
        """Format a single SSE event."""
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event}\ndata: {payload}\n\n"

    # Queue for communication between processing task and SSE generator.
    # Processing runs as a background task decoupled from the SSE connection,
    # ensuring responses are ALWAYS persisted to DB and session even if the
    # client disconnects mid-stream.
    event_queue: asyncio.Queue = asyncio.Queue()

    asyncio.create_task(
        _run_stream_processing(
            event_queue=event_queue,
            thread_id=thread_id,
            user_message=user_message,
            assistant_name=assistant_name,
            agent_notebook_id=agent_notebook_id,
            agent_system_prompt=agent_system_prompt,
            agent_profile=agent_profile,
            is_first_message=is_first_message,
        )
    )

    async def event_generator():
        """Reads events from the processing queue and streams as SSE to the client.
        If the client disconnects, the processing task continues in background,
        ensuring the response is always persisted."""
        try:
            while True:
                event = await event_queue.get()
                if event is None:  # Sentinel: processing complete
                    break
                yield _sse(event["type"], event["data"])
        except (asyncio.CancelledError, GeneratorExit):
            print(f"[STREAM] Cliente desconectou — thread={thread_id} (processamento continua em background)")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # crucial for nginx to not buffer SSE
        },
    )

class MessageFeedbackRequest(BaseModel):
    thumb: int  # 1 ou -1
    text: Optional[str] = None


@app.put("/chat/{chat_id}/feedback")
async def update_message_feedback(chat_id: int, request: MessageFeedbackRequest, authorization: str = Header(None)):
    """Atualiza o feedback da mensagem."""
    verify_api_key(authorization)
    thread_id = None
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE chat SET feedback_thumb = %s, feedback_text = %s WHERE id = %s RETURNING id;
                """, (request.thumb, request.text, chat_id))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Chat não encontrado")
                
                # Buscar thread_id associado ao chat_id
                cur.execute("""
                    SELECT thread_id FROM chat_thread WHERE chat_id = %s LIMIT 1;
                """, (chat_id,))
                t_row = cur.fetchone()
                if t_row:
                    thread_id = t_row["thread_id"] if isinstance(t_row, dict) else t_row[0]
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao atualizar feedback de mensagem: {e}")
        raise HTTPException(status_code=500, detail="Erro ao atualizar feedback")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    if thread_id:
        _broadcast_thread_update(thread_id)

    return {"status": "ok", "chat_id": chat_id, "thumb": request.thumb}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions_ativas": len(sessions)}


@app.get("/feedbacks")
async def list_feedbacks(
    page: int = 1,
    limit: int = 30,
    search: str = "",
    thumb: Optional[int] = Query(None),
    authorization: str = Header(None),
):
    """
    Retorna lista paginada de mensagens que possuem feedback (texto ou thumb).
    """
    verify_api_key(authorization)
    offset = (page - 1) * limit

    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Condição base: ou tem texto, ou tem thumb (feedback acionado)
                conditions = ["(c.feedback_text IS NOT NULL AND c.feedback_text <> '' OR c.feedback_thumb IS NOT NULL)"]
                params: list = []

                if search.strip():
                    conditions.append("(c.feedback_text ILIKE %s OR c.message ILIKE %s OR u.email ILIKE %s OR u.name ILIKE %s)")
                    like = f"%{search.strip()}%"
                    params += [like, like, like, like]

                if thumb is not None:
                    # Garantir que thumb seja interpretado como int
                    conditions.append("c.feedback_thumb = %s")
                    params.append(int(thumb))

                where = " AND ".join(conditions)

                query = f"""
                    SELECT c.id        AS chat_id,
                           c.message   AS message,
                           c.feedback_thumb,
                           c.feedback_text,
                           c.created_at,
                           ct.feedback_rating,
                           t.id        AS thread_id,
                           t.subject   AS thread_subject,
                           u.name      AS user_name,
                           u.email     AS user_email,
                           a.name      AS agent_name,
                           a.title     AS agent_title
                    FROM chat c
                    LEFT JOIN chat_thread ct ON ct.chat_id = c.id
                    LEFT JOIN thread t       ON t.id = ct.thread_id
                    LEFT JOIN "user" u       ON u.id = c.user_id
                    LEFT JOIN agent a        ON a.id = c.agent_id
                    WHERE {where}
                    ORDER BY c.created_at DESC, c.id DESC
                    LIMIT %s OFFSET %s;
                """
                cur.execute(query, params + [limit, offset])
                rows = cur.fetchall()

                count_query = f"""
                    SELECT COUNT(*) AS total
                    FROM chat c
                    LEFT JOIN chat_thread ct ON ct.chat_id = c.id
                    LEFT JOIN thread t       ON t.id = ct.thread_id
                    LEFT JOIN "user" u       ON u.id = c.user_id
                    LEFT JOIN agent a        ON a.id = c.agent_id
                    WHERE {where};
                """
                cur.execute(count_query, params)
                total = cur.fetchone()["total"]

        feedbacks = []
        for r in rows:
            feedbacks.append({
                "chat_id": r["chat_id"],
                "message": r["message"],
                "feedback_thumb": r["feedback_thumb"],
                "feedback_text": r["feedback_text"],
                "feedback_rating": r["feedback_rating"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "thread_id": r["thread_id"],
                "thread_subject": r["thread_subject"],
                "user_name": r["user_name"],
                "user_email": r["user_email"],
                "agent_name": r["agent_name"],
                "agent_title": r["agent_title"],
            })

        return {
            "feedbacks": feedbacks,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if limit else 1,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[feedbacks] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar feedbacks.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.get("/refreshAuth")
async def refresh_auth():
    """
    Renova a autenticação do NotebookLM via auth_manager.
    Lê as variáveis MAC_HOST e MAC_USER do ambiente.
    """
    import importlib.util, sys
    from pathlib import Path as _Path

    auth_manager_path = _Path(__file__).parent / "auth_manager.py"
    spec = importlib.util.spec_from_file_location("auth_manager", auth_manager_path)
    auth_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(auth_module)

    mac_host = os.environ.get("MAC_HOST")
    mac_user = os.environ.get("MAC_USER")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: auth_module.check_and_renew(mac_host=mac_host, mac_user=mac_user)
        )
        return {"status": "ok", "renewed": result}
    except SystemExit:
        raise HTTPException(status_code=500, detail="Renovação de autenticação falhou. Intervenção manual necessária.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao renovar autenticação: {e}")


@app.post("/uploadAuthState")
async def upload_auth_state(
    file: UploadFile = File(...),
    profile: str = "default",
):
    """
    Recebe o storage_state.json do NotebookLM via upload e salva no servidor.
    Salva no diretório do profile especificado (~/.notebooklm/profiles/<profile>/).
    Após salvar, valida executando 'notebooklm -p <profile> list'.
    """

    from pathlib import Path as _Path
    import shutil

    # Sanitizar nome do profile
    profile = profile.strip() or "default"

    # --- 1. Ler conteúdo ---
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")

    # --- 2. Validar JSON e estrutura mínima ---
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Arquivo JSON inválido: {e}")

    if not isinstance(data, dict) or "cookies" not in data:
        raise HTTPException(
            status_code=400,
            detail="Estrutura inválida: o arquivo deve conter a chave 'cookies'. "
                   "Certifique-se de que é o ~/.notebooklm/storage_state.json correto."
        )

    cookies_count = len(data.get("cookies", []))

    # --- 3. Salvar no diretório do profile (com backup do anterior) ---
    profile_dir = _Path.home() / ".notebooklm" / "profiles" / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    session_file = profile_dir / "storage_state.json"

    if session_file.exists():
        backup = session_file.with_suffix(".json.bak")
        shutil.copy2(session_file, backup)
        print(f"[uploadAuthState] backup salvo em {backup}")

    session_file.write_bytes(content)
    print(f"[uploadAuthState] profile={profile} storage_state.json atualizado ({len(content)} bytes, {cookies_count} cookies)")

    # --- 4. Validar executando notebooklm -p <profile> list ---
    valid = False
    try:
        cmd = _get_notebooklm_cmd(profile, "list")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        valid = proc.returncode == 0
        if not valid:
            print(f"[uploadAuthState] validação falhou (profile={profile}): {stderr.decode()[:200]}")
        else:
            print(f"[uploadAuthState] sessão validada com sucesso (profile={profile}).")
    except asyncio.TimeoutError:
        print(f"[uploadAuthState] timeout na validação (profile={profile})")
    except Exception as e:
        print(f"[uploadAuthState] erro na validação (profile={profile}): {e}")

    return {
        "status": "ok",
        "profile": profile,
        "saved": True,
        "valid": valid,
        "bytes": len(content),
        "cookies_count": cookies_count,
        "message": (
            f"✅ Autenticação renovada e validada com sucesso! (profile: {profile})"
            if valid else
            f"⚠️ Arquivo salvo, mas a validação falhou. O token pode estar expirado - tente gerar um novo no Mac. (profile: {profile})"
        ),
    }



@app.get("/authStatus")
async def auth_status(profile: str = "default"):
    """
    Verifica o status do storage_state.json do NotebookLM no servidor para um profile específico.
    Retorna se o arquivo existe, quantos cookies tem, quais expiram e quando.
    """
    from pathlib import Path as _Path

    profile = profile.strip() or "default"

    # Tentar profile-based path primeiro, fallback para legacy path
    profile_file = _Path.home() / ".notebooklm" / "profiles" / profile / "storage_state.json"
    legacy_file = _Path.home() / ".notebooklm" / "storage_state.json"

    if profile_file.exists():
        session_file = profile_file
    elif profile == "default" and legacy_file.exists():
        session_file = legacy_file
    else:
        return {
            "profile": profile,
            "exists": False,
            "valid": False,
            "cookies_count": 0,
            "expires_at": None,
            "file_age_hours": None,
            "message": f"Arquivo storage_state.json não encontrado para o profile '{profile}'.",
        }

    try:
        raw = session_file.read_bytes()
        data = json.loads(raw)
        cookies = data.get("cookies", [])
        cookies_count = len(cookies)

        # Encontrar a data de expiração mais próxima entre todos os cookies
        now_ts = datetime.utcnow().timestamp()
        expires_timestamps = []
        for c in cookies:
            exp = c.get("expires")
            if exp and isinstance(exp, (int, float)) and exp > 0:
                expires_timestamps.append(exp)

        if expires_timestamps:
            nearest_expiry_ts = min(expires_timestamps)
            nearest_expiry = datetime.utcfromtimestamp(nearest_expiry_ts)
            is_expired = nearest_expiry_ts < now_ts
            expires_at_iso = nearest_expiry.isoformat() + "Z"
        else:
            is_expired = False
            expires_at_iso = None

        # Idade do arquivo em horas
        file_mtime = session_file.stat().st_mtime
        file_age_hours = round((now_ts - file_mtime) / 3600, 1)

        # Validar executando notebooklm -p <profile> list (rápido, timeout curto)
        valid_session = False
        try:
            cmd = _get_notebooklm_cmd(profile, "list")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            valid_session = proc.returncode == 0
        except (asyncio.TimeoutError, FileNotFoundError):
            valid_session = not is_expired  # fallback: inferir pelo cookie

        if valid_session:
            message = "✅ Sessão ativa e válida."
        elif is_expired:
            message = "⚠️ Token expirado. Renove a autenticação."
        else:
            message = "⚠️ Sessão pode estar inválida. Recomenda-se renovar."

        return {
            "profile": profile,
            "exists": True,
            "valid": valid_session,
            "cookies_count": cookies_count,
            "expires_at": expires_at_iso,
            "file_age_hours": file_age_hours,
            "message": message,
        }
    except Exception as e:
        return {
            "profile": profile,
            "exists": True,
            "valid": False,
            "cookies_count": 0,
            "expires_at": None,
            "file_age_hours": None,
            "message": f"Erro ao verificar o arquivo: {e}",
        }


@app.get("/authStatus/all")
async def auth_status_all():
    """
    Retorna o status de autenticação de TODOS os profiles distintos usados pelos agentes.
    Inclui quais agentes estão associados a cada profile.
    """
    from pathlib import Path as _Path

    # 1. Buscar todos os profiles distintos do banco
    profiles_map: dict[str, list[dict]] = {}
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT COALESCE(notebooklm_profile, 'default') as profile,
                           id, name, title, active
                    FROM agent
                    WHERE active = TRUE
                    ORDER BY notebooklm_profile, sort_order ASC;
                """)
                for row in cur.fetchall():
                    p = row["profile"]
                    if p not in profiles_map:
                        profiles_map[p] = []
                    profiles_map[p].append({
                        "id": row["id"],
                        "name": row["name"],
                        "title": row["title"],
                    })
    except Exception as e:
        print(f"[authStatus/all] Erro ao buscar profiles: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao buscar profiles: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    if not profiles_map:
        profiles_map["default"] = []

    # 1.5. Buscar também os profiles que existem no disco (possuem storage_state.json) mas sem agentes
    profiles_dir = _Path.home() / ".notebooklm" / "profiles"
    if profiles_dir.exists() and profiles_dir.is_dir():
        for p_dir in profiles_dir.iterdir():
            if p_dir.is_dir() and (p_dir / "storage_state.json").exists():
                p_name = p_dir.name
                if p_name not in profiles_map:
                    profiles_map[p_name] = []

    # 2. Para cada profile, verificar o status do storage_state.json
    results = []
    for profile_name, agents_list in sorted(profiles_map.items()):
        profile_file = _Path.home() / ".notebooklm" / "profiles" / profile_name / "storage_state.json"
        legacy_file = _Path.home() / ".notebooklm" / "storage_state.json"

        if profile_file.exists():
            session_file = profile_file
        elif profile_name == "default" and legacy_file.exists():
            session_file = legacy_file
        else:
            results.append({
                "profile": profile_name,
                "agents": agents_list,
                "exists": False,
                "valid": False,
                "cookies_count": 0,
                "expires_at": None,
                "file_age_hours": None,
                "message": f"Sem autenticação configurada.",
            })
            continue

        try:
            raw = session_file.read_bytes()
            data = json.loads(raw)
            cookies = data.get("cookies", [])
            cookies_count = len(cookies)

            now_ts = datetime.utcnow().timestamp()
            expires_timestamps = []
            for c in cookies:
                exp = c.get("expires")
                if exp and isinstance(exp, (int, float)) and exp > 0:
                    expires_timestamps.append(exp)

            if expires_timestamps:
                nearest_expiry_ts = min(expires_timestamps)
                nearest_expiry = datetime.utcfromtimestamp(nearest_expiry_ts)
                is_expired = nearest_expiry_ts < now_ts
                expires_at_iso = nearest_expiry.isoformat() + "Z"
            else:
                is_expired = False
                expires_at_iso = None

            file_mtime = session_file.stat().st_mtime
            file_age_hours = round((now_ts - file_mtime) / 3600, 1)

            # Quick validation — skip for speed, infer from cookie expiry
            valid_session = not is_expired

            if valid_session:
                message = "✅ Sessão ativa e válida."
            elif is_expired:
                message = "⚠️ Token expirado. Renove a autenticação."
            else:
                message = "⚠️ Sessão pode estar inválida."

            results.append({
                "profile": profile_name,
                "agents": agents_list,
                "exists": True,
                "valid": valid_session,
                "cookies_count": cookies_count,
                "expires_at": expires_at_iso,
                "file_age_hours": file_age_hours,
                "message": message,
            })
        except Exception as e:
            results.append({
                "profile": profile_name,
                "agents": agents_list,
                "exists": True,
                "valid": False,
                "cookies_count": 0,
                "expires_at": None,
                "file_age_hours": None,
                "message": f"Erro: {e}",
            })

    return {"profiles": results}


@app.get("/notebooklm-version")
async def get_notebooklm_version():
    """
    Retorna a versão do NotebookLM CLI instalada no servidor.
    Executa 'notebooklm --version'.
    """
    try:
        import sys
        env = os.environ.copy()
        venv_bin = os.path.dirname(sys.executable)
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"

        proc = await asyncio.create_subprocess_exec(
            "notebooklm", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            version_str = stdout.decode().strip()
            return {"version": version_str}
        else:
            err_msg = stderr.decode().strip()
            print(f"[get_notebooklm_version] Erro ao obter versão ({proc.returncode}): {err_msg}")
            return {"version": "Desconhecido", "error": f"Erro {proc.returncode}: {err_msg}"}
    except Exception as e:
        print(f"[get_notebooklm_version] Exceção ao obter versão: {e}")
        return {"version": "Desconhecido", "error": str(e)}


class RenameProfileRequest(BaseModel):
    old_profile: str
    new_profile: str


@app.post("/renameProfile")
async def rename_profile(request: RenameProfileRequest):
    """
    Renomeia um profile de autenticação:
    1. Atualiza todos os agentes que usam o old_profile no banco de dados para new_profile.
    2. Se existir uma pasta de sessão on-disk (~/.notebooklm/profiles/old_profile),
       renomeia a pasta para new_profile.
    """
    from pathlib import Path as _Path
    import shutil
    import re
    
    old_p = re.sub(r'[^a-z0-9-_]', '', request.old_profile.strip().lower())
    new_p = re.sub(r'[^a-z0-9-_]', '', request.new_profile.strip().lower())
    
    if not old_p or not new_p:
        raise HTTPException(status_code=400, detail="Nomes de profile inválidos.")
        
    if old_p == new_p:
        return {"status": "ok", "message": "Nomes iguais, nenhuma alteração necessária."}
        
    disk_renamed = False
    old_dir = _Path.home() / ".notebooklm" / "profiles" / old_p
    new_dir = _Path.home() / ".notebooklm" / "profiles" / new_p
    
    legacy_file = _Path.home() / ".notebooklm" / "storage_state.json"
    if old_p == "default" and not old_dir.exists() and legacy_file.exists():
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_file, new_dir / "storage_state.json")
            disk_renamed = True
            print(f"[renameProfile] Sessão legado copiada de default para {new_p}")
        except Exception as e:
            print(f"[renameProfile] Erro ao migrar arquivo legado: {e}")
            
    if old_dir.exists() and old_dir.is_dir():
        try:
            if new_dir.exists():
                for item in old_dir.iterdir():
                    shutil.move(str(item), str(new_dir / item.name))
                old_dir.rmdir()
            else:
                shutil.move(str(old_dir), str(new_dir))
            disk_renamed = True
            print(f"[renameProfile] Pasta renomeada de {old_p} para {new_p}")
        except Exception as e:
            print(f"[renameProfile] Erro ao renomear pasta: {e}")
            raise HTTPException(status_code=500, detail=f"Erro ao renomear arquivos no disco: {e}")
            
    db_updated = 0
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                if old_p == "default":
                    cur.execute("""
                        UPDATE agent
                        SET notebooklm_profile = %s, modification = NOW()
                        WHERE notebooklm_profile IS NULL OR LOWER(TRIM(notebooklm_profile)) = 'default'
                        RETURNING id;
                    """, (new_p,))
                else:
                    cur.execute("""
                        UPDATE agent
                        SET notebooklm_profile = %s, modification = NOW()
                        WHERE LOWER(TRIM(notebooklm_profile)) = LOWER(TRIM(%s))
                        RETURNING id;
                    """, (new_p, old_p))
                rows = cur.fetchall()
                db_updated = len(rows)
                print(f"[renameProfile] {db_updated} agentes atualizados de {old_p} para {new_p}")
    except Exception as e:
        print(f"[renameProfile] Erro no banco de dados: {e}")
        raise HTTPException(status_code=500, detail=f"Erro no banco de dados: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
            
    return {
        "status": "ok",
        "old_profile": old_p,
        "new_profile": new_p,
        "disk_renamed": disk_renamed,
        "agents_updated": db_updated,
        "message": f"Profile renomeado com sucesso! {db_updated} agente(s) e arquivos de sessão atualizados."
    }


@app.get("/dashboard/totals")
async def dashboard_totals(days: int = 30):
    """
    Retorna o total geral de chats e usuários ativos nos últimos N dias,
    sem limite de paginação.
    """
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT 
                        COUNT(c.id) as total_chats,
                        COUNT(DISTINCT c.user_id) as total_users
                    FROM chat c
                    JOIN "user" u ON u.id = c.user_id
                    WHERE c.origem = 'usuario'
                      AND c.created_at >= NOW() - INTERVAL '%s days'
                      AND u.email NOT IN ('admin@test.com');
                """, (days,))
                row = cur.fetchone()
                return {
                    "total_chats": row["total_chats"] or 0,
                    "total_users": row["total_users"] or 0
                }
    except Exception as e:
        print(f"[dashboard-totals] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar totais do dashboard.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.get("/dashboard/chats-per-user")
async def dashboard_chats_per_user(days: int = 30, limit: int = 10):
    """
    Retorna o número de chats por usuário nos últimos N dias (padrão 30).
    Top 10 usuários com mais chats. Agrupa por usuário e por dia.
    """
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Top 10 users by total chats in the period
                cur.execute("""
                    SELECT u.name, u.email, DATE(c.created_at) as day, COUNT(*) as total
                    FROM chat c
                    JOIN "user" u ON u.id = c.user_id
                    WHERE c.origem = 'usuario'
                      AND c.created_at >= NOW() - INTERVAL '%s days'
                      AND u.email NOT IN ('admin@test.com')
                    GROUP BY u.name, u.email, DATE(c.created_at)
                    ORDER BY day ASC, total DESC;
                """, (days,))
                rows = cur.fetchall()

                # Identify top 10 users by total volume
                cur.execute("""
                    WITH user_totals AS (
                        SELECT u.id, u.name, u.email, COUNT(DISTINCT c.id) as total
                        FROM chat c
                        JOIN "user" u ON u.id = c.user_id
                        WHERE c.origem = 'usuario'
                          AND c.created_at >= NOW() - INTERVAL '%s days'
                          AND u.email NOT IN ('admin@test.com')
                        GROUP BY u.id, u.name, u.email
                    ),
                    user_feedbacks AS (
                        SELECT c.user_id, AVG(ct.feedback_rating) as avg_rating,
                               SUM(CASE WHEN c.feedback_thumb = 1 THEN 1 ELSE 0 END) as thumb_up,
                               SUM(CASE WHEN c.feedback_thumb = -1 THEN 1 ELSE 0 END) as thumb_down
                        FROM chat_thread ct
                        JOIN chat c ON c.id = ct.chat_id
                        WHERE c.created_at >= NOW() - INTERVAL '%s days'
                        GROUP BY c.user_id
                    )
                    SELECT t.name, t.email, t.total, ROUND(f.avg_rating, 1) as avg_rating, 
                           f.thumb_up, f.thumb_down
                    FROM user_totals t
                    LEFT JOIN user_feedbacks f ON f.user_id = t.id
                    ORDER BY t.total DESC
                    LIMIT %s;
                """, (days, days, limit))
                top_users = []
                for r in cur.fetchall():
                    up = r["thumb_up"] or 0
                    down = r["thumb_down"] or 0
                    total_thumbs = up + down
                    thumb_avg = round((up / total_thumbs) * 100) if total_thumbs > 0 else None
                    top_users.append({
                        "name": r["name"] or r["email"], 
                        "email": r["email"], 
                        "total": r["total"], 
                        "avg_rating": float(r["avg_rating"]) if r["avg_rating"] is not None else None,
                        "thumb_avg": thumb_avg,
                        "thumb_up": up,
                        "thumb_down": down
                    })

        # Build series per user (daily data)
        from collections import defaultdict as _dd
        user_daily: dict = _dd(lambda: _dd(int))
        for row in rows:
            label = row["name"] or row["email"]
            day_str = row["day"].isoformat() if hasattr(row["day"], "isoformat") else str(row["day"])
            user_daily[label][day_str] += row["total"]

        top_labels = [u["name"] or u["email"] for u in top_users]

        # Collect all days in range
        all_days = sorted({
            (row["day"].isoformat() if hasattr(row["day"], "isoformat") else str(row["day"]))
            for row in rows
        })

        series = []
        for label in top_labels:
            series.append({
                "name": label,
                "data": [user_daily[label].get(d, 0) for d in all_days]
            })

        return {
            "categories": all_days,
            "series": series,
            "top_users": top_users,
        }
    except Exception as e:
        print(f"[dashboard] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar dados do dashboard.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

@app.get("/dashboard/chats-per-agent")
async def dashboard_chats_per_agent(days: int = 30):
    """
    Retorna o número de atendimentos por agente nos últimos N dias.
    Agrupa por agente e por dia.
    """
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.title as name, DATE(c.created_at) as day, COUNT(*) as total
                    FROM chat c
                    JOIN agent a ON a.id = c.agent_id
                    JOIN "user" u ON u.id = c.user_id
                    WHERE c.origem = 'usuario'
                      AND c.created_at >= NOW() - INTERVAL '%s days'
                      AND u.email NOT IN ('admin@test.com')
                    GROUP BY a.title, DATE(c.created_at)
                    ORDER BY day ASC;
                """, (days,))
                rows = cur.fetchall()

                cur.execute("""
                    SELECT a.title as name, COUNT(*) as total
                    FROM chat c
                    JOIN agent a ON a.id = c.agent_id
                    JOIN "user" u ON u.id = c.user_id
                    WHERE c.origem = 'usuario'
                      AND c.created_at >= NOW() - INTERVAL '%s days'
                      AND u.email NOT IN ('admin@test.com')
                    GROUP BY a.title
                    ORDER BY total DESC;
                """, (days,))
                agents = [{"name": r["name"], "total": r["total"]} for r in cur.fetchall()]

        from collections import defaultdict as _dd
        agent_daily: dict = _dd(lambda: _dd(int))
        for row in rows:
            label = row["name"]
            day_str = row["day"].isoformat() if hasattr(row["day"], "isoformat") else str(row["day"])
            agent_daily[label][day_str] += row["total"]

        all_labels = [a["name"] for a in agents]

        all_days = sorted({
            (row["day"].isoformat() if hasattr(row["day"], "isoformat") else str(row["day"]))
            for row in rows
        })

        series = []
        for label in all_labels:
            series.append({
                "name": label,
                "data": [agent_daily[label].get(d, 0) for d in all_days]
            })

        return {
            "categories": all_days,
            "series": series,
            "agents": agents,
        }
    except Exception as e:
        print(f"[dashboard] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar dados do dashboard por agente.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

@app.get("/dashboard/feedback-per-agent")
async def dashboard_feedback_per_agent(days: int = 30):
    """
    Retorna a média de avaliação (feedback_rating) por agente nos últimos N dias.
    """
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.title as name, 
                           ROUND(AVG(ct.feedback_rating), 1) as avg_rating, 
                           COUNT(ct.feedback_rating) as total_ratings,
                           SUM(CASE WHEN c.feedback_thumb = 1 THEN 1 ELSE 0 END) as thumb_up,
                           SUM(CASE WHEN c.feedback_thumb = -1 THEN 1 ELSE 0 END) as thumb_down
                    FROM chat_thread ct
                    JOIN chat c ON c.id = ct.chat_id
                    JOIN agent a ON a.id = c.agent_id
                    JOIN "user" u ON u.id = c.user_id
                    WHERE (ct.feedback_rating IS NOT NULL OR c.feedback_thumb IS NOT NULL)
                      AND c.created_at >= NOW() - INTERVAL '%s days'
                      AND a.active = TRUE
                      AND u.email NOT IN ('admin@test.com')
                    GROUP BY a.title
                    ORDER BY avg_rating DESC, total_ratings DESC;
                """, (days,))
                feedbacks_raw = cur.fetchall()
                
                feedbacks = []
                for r in feedbacks_raw:
                    up = r["thumb_up"] or 0
                    down = r["thumb_down"] or 0
                    total = up + down
                    thumb_avg = round((up / total) * 100) if total > 0 else None
                    feedbacks.append({
                        "name": r["name"],
                        "avg_rating": float(r["avg_rating"]) if r["avg_rating"] is not None else None,
                        "total_ratings": r["total_ratings"],
                        "thumb_avg": thumb_avg,
                        "thumb_up": up,
                        "thumb_down": down
                    })

        categories = [r["name"] for r in feedbacks]
        series = [{
            "name": "Média de Estrelas",
            "data": [float(r["avg_rating"]) if r["avg_rating"] is not None else None for r in feedbacks]
        }]
        
        return {
            "categories": categories,
            "series": series,
            "feedbacks": feedbacks
        }
    except Exception as e:
        print(f"[dashboard] Erro feedback: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar avaliação dos agentes.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.get("/agents/all")
async def get_agents_all():
    """Retorna todos os agentes com todos os campos (para edição no dashboard)."""
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, title, name, system_prompt, email, overview,
                           sort_order, active, creation, modification,
                           COALESCE(notebooklm_profile, 'default') as notebooklm_profile,
                           hide
                    FROM agent
                    ORDER BY sort_order ASC, title ASC;
                """)
                rows = cur.fetchall()
                agents = []
                for r in rows:
                    agents.append({
                        "id": r["id"],
                        "title": r["title"],
                        "name": r["name"],
                        "system_prompt": r["system_prompt"],
                        "email": r["email"],
                        "overview": r["overview"],
                        "sort_order": r["sort_order"],
                        "active": r["active"],
                        "creation": r["creation"].isoformat() if r["creation"] else None,
                        "modification": r["modification"].isoformat() if r["modification"] else None,
                        "notebooklm_profile": r["notebooklm_profile"],
                        "hide": r["hide"],
                        # faq_content intentionally omitted from list (fetched on-demand via GET /agents/{id})
                    })
                return {"agents": agents}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar agentes: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.get("/agents/{agent_id}")
async def get_agent_by_id(agent_id: str):
    """Retorna um agente pelo ID, incluindo faq_content (para edição no dashboard)."""
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, title, name, system_prompt, email, overview,
                           sort_order, active, creation, modification, faq_content,
                           COALESCE(notebooklm_profile, 'default') as notebooklm_profile,
                           hide
                    FROM agent WHERE id = %s;
                """, (agent_id,))
                r = cur.fetchone()
                if not r:
                    raise HTTPException(status_code=404, detail="Agente não encontrado.")
                return {
                    "id": r["id"],
                    "title": r["title"],
                    "name": r["name"],
                    "system_prompt": r["system_prompt"],
                    "email": r["email"],
                    "overview": r["overview"],
                    "sort_order": r["sort_order"],
                    "active": r["active"],
                    "creation": r["creation"].isoformat() if r["creation"] else None,
                    "modification": r["modification"].isoformat() if r["modification"] else None,
                    "faq_content": r["faq_content"] or "",
                    "notebooklm_profile": r["notebooklm_profile"],
                    "hide": r["hide"],
                }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar agente: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


class AgentUpdateRequest(BaseModel):
    title: Optional[str] = None
    name: Optional[str] = None
    system_prompt: Optional[str] = None
    email: Optional[str] = None
    overview: Optional[str] = None
    sort_order: Optional[int] = None
    active: Optional[bool] = None
    faq_content: Optional[str] = None
    notebooklm_profile: Optional[str] = None
    hide: Optional[bool] = None


@app.put("/agents/{agent_id}")
async def update_agent(agent_id: str, request: AgentUpdateRequest):
    """Atualiza campos de um agente pelo ID."""
    from typing import Any as _Any
    fields: dict[str, _Any] = {k: v for k, v in request.dict().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar.")

    fields["modification"] = datetime.utcnow()
    set_clause = ", ".join(f"{k} = %({k})s" for k in fields)
    fields["agent_id"] = agent_id

    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"""
                    UPDATE agent SET {set_clause}
                    WHERE id = %(agent_id)s
                    RETURNING id, title, name, system_prompt, email, overview,
                              sort_order, active, creation, modification,
                              COALESCE(notebooklm_profile, 'default') as notebooklm_profile,
                              hide;
                """, fields)
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Agente não encontrado.")
                return {
                    **dict(row),
                    "creation": row["creation"].isoformat() if row["creation"] else None,
                    "modification": row["modification"].isoformat() if row["modification"] else None,
                }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao atualizar agente: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.post("/agents/{agent_id}/sync-faq")
async def sync_agent_faq_to_notebooklm(agent_id: str):
    """
    Pega o faq_content atual do agente no banco e sincroniza (recria) a
    source FAQ no notebook NotebookLM correspondente.
    Reutiliza a mesma lógica do endpoint /thread/{id}/add-to-faq.
    """
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, title, faq_content, COALESCE(notebooklm_profile, 'default') as notebooklm_profile FROM agent WHERE id = %s;",
                    (agent_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Agente não encontrado.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar agente: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    faq_content = row["faq_content"] or ""
    if not faq_content.strip():
        raise HTTPException(status_code=400, detail="FAQ vazia. Adicione conteúdo antes de sincronizar.")

    agent_profile = row.get("notebooklm_profile", "default")
    faq_title = _build_faq_source_title(row["title"])
    add_result = await _add_faq_source_to_notebook(agent_id, faq_title, faq_content, profile=agent_profile)

    return {
        "status": "ok" if add_result["success"] else "error",
        "agent_id": agent_id,
        "faq_title": faq_title,
        "notebook_result": add_result,
    }


# ---------------------------------------------------------------------------
# Consultas de Histórico
# ---------------------------------------------------------------------------

@app.get("/history")
async def get_history(
    email: str,
    page: int = 1,
    limit: int = 30,
    authorization: str = Header(None)
):
    """
    Retorna histórico de threads de um usuário específico via email, com paginação.
    """
    verify_api_key(authorization)
    offset = (page - 1) * limit
    
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Obter user_id pelo email
                cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
                user_row = cur.fetchone()
                if not user_row:
                    return {"threads": [], "total": 0, "page": page, "limit": limit}
                user_id = user_row["id"]

                # Retorna dados das threads, incluindo a data da primeira mensagem e feedback_rating...
                cur.execute("""
                    SELECT t.id as thread_id, t.subject, a.name as agent_name, a.title as agent_title,
                           MIN(c.created_at) as created_at,
                           MAX(ct.feedback_rating) as feedback_rating
                    FROM thread t
                    JOIN chat_thread ct ON ct.thread_id = t.id
                    JOIN chat c ON ct.chat_id = c.id
                    JOIN agent a ON c.agent_id = a.id
                    WHERE c.user_id = %s
                    GROUP BY t.id, t.subject, a.name, a.title
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s;
                """, (user_id, limit, offset))
                threads = cur.fetchall()
                
                # Conta total para ajudar na paginação
                cur.execute("""
                    SELECT COUNT(DISTINCT t.id) as total
                    FROM thread t
                    JOIN chat_thread ct ON ct.thread_id = t.id
                    JOIN chat c ON ct.chat_id = c.id
                    WHERE c.user_id = %s;
                """, (user_id,))
                total = cur.fetchone()["total"]

        return {
            "threads": threads,
            "total": total,
            "page": page,
            "limit": limit
        }
    except Exception as e:
        print(f"[history] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar histórico.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.get("/thread/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str,
    authorization: str = Header(None)
):
    """
    Retorna as mensagens de uma thread específica.
    """
    verify_api_key(authorization)
    
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT c.id, c.message, c.origem, c.created_at, c.feedback_thumb, c.feedback_text,
                           ct.feedback_rating, c.auditor_id,
                           aud.name AS auditor_name, aud.nickname AS auditor_nickname,
                           aud.icon_svg AS auditor_icon_svg
                    FROM chat c
                    JOIN chat_thread ct ON ct.chat_id = c.id
                    LEFT JOIN auditor aud ON aud.id = c.auditor_id
                    WHERE ct.thread_id = %s
                    ORDER BY c.created_at ASC, c.id ASC;
                """, (thread_id,))
                messages = cur.fetchall()
                
        # Formatar mensagens para o padrão esperado pelo front: { role: 'user'|'assistant', content: '...' }
        # Pulamos a mensagem interna de "Thread iniciada"
        formatted_messages = []
        for m in messages:
            if m["message"].startswith("Thread iniciada:"):
                continue
            if m["origem"] == "usuario":
                role = "user"
            elif m["origem"] == "auditor":
                role = "auditor"
            else:
                role = "assistant"
            msg_data = {
                "id": m["id"],
                "role": role,
                "content": m["message"],
                "feedback_thumb": m["feedback_thumb"],
                "feedback_text": m["feedback_text"],
                "feedback_rating": m["feedback_rating"],
                "created_at": m["created_at"].isoformat() if m.get("created_at") else None,
            }
            if role == "auditor":
                msg_data["auditor_id"] = m.get("auditor_id")
                msg_data["auditor_name"] = m.get("auditor_name")
                msg_data["auditor_nickname"] = m.get("auditor_nickname") or m.get("auditor_name")
                msg_data["auditor_icon_svg"] = m.get("auditor_icon_svg")
            formatted_messages.append(msg_data)
            
        return {"messages": formatted_messages}
    except Exception as e:
        print(f"[thread_messages] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar mensagens.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.delete("/thread/{thread_id}")
async def delete_thread(
    thread_id: str,
    authorization: str = Header(None)
):
    """
    Exclui uma thread e todos os seus chats associados.
    """
    verify_api_key(authorization)

    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                # Busca os IDs dos chats desta thread
                cur.execute(
                    "SELECT chat_id FROM chat_thread WHERE thread_id = %s",
                    (thread_id,)
                )
                chat_ids = [row[0] for row in cur.fetchall()]

                if chat_ids:
                    # Remove os vínculos na tabela chat_thread
                    cur.execute(
                        "DELETE FROM chat_thread WHERE thread_id = %s",
                        (thread_id,)
                    )
                    # Remove os registros de chat
                    cur.execute(
                        "DELETE FROM chat WHERE id = ANY(%s)",
                        (chat_ids,)
                    )

                # Remove a thread
                cur.execute("DELETE FROM thread WHERE id = %s", (thread_id,))

        # Remove da sessão em memória, se existir
        sessions.pop(thread_id, None)
        pending_threads.pop(thread_id, None)

        return {"status": "deleted", "thread_id": thread_id}
    except Exception as e:
        print(f"[delete_thread] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao excluir thread.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


# ---------------------------------------------------------------------------
# Admin - TTS (Text to Speech) para notificações do auditor
# ---------------------------------------------------------------------------

class TTSRequest(BaseModel):
    text: str

@app.post("/admin/tts")
async def admin_tts(request: TTSRequest, authorization: str = Header(None)):
    """Gera áudio TTS usando OpenAI gpt-4o-mini-tts (voz coral, pt-BR). Cache em disco."""
    verify_api_key(authorization)

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Texto vazio")

    # Cache key: hash MD5 do texto
    cache_key = hashlib.md5(text.encode("utf-8")).hexdigest()
    cache_path = TTS_CACHE_DIR / f"{cache_key}.mp3"

    if cache_path.exists():
        print(f"[admin/tts] Cache hit: {cache_key}")
        audio_bytes = cache_path.read_bytes()
        return StreamingResponse(
            iter([audio_bytes]),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline; filename=notification.mp3"},
        )

    try:
        response = await openai_client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="coral",
            input=text,
            instructions="Fale em português do Brasil, com tom profissional e claro, como uma notificação de sistema.",
            response_format="mp3",
            speed=1.5,
        )

        # response is an HttpxBinaryResponseContent — read all bytes
        audio_bytes = response.content

        # Salvar no cache
        try:
            cache_path.write_bytes(audio_bytes)
            print(f"[admin/tts] Cache saved: {cache_key}")
        except Exception as e:
            print(f"[admin/tts] Erro ao salvar cache: {e}")

        return StreamingResponse(
            iter([audio_bytes]),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline; filename=notification.mp3"},
        )
    except Exception as e:
        print(f"[admin/tts] Erro: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao gerar áudio TTS: {e}")


# ---------------------------------------------------------------------------
# Admin - SSE Global Events (notificações em tempo real para dashboards)
# ---------------------------------------------------------------------------

@app.get("/admin/events")
async def admin_events_sse(request: Request, authorization: str = Header(None), token: str = Query(None)):
    """SSE stream global para dashboards. Emite new_chat quando chega nova conversa."""
    auth = authorization or (f"Bearer {token}" if token else None)
    verify_api_key(auth)

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    admin_broadcast_queues.append(queue)

    def _sse(event: str, data: dict) -> str:
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event}\ndata: {payload}\n\n"

    async def event_stream():
        try:
            yield _sse("connected", {"status": "ok"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield _sse(event.get("type", "message"), event)
                except asyncio.TimeoutError:
                    yield _sse("ping", {"ts": datetime.utcnow().isoformat()})
        finally:
            if queue in admin_broadcast_queues:
                admin_broadcast_queues.remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Admin - Auditoria de conversas (todas as threads de todos os usuários)
# ---------------------------------------------------------------------------

@app.get("/admin/threads")
async def admin_list_threads(
    page: int = 1,
    limit: int = 20,
    search: str = "",
    auditor_only: bool = False,
    authorization: str = Header(None),
):
    """
    Retorna lista paginada de TODAS as threads (admin audit).
    Suporta busca por subject, email ou thread_id.
    """
    verify_api_key(authorization)
    offset = (page - 1) * limit

    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                search_clause = ""
                params: list = []

                if search.strip():
                    search_clause = """
                        AND (
                            t.subject ILIKE %s
                            OR u.email ILIKE %s
                            OR u.name ILIKE %s
                            OR t.id::text ILIKE %s
                        )
                    """
                    like = f"%{search.strip()}%"
                    params = [like, like, like, like]

                auditor_clause = ""
                if auditor_only:
                    auditor_clause = """
                        AND EXISTS (
                            SELECT 1 FROM chat_thread ct2
                            JOIN chat c2 ON ct2.chat_id = c2.id
                            WHERE ct2.thread_id = t.id AND c2.origem = 'auditor'
                        )
                    """

                # Main query - threads with aggregated info
                query = f"""
                    SELECT t.id           AS thread_id,
                           t.subject,
                           a.name         AS agent_name,
                           a.title        AS agent_title,
                           u.name         AS user_name,
                           u.email        AS user_email,
                           MIN(c.created_at) AS created_at,
                           COUNT(DISTINCT c.id) FILTER (WHERE c.message NOT LIKE 'Thread iniciada:%%') AS message_count,
                           MAX(ct.feedback_rating) AS feedback_rating,
                           COUNT(c.id) FILTER (WHERE c.feedback_thumb = 1) AS thumb_up_count,
                           COUNT(c.id) FILTER (WHERE c.feedback_thumb = -1) AS thumb_down_count,
                           EXISTS (
                               SELECT 1 FROM chat_thread ct2
                               JOIN chat c2 ON ct2.chat_id = c2.id
                               WHERE ct2.thread_id = t.id AND c2.origem = 'auditor'
                           ) AS has_auditor,
                           (
                               SELECT COALESCE(aud.nickname, aud.name)
                               FROM chat c3
                               JOIN chat_thread ct3 ON ct3.chat_id = c3.id
                               JOIN auditor aud ON aud.id = c3.auditor_id
                               WHERE ct3.thread_id = t.id AND c3.origem = 'auditor'
                               ORDER BY c3.created_at DESC
                               LIMIT 1
                           ) AS auditor_nickname
                    FROM thread t
                    JOIN chat_thread ct ON ct.thread_id = t.id
                    JOIN chat c         ON ct.chat_id   = c.id
                    JOIN agent a        ON c.agent_id   = a.id
                    JOIN "user" u       ON c.user_id    = u.id
                    WHERE 1=1
                    {search_clause}
                    {auditor_clause}
                    GROUP BY t.id, t.subject, a.name, a.title, u.name, u.email
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s;
                """
                cur.execute(query, params + [limit, offset])
                threads = cur.fetchall()

                # Total count for pagination
                count_query = f"""
                    SELECT COUNT(DISTINCT t.id) AS total
                    FROM thread t
                    JOIN chat_thread ct ON ct.thread_id = t.id
                    JOIN chat c         ON ct.chat_id   = c.id
                    JOIN agent a        ON c.agent_id   = a.id
                    JOIN "user" u       ON c.user_id    = u.id
                    WHERE 1=1
                    {search_clause}
                    {auditor_clause};
                """
                cur.execute(count_query, params)
                total = cur.fetchone()["total"]

        return {
            "threads": threads,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if limit else 1,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[admin/threads] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar threads para auditoria.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


# ---------------------------------------------------------------------------
# Presença e mensagens do auditor (SSE real-time)
# ---------------------------------------------------------------------------

import time as _time


def _is_user_online(thread_id: str) -> bool:
    """Verifica se o usuário de uma thread está online (heartbeat recente)."""
    last_hb = user_presence.get(thread_id)
    if last_hb is None:
        return False
    return (_time.time() - last_hb) < PRESENCE_TIMEOUT


def _notify_auditor_new_message(thread_id: str, chat_id: int, message: str, origem: str):
    """Notifica auditores conectados sobre nova mensagem na thread."""
    event = {
        "type": "new_message",
        "thread_id": thread_id,
        "chat_id": chat_id,
        "message": message,
        "role": "user" if origem == "usuario" else "assistant" if origem == "agente" else "auditor",
    }
    for q in auditor_queues.get(thread_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    # Notifica também o painel global
    _broadcast_thread_update(thread_id)


@app.put("/thread/{thread_id}/heartbeat")
async def user_heartbeat(thread_id: str):
    """
    Chamado periodicamente pelo chat do usuário para sinalizar que está online.
    Notifica auditores conectados sobre mudança de presença.
    """
    was_online = _is_user_online(thread_id)
    user_presence[thread_id] = _time.time()

    # Se mudou de offline → online, notificar auditores
    if not was_online:
        event = {"type": "presence", "online": True, "thread_id": thread_id}
        for q in auditor_queues.get(thread_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    return {"status": "ok"}


@app.get("/thread/{thread_id}/presence")
async def thread_presence_sse(thread_id: str, request: Request):
    """
    SSE stream para o auditor monitorar presença do usuário em tempo real.
    Emite eventos: presence (online/offline), auditor_message_sent (confirmação).
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    auditor_queues[thread_id].append(queue)

    def _sse(event: str, data: dict) -> str:
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event}\ndata: {payload}\n\n"

    async def event_stream():
        try:
            # Enviar estado inicial de presença
            yield _sse("presence", {
                "online": _is_user_online(thread_id),
                "thread_id": thread_id
            })

            while True:
                # Verificar se o request foi desconectado
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield _sse(event["type"], event)
                except asyncio.TimeoutError:
                    # Enviar ping periódico + atualização de presença
                    yield _sse("presence", {
                        "online": _is_user_online(thread_id),
                        "thread_id": thread_id
                    })
        finally:
            # Limpar a queue do auditor quando desconectar
            if queue in auditor_queues.get(thread_id, []):
                auditor_queues[thread_id].remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/thread/{thread_id}/user-events")
async def thread_user_events_sse(thread_id: str, request: Request):
    """
    SSE stream para o chat do USUÁRIO receber mensagens do auditor em tempo real.
    O chat do usuário se conecta aqui quando abre a conversa.
    Emite eventos: auditor_message (mensagem do humano auditor).
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    user_queues[thread_id].append(queue)

    # Registrar presença automaticamente quando o SSE conecta
    user_presence[thread_id] = _time.time()
    # Notificar auditores que o usuário ficou online
    for q in auditor_queues.get(thread_id, []):
        try:
            q.put_nowait({"type": "presence", "online": True, "thread_id": thread_id})
        except asyncio.QueueFull:
            pass

    def _sse(event: str, data: dict) -> str:
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event}\ndata: {payload}\n\n"

    async def event_stream():
        try:
            yield _sse("connected", {"thread_id": thread_id})

            while True:
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield _sse(event["type"], event)
                except asyncio.TimeoutError:
                    # Ping para manter conexão viva + atualizar presença
                    user_presence[thread_id] = _time.time()
                    yield _sse("ping", {"ts": _time.time()})
        finally:
            # Limpar a queue do usuário
            if queue in user_queues.get(thread_id, []):
                user_queues[thread_id].remove(queue)
            # Marcar como offline e notificar auditores
            user_presence.pop(thread_id, None)
            for q in auditor_queues.get(thread_id, []):
                try:
                    q.put_nowait({"type": "presence", "online": False, "thread_id": thread_id})
                except asyncio.QueueFull:
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/thread/{thread_id}/auditor-message")
async def send_auditor_message(
    thread_id: str,
    request: AuditorMessageRequest,
    authorization: str = Header(None),
):
    """
    Permite ao auditor injetar uma mensagem em uma thread.
    A mensagem é:
    1. Persistida no banco (origem='auditor')
    2. Enviada via SSE para o chat do usuário em tempo real
    """
    verify_api_key(authorization)

    message_text = request.message.strip()
    if not message_text:
        raise HTTPException(status_code=400, detail="Mensagem vazia")

    chat_id = None
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                # Buscar user_id e agent_id da thread
                cur.execute("""
                    SELECT c.user_id, c.agent_id FROM chat c
                    JOIN chat_thread ct ON ct.chat_id = c.id
                    WHERE ct.thread_id = %s
                    LIMIT 1;
                """, (thread_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Thread não encontrada")

                uid, aid = row

                # Persistir mensagem do auditor (com auditor_id se fornecido)
                auditor_id = request.auditor_id
                cur.execute("""
                    INSERT INTO chat (user_id, agent_id, message, origem, auditor_id)
                    VALUES (%s, %s, %s, 'auditor', %s)
                    RETURNING id;
                """, (uid, aid, message_text, auditor_id))
                chat_id = cur.fetchone()[0]

                # Vincular à thread
                cur.execute("""
                    INSERT INTO chat_thread (thread_id, chat_id)
                    VALUES (%s, %s);
                """, (thread_id, chat_id))
    except HTTPException:
        raise
    except Exception as e:
        print(f"[auditor-message] Erro ao persistir: {e}")
        raise HTTPException(status_code=500, detail="Erro ao salvar mensagem")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    # Enviar via SSE para o chat do usuário
    # Buscar dados do auditor para incluir no evento SSE
    auditor_info = {}
    if request.auditor_id:
        try:
            conn2 = get_db_connection()
            with conn2:
                with conn2.cursor(cursor_factory=RealDictCursor) as cur2:
                    cur2.execute(
                        "SELECT id, login, name, nickname, icon_svg FROM auditor WHERE id = %s",
                        (request.auditor_id,)
                    )
                    aud_row = cur2.fetchone()
                    if aud_row:
                        auditor_info = {
                            "auditor_id": aud_row["id"],
                            "auditor_name": aud_row["name"] or aud_row["login"],
                            "auditor_nickname": aud_row["nickname"] or aud_row["name"] or aud_row["login"],
                            "auditor_icon_svg": aud_row["icon_svg"],
                        }
        except Exception as e:
            print(f"[auditor-message] Erro ao buscar dados do auditor: {e}")
        finally:
            if 'conn2' in locals() and conn2:
                conn2.close()

    event = {
        "type": "auditor_message",
        "chat_id": chat_id,
        "message": message_text,
        "thread_id": thread_id,
        **auditor_info,
    }
    sent_count = 0
    for q in user_queues.get(thread_id, []):
        try:
            q.put_nowait(event)
            sent_count += 1
        except asyncio.QueueFull:
            pass

    # Confirmar para o auditor
    for q in auditor_queues.get(thread_id, []):
        try:
            q.put_nowait({
                "type": "auditor_message_sent",
                "chat_id": chat_id,
                "message": message_text,
                "delivered": sent_count > 0,
            })
        except asyncio.QueueFull:
            pass

    # Notifica o painel global
    _broadcast_thread_update(thread_id)

    # --- INJETAR NO CONTEXTO DA CONVERSA ---
    # A mensagem do auditor é adicionada ao histórico em memória como uma
    # instrução do tipo 'system' com prefixo especial. Isso faz com que o
    # query_rewrite e o build_messages considerem a correção do auditor
    # como fonte de verdade nas próximas interações da conversa.
    if thread_id in sessions:
        sessions[thread_id].append({
            "role": "system",
            "content": f"[CORREÇÃO DO SUPORTE HUMANO]: {message_text}"
        })
        print(f"[auditor] Correção injetada no contexto da thread {thread_id}: {message_text[:80]}...")

    return {
        "status": "ok",
        "chat_id": chat_id,
        "delivered_to_user": sent_count > 0,
        "user_online": _is_user_online(thread_id),
    }


@app.get("/thread/{thread_id}/status")
async def thread_status(thread_id: str, authorization: str = Header(None)):
    """Retorna o status de presença do usuário em uma thread (polling simples)."""
    verify_api_key(authorization)
    return {
        "thread_id": thread_id,
        "user_online": _is_user_online(thread_id),
        "last_heartbeat": user_presence.get(thread_id),
    }


# ---------------------------------------------------------------------------
# FAQ - Gerar FAQ a partir de chats auditados e adicionar ao NotebookLM
# ---------------------------------------------------------------------------

FAQ_GENERATION_PROMPT = """
Você é um especialista em criar FAQs para suporte técnico.

Sua tarefa é analisar a conversa abaixo entre um USUÁRIO, um ASSISTENTE (IA) e um AUDITOR (suporte humano que corrigiu a IA).

REGRAS CRÍTICAS:
1. Gere FAQ APENAS com base nas CORREÇÕES DO AUDITOR. O auditor é a ÚNICA fonte de verdade.
2. Se o AUDITOR corrigiu a IA, a RESPOSTA da FAQ deve ser baseada EXCLUSIVAMENTE no que o AUDITOR disse, IGNORANDO a resposta original da IA.
3. A PERGUNTA deve ser a dúvida original do usuário, reescrita de forma clara e genérica.
4. NÃO gere FAQ para tópicos onde o auditor NÃO interveio. Se a IA respondeu sozinha sem correção do auditor, IGNORE esse trecho.
5. NÃO gere respostas genéricas como "consulte o suporte" ou "entre em contato". Se a única informação é essa, NÃO inclua na FAQ.
6. Respostas devem ser OBJETIVAS, DIRETAS e com conteúdo ÚTIL e ACIONÁVEL.
7. Se a conversa cobre múltiplos tópicos corrigidos pelo auditor, gere múltiplos pares Pergunta/Resposta.
8. NÃO inclua informações pessoais do usuário (nome, email, etc).
9. Se o auditor apenas confirmou a resposta da IA sem adicionar informação nova, use a resposta combinada.
10. NÃO gere perguntas META sobre o próprio suporte ou processos internos. Exemplos de perguntas IRRELEVANTES que devem ser IGNORADAS:
    - "A documentação está sendo atualizada?"
    - "O suporte vai melhorar?"
    - "A IA vai ser corrigida?"
    - "Vocês estão revisando a documentação?"
    Foque APENAS em perguntas técnicas sobre o PRODUTO/SISTEMA que ajudem outros usuários.
11. EVITAR DUPLICATAS: Se a [FAQ ATUAL] já tiver a MESMA correção/informação que o auditor deu (mesmo sentido/resposta analóga), NÃO gere um novo par. Simplesmente IGNORE. Só gere a FAQ se for algo novo, ou uma informação complementar não coberta.

FORMATO OBRIGATÓRIO (Resposta SEMPRE em nova linha, 2 linhas em branco entre pares):

Pergunta: [pergunta clara e genérica]
Resposta: [resposta baseada EXCLUSIVAMENTE na correção do auditor]


Pergunta: [outra pergunta, se aplicável]
Resposta: [resposta correspondente]
"""


async def _generate_faq_from_messages(messages_data: list[dict], existing_faq: str = "") -> str:
    """
    Usa OpenAI para transformar mensagens de um chat em pares FAQ.
    Prioriza correções do auditor sobre respostas da IA.
    Evita duplicar informações caso existing_faq seja passado.
    """
    # Formatar conversa para o prompt
    conversation_lines = []
    for m in messages_data:
        if m["role"] == "user":
            conversation_lines.append(f"USUÁRIO: {m['content']}")
        elif m["role"] == "assistant":
            conversation_lines.append(f"ASSISTENTE (IA): {m['content']}")
        elif m["role"] == "auditor":
            conversation_lines.append(f"AUDITOR (CORREÇÃO HUMANA): {m['content']}")

    conversation_text = "\n\n".join(conversation_lines)
    
    user_content = ""
    if existing_faq.strip():
        user_content += f"[FAQ ATUAL]\n{existing_faq}\n\n"
        
    user_content += f"[CONVERSA]\n{conversation_text}\n\n"
    user_content += f"Gere os pares Pergunta/Resposta para a FAQ se não for duplicado:"

    try:
        result = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=4096,
            temperature=0.2,
            timeout=120.0,
            messages=[
                {"role": "system", "content": FAQ_GENERATION_PROMPT},
                {
                    "role": "user",
                    "content": user_content
                }
            ]
        )
        return result.choices[0].message.content.strip()
    except Exception as e:
        print(f"[faq] Erro ao gerar FAQ com OpenAI: {e}")
        raise


def _build_faq_source_title(agent_title: str) -> str:
    """Gera o título padronizado da source FAQ: FAQ_NOME_DO_NOTEBOOK
    Ex: agent_title='Sistema Control' -> 'FAQ_SISTEMA_CONTROL'
    """
    import re
    # Remove caracteres especiais, substitui espaços por underscore, uppercase
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', agent_title)
    clean = re.sub(r'\s+', '_', clean.strip()).upper()
    return f"FAQ_{clean}"


async def _delete_faq_sources(notebook_id: str, faq_title: str, profile: str = "default"):
    """
    Deleta TODAS as sources cujo título contenha 'FAQ' ou 'faq' no notebook.
    Isso garante limpeza de sources duplicados criados anteriormente.
    """
    try:
        cmd = _get_notebooklm_cmd(profile, "source", "list", "-n", notebook_id, "--json")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return

        sources = json.loads(stdout.decode())
        if isinstance(sources, dict):
            sources = sources.get("sources", [])

        for s in sources:
            s_title = s.get("title", "")
            source_id = s.get("id", "")
            # Deletar qualquer source que tenha FAQ no título OU que seja o arquivo antigo faq_*.txt
            if source_id and ("faq" in s_title.lower() or s_title == faq_title):
                try:
                    cmd_del = _get_notebooklm_cmd(profile, "source", "delete", source_id, "-n", notebook_id, "-y")
                    proc_del = await asyncio.create_subprocess_exec(
                        *cmd_del,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(proc_del.communicate(), timeout=30)
                    print(f"[faq] Source deletada: '{s_title}' ({source_id[:8]}...)")
                except Exception as e:
                    print(f"[faq] Erro ao deletar source {source_id[:8]}: {e}")
    except Exception as e:
        print(f"[faq] Erro ao listar/deletar sources: {e}")


async def _add_faq_source_to_notebook(notebook_id: str, faq_title: str, full_faq_content: str, profile: str = "default") -> dict:
    """
    Deleta todas as sources FAQ antigas e adiciona UMA ÚNICA source com o conteúdo
    completo da FAQ acumulada. O arquivo temporário recebe o nome do título para
    que o NotebookLM mostre o nome correto.
    """
    import tempfile
    from pathlib import Path as _Path

    # 1. Deletar qualquer source FAQ existente
    await _delete_faq_sources(notebook_id, faq_title, profile=profile)

    # 2. Criar arquivo temporário com o NOME CORRETO (NotebookLM usa o filename como título)
    tmp_dir = _Path(tempfile.gettempdir()) / "notebooklm_faq"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    faq_file = tmp_dir / f"{faq_title}.md"
    faq_file.write_text(full_faq_content, encoding="utf-8")

    # 3. Adicionar source
    try:
        cmd = _get_notebooklm_cmd(profile, "source", "add", str(faq_file), "-n", notebook_id, "--json")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode == 0:
            result = json.loads(stdout.decode()) if stdout else {}
            print(f"[faq] Source '{faq_title}' adicionado ao notebook {notebook_id} (profile={profile})")
            return {"success": True, "result": result}
        else:
            err = stderr.decode()[:500]
            print(f"[faq] Erro ao adicionar source: {err}")
            return {"success": False, "error": err}
    except asyncio.TimeoutError:
        return {"success": False, "error": "Timeout ao adicionar source"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        try:
            faq_file.unlink(missing_ok=True)
        except:
            pass


@app.post("/thread/{thread_id}/add-to-faq")
async def add_thread_to_faq(
    thread_id: str,
    request: Optional[FAQRequest] = None,
    authorization: str = Header(None),
):
    """
    Gera FAQ a partir das mensagens de uma thread (priorizando correções do auditor)
    e adiciona como source de texto no NotebookLM do agente correspondente.
    O conteúdo FAQ é acumulado na coluna agent.faq_content e a source inteira
    é recriada no NotebookLM a cada adição.
    """
    verify_api_key(authorization)

    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Verificar se já foi adicionada à FAQ
                cur.execute("SELECT faq_added FROM thread WHERE id = %s;", (thread_id,))
                thread_row = cur.fetchone()
                if not thread_row:
                    raise HTTPException(status_code=404, detail="Thread não encontrada")
                if thread_row["faq_added"]:
                    raise HTTPException(status_code=409, detail="Esta thread já foi adicionada à FAQ")

                # Buscar agente da thread
                cur.execute("""
                    SELECT DISTINCT a.id AS agent_id, a.name AS agent_name, a.title AS agent_title,
                           COALESCE(a.faq_content, '') AS faq_content,
                           COALESCE(a.notebooklm_profile, 'default') AS notebooklm_profile
                    FROM chat c
                    JOIN chat_thread ct ON ct.chat_id = c.id
                    JOIN agent a ON c.agent_id = a.id
                    WHERE ct.thread_id = %s
                    LIMIT 1;
                """, (thread_id,))
                agent_row = cur.fetchone()
                if not agent_row:
                    raise HTTPException(status_code=404, detail="Agente não encontrado para esta thread")

                # Verificar se tem interação do auditor
                cur.execute("""
                    SELECT EXISTS(
                        SELECT 1 FROM chat c
                        JOIN chat_thread ct ON ct.chat_id = c.id
                        WHERE ct.thread_id = %s AND c.origem = 'auditor'
                    ) AS has_auditor;
                """, (thread_id,))
                has_auditor = cur.fetchone()["has_auditor"]
                if not has_auditor:
                    raise HTTPException(
                        status_code=400,
                        detail="Esta thread não possui interação do auditor. Apenas threads com correções do auditor podem ser adicionadas à FAQ."
                    )

                # Buscar todas as mensagens
                cur.execute("""
                    SELECT c.message, c.origem
                    FROM chat c
                    JOIN chat_thread ct ON ct.chat_id = c.id
                    WHERE ct.thread_id = %s
                    ORDER BY c.created_at ASC, c.id ASC;
                """, (thread_id,))
                raw_messages = cur.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        print(f"[add-to-faq] Erro ao buscar dados: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar dados da thread")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    # Formatar mensagens para o gerador de FAQ
    messages_for_faq = []
    for m in raw_messages:
        if m["message"].startswith("Thread iniciada:"):
            continue
        if m["origem"] == "usuario":
            role = "user"
        elif m["origem"] == "auditor":
            role = "auditor"
        else:
            role = "assistant"
        messages_for_faq.append({"role": role, "content": m["message"]})

    if not messages_for_faq:
        raise HTTPException(status_code=400, detail="Nenhuma mensagem encontrada na thread")

    # Acumular FAQ no banco de dados (agent.faq_content)
    existing_faq = agent_row["faq_content"] or ""

    # Limpar títulos antigos acumulados
    import re
    existing_clean = re.sub(r'#?\s*FAQ\s*-?\s*Perguntas\s*Frequentes\s*', '', existing_faq).strip()

    # Gerar FAQ usando OpenAI (se não for passado texto pronto pelo auditor)
    faq_text = request.faq_text if request and request.faq_text else None
    
    if not faq_text:
        try:
            faq_text = await _generate_faq_from_messages(messages_for_faq, existing_clean)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erro ao gerar FAQ: {e}")

    if not faq_text.strip():
        # Se retornou vazio (ex: era uma dúvida idêntica à que já tem), dizemos que não teve FAQ nova (ou retornamos sucesso com arquivo inalterado)
        print("[add-to-faq] FAQ ignorada pela IA (possível duplicata).")
        # Vamos apenas marcar como true a thread sem falhar.
        faq_text = ""

    if existing_clean:
        raw_full_faq = existing_clean + ("\n\n" + faq_text if faq_text else "")
    else:
        raw_full_faq = faq_text

    # --- Pós-processamento: garantir formatação correta ---
    # Passo 1: Separar "Resposta:" para nova linha quando colado com texto
    # Substitui qualquer "Resposta:" precedido por texto na mesma linha
    formatted = re.sub(r'(\S)\s+Resposta:', r'\1\nResposta:', raw_full_faq)

    # Passo 2: Processar linha a linha e limpar separadores antigos
    clean_lines = []
    for line in formatted.split('\n'):
        stripped = line.strip()
        if not stripped or stripped == "---":
            continue
        # Ignorar títulos residuais
        if re.match(r'^#?\s*FAQ', stripped, re.IGNORECASE):
            continue

        # Se a linha ainda contém AMBOS Pergunta: e Resposta:, dividir
        if 'Pergunta:' in stripped and 'Resposta:' in stripped:
            idx = stripped.index('Resposta:')
            pergunta_part = stripped[:idx].strip()
            resposta_part = stripped[idx:].strip()
            if clean_lines:
                clean_lines.append("")
                clean_lines.append("")
            clean_lines.append(pergunta_part)
            clean_lines.append(resposta_part)
        elif stripped.lower().startswith("pergunta:") or stripped.startswith("## "):
            if clean_lines:
                clean_lines.append("")
                clean_lines.append("")
            clean_lines.append(stripped)
        else:
            clean_lines.append(stripped)

    full_faq = "\n".join(clean_lines)

    # Salvar FAQ acumulada no banco
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent SET faq_content = %s WHERE id = %s;",
                    (full_faq, agent_row["agent_id"])
                )
                cur.execute(
                    "UPDATE thread SET faq_added = TRUE WHERE id = %s;",
                    (thread_id,)
                )
    except Exception as e:
        print(f"[add-to-faq] Erro ao salvar FAQ no banco: {e}")
        raise HTTPException(status_code=500, detail="Erro ao salvar FAQ")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    # Recriar source no NotebookLM (deleta antigas + cria única nova)
    agent_notebook_id = agent_row["agent_id"]
    agent_profile = agent_row.get("notebooklm_profile", "default")
    faq_title = _build_faq_source_title(agent_row["agent_title"])

    add_result = await _add_faq_source_to_notebook(agent_notebook_id, faq_title, full_faq, profile=agent_profile)

    if not add_result["success"]:
        print(f"[add-to-faq] Falha ao adicionar source ao NotebookLM: {add_result.get('error')}")

    return {
        "status": "ok",
        "thread_id": thread_id,
        "agent_name": agent_row["agent_name"],
        "agent_title": agent_row["agent_title"],
        "faq_source_title": faq_title,
        "faq_generated": faq_text,
        "notebook_result": add_result,
    }


@app.get("/thread/{thread_id}/faq-status")
async def get_faq_status(
    thread_id: str,
    authorization: str = Header(None),
):
    """Retorna se uma thread já foi adicionada à FAQ e se tem interação do auditor."""
    verify_api_key(authorization)

    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT t.faq_added,
                           EXISTS(
                               SELECT 1 FROM chat_thread ct
                               JOIN chat c ON ct.chat_id = c.id
                               WHERE ct.thread_id = t.id AND c.origem = 'auditor'
                           ) AS has_auditor
                    FROM thread t
                    WHERE t.id = %s;
                """, (thread_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Thread não encontrada")
        return {
            "thread_id": thread_id,
            "faq_added": row["faq_added"] or False,
            "has_auditor": row["has_auditor"],
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[faq-status] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar status")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


@app.post("/agent/{agent_id}/reset-faq")
async def reset_agent_faq(
    agent_id: str,
    authorization: str = Header(None),
):
    """Limpa a FAQ acumulada de um agente e reseta o flag faq_added em todas as threads."""
    verify_api_key(authorization)

    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                # Limpar faq_content do agente
                cur.execute("UPDATE agent SET faq_content = '' WHERE id = %s;", (agent_id,))
                # Resetar faq_added em todas as threads deste agente
                cur.execute("""
                    UPDATE thread SET faq_added = FALSE
                    WHERE id IN (
                        SELECT DISTINCT ct.thread_id
                        FROM chat_thread ct
                        JOIN chat c ON ct.chat_id = c.id
                        WHERE c.agent_id = %s
                    );
                """, (agent_id,))
        return {"status": "ok", "agent_id": agent_id, "message": "FAQ resetada com sucesso"}
    except Exception as e:
        print(f"[reset-faq] Erro: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao resetar FAQ: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


# ---------------------------------------------------------------------------
# Limpeza de sessões ociosas (> 2h)
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    # Garante que a tabela agent existe no banco
    try:
        ensure_tables()
    except Exception as e:
        print(f"[startup] Aviso: não foi possível verificar as tabelas: {e}")
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_tts_cache_cleanup_loop())

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

async def _tts_cache_cleanup_loop():
    """Remove arquivos de cache TTS mais antigos que 24 horas."""
    import time as _t
    while True:
        await asyncio.sleep(3600)  # Verifica a cada hora
        try:
            now = _t.time()
            removed = 0
            for f in TTS_CACHE_DIR.iterdir():
                if f.is_file() and f.suffix == ".mp3":
                    age = now - f.stat().st_mtime
                    if age > 86400:  # 24 horas
                        f.unlink()
                        removed += 1
            if removed:
                print(f"[tts_cache_cleanup] {removed} arquivos removidos")
        except Exception as e:
            print(f"[tts_cache_cleanup] Erro: {e}")