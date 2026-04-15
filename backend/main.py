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
from collections import defaultdict
from datetime import datetime
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from dotenv import load_dotenv  # Carregar variáveis do .env
load_dotenv()

from openai import AsyncOpenAI
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Request
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
    """Cria as tabelas agent, user, chat, thread e chat_thread se não existirem."""
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS agent (
                    id           VARCHAR(36) PRIMARY KEY,
                    title        TEXT        NOT NULL,
                    name         TEXT,
                    system_prompt TEXT,
                    email        TEXT,
                    overview     TEXT,
                    sort_order   INTEGER     DEFAULT 0,
                    active       BOOLEAN     DEFAULT TRUE,
                    creation     TIMESTAMP   NOT NULL DEFAULT NOW(),
                    modification TIMESTAMP   NOT NULL DEFAULT NOW()
                );
                """)
                cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'agent' AND column_name = 'sort_order'
                    ) THEN
                        ALTER TABLE agent ADD COLUMN sort_order INTEGER DEFAULT 0;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'agent' AND column_name = 'active'
                    ) THEN
                        ALTER TABLE agent ADD COLUMN active BOOLEAN DEFAULT TRUE;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'agent' AND column_name = 'faq_content'
                    ) THEN
                        ALTER TABLE agent ADD COLUMN faq_content TEXT DEFAULT '';
                    END IF;
                END $$;
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS "user" (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    name VARCHAR(255),
                    cnpj VARCHAR(255)
                );
                """)
                # Renomear tabela antiga se existir
                cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'chats') THEN
                        ALTER TABLE chats RENAME TO chat;
                    END IF;
                END $$;
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS chat (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES "user"(id) ON DELETE CASCADE,
                    agent_id VARCHAR(36) REFERENCES agent(id) ON DELETE CASCADE,
                    message TEXT,
                    origem VARCHAR(10) DEFAULT 'sistema',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """)
                # Adicionar coluna created_at e origem caso tabela já exista sem ela
                cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'chat' AND column_name = 'created_at'
                    ) THEN
                        ALTER TABLE chat ADD COLUMN created_at TIMESTAMP NOT NULL DEFAULT NOW();
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'chat' AND column_name = 'origem'
                    ) THEN
                        ALTER TABLE chat ADD COLUMN origem VARCHAR(10) DEFAULT 'sistema';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'chat' AND column_name = 'feedback_thumb'
                    ) THEN
                        ALTER TABLE chat ADD COLUMN feedback_thumb INT;
                        ALTER TABLE chat ADD COLUMN feedback_text TEXT;
                    END IF;
                    -- Remover feedback_rating de chat se existir (migrou para chat_thread)
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'chat' AND column_name = 'feedback_rating'
                    ) THEN
                        ALTER TABLE chat DROP COLUMN feedback_rating;
                    END IF;
                END $$;
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS thread (
                    id VARCHAR(36) PRIMARY KEY,
                    subject TEXT,
                    faq_added BOOLEAN DEFAULT FALSE
                );
                """)
                # Adicionar coluna faq_added a thread caso já exista sem ela
                cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'thread' AND column_name = 'faq_added'
                    ) THEN
                        ALTER TABLE thread ADD COLUMN faq_added BOOLEAN DEFAULT FALSE;
                    END IF;
                END $$;
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_thread (
                    id SERIAL PRIMARY KEY,
                    thread_id VARCHAR(36) REFERENCES thread(id) ON DELETE CASCADE,
                    chat_id INT REFERENCES chat(id) ON DELETE CASCADE,
                    feedback_rating INT CHECK (feedback_rating >= 1 AND feedback_rating <= 5)
                );
                """)
                # Adicionar coluna feedback_rating a chat_thread caso já exista sem ela
                cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'chat_thread' AND column_name = 'feedback_rating'
                    ) THEN
                        ALTER TABLE chat_thread ADD COLUMN feedback_rating INT CHECK (feedback_rating >= 1 AND feedback_rating <= 5);
                    END IF;
                END $$;
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    login VARCHAR(255) UNIQUE NOT NULL,
                    senha VARCHAR(255) NOT NULL
                );
                """)
        print("[DB] Tabelas agent, user, users, chat, thread e chat_thread verificadas/criadas com sucesso.")
    except Exception as e:
        print("[DB] Erro ao criar tabelas:", e)
    finally:
        conn.close()


