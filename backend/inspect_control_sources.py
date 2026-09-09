#!/usr/bin/env python3
"""
Script para inspecionar fontes de um notebook no NotebookLM e calcular o tamanho de cada arquivo.
Exibe: Título, ID, Tipo, Contagem de Caracteres, Palavras e Tamanho (KB).
"""

import asyncio
import sys
from pathlib import Path

# ID padrão: "Pratto Control" na conta arpa.drive@gmail.com
DEFAULT_NOTEBOOK_ID = "d6b5295e-e808-45d2-aa67-315d94dfc6e3"

def get_storage_path() -> Path:
    profile_path = Path.home() / ".notebooklm" / "profiles" / "default" / "storage_state.json"
    legacy_path = Path.home() / ".notebooklm" / "storage_state.json"
    if profile_path.exists():
        return profile_path
    if legacy_path.exists():
        return legacy_path
    return profile_path

async def fetch_source_size(client, notebook_id: str, source, sem: asyncio.Semaphore):
    async with sem:
        try:
            fulltext = await client.sources.get_fulltext(notebook_id, source.id)
            chars = fulltext.char_count or len(fulltext.content or "")
            words = len(fulltext.content.split()) if fulltext.content else 0
            size_kb = len((fulltext.content or "").encode("utf-8")) / 1024
            return {
                "id": source.id,
                "title": source.title,
                "type": getattr(source, "kind", "pdf") or "pdf",
                "chars": chars,
                "words": words,
                "size_kb": size_kb,
                "error": None
            }
        except Exception as e:
            return {
                "id": source.id,
                "title": source.title,
                "type": getattr(source, "kind", "pdf") or "pdf",
                "chars": 0,
                "words": 0,
                "size_kb": 0.0,
                "error": str(e)
            }

async def inspect_notebook(notebook_id: str):
    try:
        from notebooklm.client import NotebookLMClient
    except ImportError:
        print("Erro: A biblioteca 'notebooklm-py' não está instalada no ambiente Python atual.")
        print("Execute: pip install 'notebooklm-py[browser]'")
        return

    storage = get_storage_path()
    if not storage.exists():
        print(f"Erro: Arquivo de sessão não encontrado em {storage}")
        return

    print(f"Conectando ao NotebookLM com a sessão: {storage} ...")
    print(f"Inspecionando notebook: {notebook_id}\n")

    async with NotebookLMClient.from_storage(storage) as client:
        # Obter lista de fontes
        print("Obtendo lista de fontes...")
        sources = await client.sources.list(notebook_id)
        total_sources = len(sources)
        print(f"Total de {total_sources} fontes encontradas. Coletando tamanho e métricas de texto...\n")

        # Buscar tamanho de texto concorrentemente (máx 8 conexões paralelas)
        sem = asyncio.Semaphore(8)
        tasks = [fetch_source_size(client, notebook_id, s, sem) for s in sources]
        results = await asyncio.gather(*tasks)

        # Exibir tabela formatada
        print("=" * 105)
        print(f"{'#':<3} | {'ID':<8} | {'TIPO':<4} | {'CARACTERES':>10} | {'PALAVRAS':>9} | {'TAM. (KB)':>10} | {'TÍTULO DA FONTE'}")
        print("=" * 105)

        total_chars = 0
        total_words = 0
        total_kb = 0.0

        for idx, item in enumerate(results, start=1):
            sid = item["id"][:8]
            stype = str(item["type"]).replace("SourceKind.", "").upper()[:4]
            chars = item["chars"]
            words = item["words"]
            size_kb = item["size_kb"]
            title = item["title"]

            total_chars += chars
            total_words += words
            total_kb += size_kb

            print(f"{idx:02d}  | {sid} | {stype:<4} | {chars:>10,d} | {words:>9,d} | {size_kb:>9.1f} KB | {title[:55]}")

        print("=" * 105)
        print(f"TOTAIS ACUMULADOS: {total_sources} fontes | {total_chars:,} caracteres | {total_words:,} palavras | {total_kb:.1f} KB (~{total_kb/1024:.2f} MB)")
        print("=" * 105)

        # Top 5 maiores fontes
        sorted_by_size = sorted(results, key=lambda x: x["chars"], reverse=True)
        print("\nTOP 5 MAIORES FONTES (MAIOR CONSUMO DE CONTEXTO):")
        for i, item in enumerate(sorted_by_size[:5], 1):
            print(f" {i}. {item['title']} ({item['chars']:,} chars | {item['words']:,} palavras | {item['size_kb']:.1f} KB)")
        print("-" * 105)

def main():
    notebook_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_NOTEBOOK_ID
    asyncio.run(inspect_notebook(notebook_id))

if __name__ == "__main__":
    main()
