"""
Backend FastAPI — substitui OpenAI Assistants API
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
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config — via variáveis de ambiente
# ---------------------------------------------------------------------------

OPENAI_API_KEY         = os.environ.get("OPENAI_API_KEY", "")
BACKEND_API_KEY        = os.environ.get("BACKEND_API_KEY", "")

HISTORY_LIMIT      = 10   # últimas N mensagens enviadas ao OpenAI (5 turnos)
NOTEBOOKLM_TIMEOUT = 60

# ---------------------------------------------------------------------------
# PostgreSQL — conexão e helpers
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
                    subject TEXT
                );
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
        print("[DB] Tabelas agent, user, chat, thread e chat_thread verificadas/criadas com sucesso.")
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


async def query_notebooklm(user_message: str, notebook_id: str) -> str:
    if not notebook_id:
        return ""

    cmd = [
        "notebooklm", "ask",
        user_message,
        "-n", notebook_id,
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
    3. Retorna resumo: notebooks encontrados, inseridos e atualizados.
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
        INSERT INTO agent (id, title, name, creation, modification)
        VALUES (%(id)s, %(title)s, %(name)s, %(creation)s, %(modification)s)
        ON CONFLICT (id) DO UPDATE
            SET title        = EXCLUDED.title,
                modification = EXCLUDED.modification
        RETURNING (xmax = 0) AS inserted;
    """

    inserted_count = 0
    updated_count  = 0
    deleted_count  = 0
    errors         = []
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
                
                # --- 4. Excluir removidos ---
                if valid_ids:
                    # Deletar todos onde o ID não está na lista de IDs válidos
                    cur.execute("DELETE FROM agent WHERE id != ALL(%s) RETURNING id;", (valid_ids,))
                    deleted_rows = cur.fetchall()
                    deleted_count = len(deleted_rows)
                else:
                    # Se vier vazio por algum motivo, não seria prudente apagar tudo cego, então podemos omitir
                    pass
    finally:
        conn.close()

    return {
        "status":      "ok",
        "total":       len(notebooks),
        "inseridos":   inserted_count,
        "atualizados": updated_count,
        "removidos":   deleted_count,
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

    # 1. Query Rewriting — expande perguntas vagas usando histórico da thread
    search_query = await rewrite_query_with_context(thread_id, user_message)

    print(f"\n{'='*50}")
    print(f"[DEBUG Chat] Thread: {thread_id}")
    print(f"[DEBUG Chat] Assistant: {assistant_name}")
    print(f"[DEBUG Chat] Original : {user_message!r}")
    print(f"[DEBUG Chat] Rewritten: {search_query!r}")

    # 2. NotebookLM — busca contexto nos manuais com a query expandida usando o ID dinâmico
    notebooklm_context = await query_notebooklm(search_query, agent_notebook_id)

    context_preview = notebooklm_context[:200].replace('\n', ' ') + "..." if notebooklm_context else "VAZIO"
    print(f"[DEBUG Chat] Contexto : {context_preview}")
    print(f"{'='*50}\n")

    # 3. Monta histórico + contexto injetado (usa mensagem original do usuário)
    messages = build_messages(thread_id, user_message, notebooklm_context)

    # 4. OpenAI gpt-4o-mini — formata resposta com base no contexto do NotebookLM
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=10240,
            messages=[{"role": "system", "content": agent_system_prompt or "Você é um assistente útil."}] + messages,
            timeout=60.0
        )
        assistant_text = response.choices[0].message.content

    except Exception as e:
        print(f"[openai] erro: {e}")
        raise HTTPException(status_code=500, detail="Erro ao gerar resposta")

    # 5. Histórico — salva mensagem original do usuário (não a query reescrita)
    sessions[thread_id].append({"role": "user",      "content": user_message})
    sessions[thread_id].append({"role": "assistant", "content": assistant_text})

    # 5b. Persiste no banco as duas mensagens desta interação
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
                        cur.execute("""
                            INSERT INTO chat_thread (thread_id, chat_id)
                            VALUES (%s, %s);
                        """, (thread_id, chat_id))
    except Exception as e:
        print(f"[chat] Erro ao persistir mensagens: {e}")
    finally:
        if 'conn_hist' in locals() and conn_hist:
            conn_hist.close()

    # 5c. Atualiza subject automaticamente caso seja a primeira mensagem
    if is_first_message:
        asyncio.create_task(generate_and_update_subject(thread_id, user_message, assistant_text))

    # 6. Retorna no formato que o Flutter já espera
    return {
        "content": [assistant_text],
        "images":  []
    }


@app.get("/health")
async def health():
    return {"status": "ok", "sessions_ativas": len(sessions)}


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

                # Retorna dados das threads, incluindo a data da primeira mensagem...
                cur.execute("""
                    SELECT t.id as thread_id, t.subject, a.name as agent_name, a.title as agent_title, MIN(c.created_at) as created_at
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
                    SELECT c.id, c.message, c.origem, c.created_at
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
            role = "user" if m["origem"] == "usuario" else "assistant"
            formatted_messages.append({
                "role": role,
                "content": m["message"]
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