def get_agent_info_by_name(name: str):
    """Busca id (notebook_id) e system_prompt pelo nome do agente."""
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, system_prompt FROM agent WHERE name = %s LIMIT 1", (name,))
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

# Presença: mapeia thread_id -> timestamp do último heartbeat do usuário
user_presence: dict[str, float] = {}

# SSE queues: mapeia thread_id -> lista de asyncio.Queue (cada auditor conectado recebe uma)
# Usado para notificar auditores sobre presença e enviar eventos para o chat do usuário
auditor_queues: dict[str, list[asyncio.Queue]] = defaultdict(list)
user_queues: dict[str, list[asyncio.Queue]] = defaultdict(list)

PRESENCE_TIMEOUT = 30  # segundos sem heartbeat = offline

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

class LoginRequest(BaseModel):
    login: str
    senha: str

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_api_key(authorization: str = Header(None)):
    if not authorization or authorization != f"Bearer {BACKEND_API_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


async def query_notebooklm(user_message: str, notebook_id: str, max_retries: int = 3) -> str:
    if not notebook_id:
        return ""

    cmd = [
        "notebooklm", "ask",
        user_message,
        "-n", notebook_id,
        "--json",
    ]

    for attempt in range(1, max_retries + 1):
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
            print(f"[notebooklm] tentativa {attempt}/{max_retries}: timeout ({NOTEBOOKLM_TIMEOUT}s)")
            if attempt < max_retries:
                await asyncio.sleep(2)
        except Exception as e:
            print(f"[notebooklm] tentativa {attempt}/{max_retries}: erro: {e}")
            if attempt < max_retries:
                await asyncio.sleep(2)

    print(f"[notebooklm] FALHOU após {max_retries} tentativas")
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
            f"Com base no contexto acima (e nas correções do suporte humano, se houver), responda a seguinte pergunta do cliente:\n"
            f"{user_message}"
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
                cur.execute("SELECT id, login FROM users WHERE login = %s AND senha = %s", (request.login, request.senha))
                user = cur.fetchone()
                if user:
                    return {"status": "ok", "user": {"id": user["id"], "login": user["login"]}}
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

    # Registra no banco: user, thread, chat e chat_thread
    try:
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                # Upsert user
                cur.execute("""
                    INSERT INTO "user" (email, name, cnpj)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (email) DO UPDATE 
                    SET name = EXCLUDED.name, cnpj = EXCLUDED.cnpj
                    RETURNING id;
                """, (email, name, cnpj))
                user_id = cur.fetchone()[0]

                # Localizar agent_id e title pelo agentName
                cur.execute("SELECT id, title FROM agent WHERE name = %s;", (agentName,))
                agent_row = cur.fetchone()

                agent_id = None
                subject_title = "indefinido"
                if agent_row:
                    agent_id = agent_row[0]
                    subject_title = f"Nova conversa com {agent_row[1]}"

                # Criar thread
                cur.execute("""
                    INSERT INTO thread (id, subject)
                    VALUES (%s, %s);
                """, (thread_id, subject_title))

                if agent_id:
                    # Inserir chat
                    cur.execute("""
                        INSERT INTO chat (user_id, agent_id, message)
                        VALUES (%s, %s, %s)
                        RETURNING id;
                    """, (user_id, agent_id, f"Thread iniciada: {thread_id}"))
                    chat_id = cur.fetchone()[0]

                    # Vincular chat à thread
                    cur.execute("""
                        INSERT INTO chat_thread (thread_id, chat_id)
                        VALUES (%s, %s);
                    """, (thread_id, chat_id))

    except Exception as e:
        print("Erro ao registrar user/chat/thread no banco:", e)
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    return {"threadId": thread_id, "userId": user_id if 'user_id' in locals() else None}


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
                cur.execute("SELECT id, name, title, sort_order FROM agent WHERE active = TRUE ORDER BY sort_order ASC;")
                rows = cur.fetchall()
                # Removemos datetime objectos caso existissem, mas select é só id, name, title
                return {"agents": rows}
    finally:
        conn.close()


