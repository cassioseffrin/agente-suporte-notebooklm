"""
Script de correção: Regenera os assuntos (subjects) de threads no PostgreSQL
usando a API LiteLLM / GPT-OSS para conversas existentes com título "Nova conversa...".

Uso:
  python fix_thread_subjects.py             # Atualiza no banco
  python fix_thread_subjects.py --dry-run   # Apenas simula e exibe os títulos gerados
  python fix_thread_subjects.py --limit 10   # Processa no máximo 10 threads
"""

import asyncio
import os
import re
import sys
import argparse
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from openai import AsyncOpenAI

# Carregar variáveis do .env do backend
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# Configurações
DB_HOST = os.environ.get("DB_HOST", "192.168.50.21")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "agente_suporte")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "Arpa@2010")

LITELLM_API_BASE = os.environ.get("LITELLM_API_BASE", "https://apiai.arpasistemas.com.br/v1")
LITELLM_API_KEY  = os.environ.get("LITELLM_API_KEY", "")
LITELLM_MODEL    = os.environ.get("LITELLM_MODEL", "gptoss-reasoning")


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
    )


async def generate_subject(client: AsyncOpenAI, user_msg: str, agent_msg: str = "") -> str:
    """Gera um título de assunto curto para uma thread com base no primeiro turno."""
    user_truncated = (user_msg or "")[:300]
    agent_truncated = (agent_msg or "")[:300]

    prompt = (
        "Gere um título conciso (máximo 60 caracteres) em português do Brasil para a seguinte interação de suporte. "
        "Retorne APENAS o título, sem aspas, sem pontuação final e sem explicações.\n\n"
        f"Usuário: {user_truncated}\n"
        f"Assistente: {agent_truncated}"
    )

    try:
        response = await client.chat.completions.create(
            model=LITELLM_MODEL,
            max_tokens=512,
            temperature=0.3,
            timeout=30.0,
            messages=[{"role": "user", "content": prompt}]
        )
        msg = response.choices[0].message
        raw_content = msg.content or ""
        
        # Limpar raciocínio e pontuação/aspas
        raw_content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL)
        new_subject = raw_content.strip().strip('"\'`')
        if new_subject:
            return new_subject[:200]
    except Exception as e:
        print(f"  └ Erro ao gerar com LLM: {e}")
    
    return ""


async def process_threads(dry_run: bool = False, limit: int = 0):
    conn = get_db_connection()
    client = AsyncOpenAI(api_key=LITELLM_API_KEY, base_url=LITELLM_API_BASE)

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT t.id, t.subject,
                    (SELECT c.message FROM chat c JOIN chat_thread ct ON ct.chat_id = c.id WHERE ct.thread_id = t.id AND c.origem = 'usuario' ORDER BY c.created_at ASC, c.id ASC LIMIT 1) AS user_msg,
                    (SELECT c.message FROM chat c JOIN chat_thread ct ON ct.chat_id = c.id WHERE ct.thread_id = t.id AND c.origem = 'agente' ORDER BY c.created_at ASC, c.id ASC LIMIT 1) AS agent_msg
                FROM thread t
                WHERE (t.subject LIKE 'Nova conversa com Sistema Control%' OR t.subject = 'indefinido' OR t.subject IS NULL OR t.subject = '')
                  AND EXISTS (
                      SELECT 1 FROM chat_thread ct 
                      JOIN chat c ON c.id = ct.chat_id 
                      WHERE ct.thread_id = t.id AND c.origem = 'usuario'
                  )
                ORDER BY t.id
            """
            if limit > 0:
                query += f" LIMIT {limit}"

            cur.execute(query)
            threads = cur.fetchall()

        total = len(threads)
        print(f"Encontradas {total} thread(s) com mensagens precisando de novo assunto.")
        if dry_run:
            print("MODO DRY-RUN ATIVADO: Nenhuma alteração será salva no banco de dados.\n")
        else:
            print("MODO DE EXECUÇÃO REAL: Os assuntos serão atualizados no PostgreSQL.\n")

        updated_count = 0
        error_count = 0

        for i, t in enumerate(threads, 1):
            thread_id = t["id"]
            old_subject = t["subject"] or ""
            user_msg = t["user_msg"] or ""
            agent_msg = t["agent_msg"] or ""

            print(f"[{i}/{total}] Thread: {thread_id}")
            print(f"  ├ Assunto antigo: {old_subject!r}")
            print(f"  ├ Msg Usuário   : {user_msg[:70]!r}")

            new_subject = await generate_subject(client, user_msg, agent_msg)

            if new_subject:
                print(f"  └ NOVO Assunto  : {new_subject!r}")
                if not dry_run:
                    with conn.cursor() as update_cur:
                        update_cur.execute(
                            "UPDATE thread SET subject = %s WHERE id = %s;",
                            (new_subject, thread_id)
                        )
                    conn.commit()
                updated_count += 1
            else:
                print("  └ Falha ao gerar novo assunto (mantido antigo).")
                error_count += 1

            print()

        print("="*60)
        print(f"Resumo da execução:")
        print(f"  - Processadas: {total}")
        print(f"  - Atualizadas: {updated_count}")
        print(f"  - Falhas    : {error_count}")
        if dry_run:
            print("\nExecute sem '--dry-run' para aplicar as mudanças no banco de dados.")

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Atualiza assuntos de threads no PostgreSQL usando LiteLLM")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem salvar alterações no banco")
    parser.add_argument("--limit", type=int, default=0, help="Limita o número de threads a processar")
    args = parser.parse_args()

    asyncio.run(process_threads(dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