@app.get("/updateNotebooks")
async def update_notebooks():
    """
    1. Executa `notebooklm list --json` para obter todos os notebooks.
    2. Para cada notebook, faz INSERT ... ON CONFLICT (id) DO UPDATE na tabela agent.
    3. Notebooks que não estão mais na lista têm active=false (nunca são deletados).
    4. Retorna resumo: notebooks encontrados, inseridos, atualizados e desativados.
    """
    # --- 1. Chamar CLI ---
    try:
        proc = await asyncio.create_subprocess_exec(
            "notebooklm", "list", "--json",
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
        INSERT INTO agent (id, title, name, creation, modification, active)
        VALUES (%(id)s, %(title)s, %(name)s, %(creation)s, %(modification)s, FALSE)
        ON CONFLICT (id) DO UPDATE
            SET title        = EXCLUDED.title,
                modification = EXCLUDED.modification
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
                    # Marca como inativo os agentes que não vieram na lista atual
                    cur.execute(
                        "UPDATE agent SET active = FALSE, modification = %s WHERE id != ALL(%s) AND active = TRUE RETURNING id;",
                        (now, valid_ids)
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


@app.post("/chat")
async def chat(request: ChatRequest, authorization: str = Header(None)):
    verify_api_key(authorization)

    thread_id    = request.threadId
    user_message = request.message.strip()
    assistant_name = request.assistantName or "SMART"

    if not user_message:
        raise HTTPException(status_code=400, detail="Mensagem vazia")

    # Busca o agente no banco de dados para pegar notebook ID e system prompt.
    agent_info = get_agent_info_by_name(assistant_name)
    if not agent_info:
        raise HTTPException(status_code=404, detail=f"Agent '{assistant_name}' não encontrado.")
    
    agent_notebook_id = agent_info["id"]
    agent_system_prompt = agent_info.get("system_prompt", "Você é um assistente útil.")

    if thread_id not in sessions:
        sessions[thread_id] = []
        
    is_first_message = len(sessions[thread_id]) == 0

    # Notifica o auditor assim que a mensagem chega (antes da IA pensar)
    _notify_auditor_new_message(thread_id, 0, user_message, 'usuario')

    # 1. Query Rewriting - expande perguntas vagas usando histórico da thread
    search_query = await rewrite_query_with_context(thread_id, user_message)

    print(f"\n{'='*50}")
    print(f"[DEBUG Chat] Thread: {thread_id}")
    print(f"[DEBUG Chat] Assistant: {assistant_name}")
    print(f"[DEBUG Chat] Original : {user_message!r}")
    print(f"[DEBUG Chat] Rewritten: {search_query!r}")

    # 2. NotebookLM - busca contexto nos manuais com a query expandida usando o ID dinâmico
    notebooklm_context = await query_notebooklm(search_query, agent_notebook_id)

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

    # 5b. Persiste no banco as duas mensagens desta interação
    assistant_chat_id = None
    try:
        conn_hist = get_db_connection()
        with conn_hist:
            with conn_hist.cursor() as cur:
                # Buscar user_id pelo thread -> chat_thread -> chat
                cur.execute("""
                    SELECT c.user_id, c.agent_id FROM chat c
                    JOIN chat_thread ct ON ct.chat_id = c.id
                    WHERE ct.thread_id = %s
                    LIMIT 1;
                """, (thread_id,))
                row = cur.fetchone()
                if row:
                    uid, aid = row
                    for msg_text, msg_origem in [(user_message, 'usuario'), (assistant_text, 'agente')]:
                        cur.execute("""
                            INSERT INTO chat (user_id, agent_id, message, origem)
                            VALUES (%s, %s, %s, %s)
                            RETURNING id;
                        """, (uid, aid, msg_text, msg_origem))
                        chat_id = cur.fetchone()[0]
                        if msg_origem == 'agente':
                            assistant_chat_id = chat_id
                        cur.execute("""
                            INSERT INTO chat_thread (thread_id, chat_id)
                            VALUES (%s, %s);
                        """, (thread_id, chat_id))
    except Exception as e:
        print(f"[chat] Erro ao persistir mensagens: {e}")
    finally:
        if 'conn_hist' in locals() and conn_hist:
            conn_hist.close()

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

    if thread_id not in sessions:
        sessions[thread_id] = []

    is_first_message = len(sessions[thread_id]) == 0

    # Notifica o auditor assim que a mensagem chega (antes da IA pensar)
    _notify_auditor_new_message(thread_id, 0, user_message, 'usuario')

    def _sse(event: str, data: dict) -> str:
        """Format a single SSE event."""
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event}\ndata: {payload}\n\n"

    async def event_generator():
        nonlocal is_first_message

        # --- Etapa 1: Query Rewriting ---
        yield _sse("status", {"stage": "rewriting", "detail": "Reescrevendo consulta..."})
        search_query = await rewrite_query_with_context(thread_id, user_message)

        print(f"\n{'='*50}")
        print(f"[STREAM] Thread: {thread_id}")
        print(f"[STREAM] Assistant: {assistant_name}")
        print(f"[STREAM] Original : {user_message!r}")
        print(f"[STREAM] Rewritten: {search_query!r}")

        # --- Etapa 2: NotebookLM RAG ---
        yield _sse("status", {"stage": "searching", "detail": "Buscando nos manuais..."})
        notebooklm_context = await query_notebooklm(search_query, agent_notebook_id)

        context_preview = notebooklm_context[:200].replace('\n', ' ') + "..." if notebooklm_context else "VAZIO"
        print(f"[STREAM] Contexto : {context_preview}")
        print(f"{'='*50}\n")

        has_notebooklm_context = bool(notebooklm_context and notebooklm_context.strip())

        # --- Etapa 3: OpenAI generation (with streaming) ---
        yield _sse("status", {"stage": "generating", "detail": "Gerando resposta com IA..."})

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
                    yield _sse("token", {"text": token_text})

            openai_ok = True

        except Exception as e:
            print(f"[STREAM openai] erro: {e}")
            # If OpenAI fails but we have NotebookLM context, use it as fallback
            if has_notebooklm_context:
                assistant_text = notebooklm_context
                yield _sse("fallback", {
                    "content": notebooklm_context,
                    "reason": "Não foi possível refinar a resposta com IA. Exibindo resposta direta dos manuais."
                })
            else:
                yield _sse("error", {"detail": "Erro ao gerar resposta. Tente novamente."})
                return

        # --- Etapa 4: Persistence ---
        yield _sse("status", {"stage": "saving", "detail": "Salvando..."})

        sessions[thread_id].append({"role": "user",      "content": user_message})
        sessions[thread_id].append({"role": "assistant", "content": assistant_text})

        assistant_chat_id = None
        try:
            conn_hist = get_db_connection()
            with conn_hist:
                with conn_hist.cursor() as cur:
                    cur.execute("""
                        SELECT c.user_id, c.agent_id FROM chat c
                        JOIN chat_thread ct ON ct.chat_id = c.id
                        WHERE ct.thread_id = %s
                        LIMIT 1;
                    """, (thread_id,))
                    row = cur.fetchone()
                    if row:
                        uid, aid = row
                        for msg_text, msg_origem in [(user_message, 'usuario'), (assistant_text, 'agente')]:
                            cur.execute("""
                                INSERT INTO chat (user_id, agent_id, message, origem)
                                VALUES (%s, %s, %s, %s)
                                RETURNING id;
                            """, (uid, aid, msg_text, msg_origem))
                            chat_id = cur.fetchone()[0]
                            if msg_origem == 'agente':
                                assistant_chat_id = chat_id
                            cur.execute("""
                                INSERT INTO chat_thread (thread_id, chat_id)
                                VALUES (%s, %s);
                            """, (thread_id, chat_id))
        except Exception as e:
            print(f"[STREAM] Erro ao persistir mensagens: {e}")
        finally:
            if 'conn_hist' in locals() and conn_hist:
                conn_hist.close()

        # Notificar auditores sobre a resposta da IA
        if assistant_chat_id:
            _notify_auditor_new_message(thread_id, assistant_chat_id, assistant_text, 'agente')

        if is_first_message:
            asyncio.create_task(generate_and_update_subject(thread_id, user_message, assistant_text))

        # --- Final done event ---
        yield _sse("done", {
            "chat_id": assistant_chat_id,
            "content": assistant_text,
            "was_fallback": not openai_ok,
        })

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
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao atualizar feedback de mensagem: {e}")
        raise HTTPException(status_code=500, detail="Erro ao atualizar feedback")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    return {"status": "ok", "chat_id": chat_id, "thumb": request.thumb}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions_ativas": len(sessions)}


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
):
    """
    Recebe o storage_state.json do NotebookLM via upload e salva no servidor.
    Após salvar, valida executando 'notebooklm list'.
    """

    from pathlib import Path as _Path
    import shutil

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

    # --- 3. Salvar no servidor (com backup do anterior) ---
    session_file = _Path.home() / ".notebooklm" / "storage_state.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)

    if session_file.exists():
        backup = session_file.with_suffix(".json.bak")
        shutil.copy2(session_file, backup)
        print(f"[uploadAuthState] backup salvo em {backup}")

    session_file.write_bytes(content)
    print(f"[uploadAuthState] storage_state.json atualizado ({len(content)} bytes, {cookies_count} cookies)")

    # --- 4. Validar executando notebooklm list ---
    valid = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "notebooklm", "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        valid = proc.returncode == 0
        if not valid:
            print(f"[uploadAuthState] validação falhou: {stderr.decode()[:200]}")
        else:
            print("[uploadAuthState] sessão validada com sucesso.")
    except asyncio.TimeoutError:
        print("[uploadAuthState] timeout na validação")
    except Exception as e:
        print(f"[uploadAuthState] erro na validação: {e}")

    return {
        "status": "ok",
        "saved": True,
        "valid": valid,
        "bytes": len(content),
        "cookies_count": cookies_count,
        "message": (
            "✅ Autenticação renovada e validada com sucesso!"
            if valid else
            "⚠️ Arquivo salvo, mas a validação falhou. O token pode estar expirado - tente gerar um novo no Mac."
        ),
    }



@app.get("/authStatus")
async def auth_status():
    """
    Verifica o status do storage_state.json do NotebookLM no servidor.
    Retorna se o arquivo existe, quantos cookies tem, quais expiram e quando.
    """
    from pathlib import Path as _Path

    session_file = _Path.home() / ".notebooklm" / "storage_state.json"

    if not session_file.exists():
        return {
            "exists": False,
            "valid": False,
            "cookies_count": 0,
            "expires_at": None,
            "file_age_hours": None,
            "message": "Arquivo storage_state.json não encontrado no servidor.",
        }

    try:
        import stat as _stat
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

        # Validar executando notebooklm list (rápido, timeout curto)
        valid_session = False
        try:
            proc = await asyncio.create_subprocess_exec(
                "notebooklm", "list",
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
            "exists": True,
            "valid": valid_session,
            "cookies_count": cookies_count,
            "expires_at": expires_at_iso,
            "file_age_hours": file_age_hours,
            "message": message,
        }
    except Exception as e:
        return {
            "exists": True,
            "valid": False,
            "cookies_count": 0,
            "expires_at": None,
            "file_age_hours": None,
            "message": f"Erro ao verificar o arquivo: {e}",
        }


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
                           sort_order, active, creation, modification
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
                           sort_order, active, creation, modification, faq_content
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
                              sort_order, active, creation, modification;
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
                    "SELECT id, title, faq_content FROM agent WHERE id = %s;",
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

    faq_title = _build_faq_source_title(row["title"])
    add_result = await _add_faq_source_to_notebook(agent_id, faq_title, faq_content)

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
                           ct.feedback_rating
                    FROM chat c
                    JOIN chat_thread ct ON ct.chat_id = c.id
                    WHERE ct.thread_id = %s
                    ORDER BY c.created_at ASC;
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
            formatted_messages.append({
                "id": m["id"],
                "role": role,
                "content": m["message"],
                "feedback_thumb": m["feedback_thumb"],
                "feedback_text": m["feedback_text"],
                "feedback_rating": m["feedback_rating"],
            })
            
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

        return {"status": "deleted", "thread_id": thread_id}
    except Exception as e:
        print(f"[delete_thread] Erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao excluir thread.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()


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
                           EXISTS (
                               SELECT 1 FROM chat_thread ct2
                               JOIN chat c2 ON ct2.chat_id = c2.id
                               WHERE ct2.thread_id = t.id AND c2.origem = 'auditor'
                           ) AS has_auditor
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

                # Persistir mensagem do auditor
                cur.execute("""
                    INSERT INTO chat (user_id, agent_id, message, origem)
                    VALUES (%s, %s, %s, 'auditor')
                    RETURNING id;
                """, (uid, aid, message_text))
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
    event = {
        "type": "auditor_message",
        "chat_id": chat_id,
        "message": message_text,
        "thread_id": thread_id,
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


async def _delete_faq_sources(notebook_id: str, faq_title: str):
    """
    Deleta TODAS as sources cujo título contenha 'FAQ' ou 'faq' no notebook.
    Isso garante limpeza de sources duplicados criados anteriormente.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "notebooklm", "source", "list",
            "-n", notebook_id,
            "--json",
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
                    proc_del = await asyncio.create_subprocess_exec(
                        "notebooklm", "source", "delete",
                        source_id,
                        "-n", notebook_id,
                        "-y",  # Skip confirmation
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(proc_del.communicate(), timeout=30)
                    print(f"[faq] Source deletada: '{s_title}' ({source_id[:8]}...)")
                except Exception as e:
                    print(f"[faq] Erro ao deletar source {source_id[:8]}: {e}")
    except Exception as e:
        print(f"[faq] Erro ao listar/deletar sources: {e}")


async def _add_faq_source_to_notebook(notebook_id: str, faq_title: str, full_faq_content: str) -> dict:
    """
    Deleta todas as sources FAQ antigas e adiciona UMA ÚNICA source com o conteúdo
    completo da FAQ acumulada. O arquivo temporário recebe o nome do título para
    que o NotebookLM mostre o nome correto.
    """
    import tempfile
    from pathlib import Path as _Path

    # 1. Deletar qualquer source FAQ existente
    await _delete_faq_sources(notebook_id, faq_title)

    # 2. Criar arquivo temporário com o NOME CORRETO (NotebookLM usa o filename como título)
    tmp_dir = _Path(tempfile.gettempdir()) / "notebooklm_faq"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    faq_file = tmp_dir / f"{faq_title}.md"
    faq_file.write_text(full_faq_content, encoding="utf-8")

    # 3. Adicionar source
    try:
        proc = await asyncio.create_subprocess_exec(
            "notebooklm", "source", "add",
            str(faq_file),
            "-n", notebook_id,
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode == 0:
            result = json.loads(stdout.decode()) if stdout else {}
            print(f"[faq] Source '{faq_title}' adicionado ao notebook {notebook_id}")
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
                           COALESCE(a.faq_content, '') AS faq_content
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
                    ORDER BY c.created_at ASC;
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

    # Gerar FAQ usando OpenAI (passando a FAQ existente para evitar duplicação)
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
        elif stripped.lower().startswith("pergunta:"):
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
    faq_title = _build_faq_source_title(agent_row["agent_title"])

    add_result = await _add_faq_source_to_notebook(agent_notebook_id, faq_title, full_faq)

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