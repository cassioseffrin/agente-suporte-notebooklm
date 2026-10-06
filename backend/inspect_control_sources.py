#!/usr/bin/env python3
"""
Script para inspecionar fontes de um notebook no NotebookLM e calcular o tamanho de cada arquivo.
Exibe: Título, ID, Tipo, Contagem de Caracteres, Palavras e Tamanho (KB).
"""

import asyncio
import sys
from pathlib import Path


# arpa.drive@gmail.com
# ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
# ┃ ID                                   ┃ Title                                   ┃ Access ┃ Created    ┃
# ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
# │ fefc0dee-766c-430e-a173-9d6b388e3ce7 │ Sistema Control                         │ Owner  │ 2026-05-20 │
# │ 31309341-208e-47b3-b853-6f30f250c3ad │ Doc. Control                            │ Owner  │ 2026-09-08 │
# │ 12962c89-92c2-4959-a632-7ca1eb31ff94 │ Teste suporte ( só banco e fr3)         │ Owner  │ 2026-09-15 │
# │ 6ad5ff40-500e-4d2e-950c-23f9a026753f │ Banco de dados (Comandos, tabelas..)    │ Owner  │ 2025-07-10 │
# │ 20428d2e-48f1-411a-9227-5bdf8882f3a2 │ Interno - Control / PDV (Versão antiga) │ Owner  │ 2025-07-14 │
# │ 84186722-4c03-4c2a-8fc8-ab8e1a4129f1 │ Interno - Control / PDV                 │ Owner  │ 2026-05-20 │
# └──────────────────────────────────────┴─────────────────────────────────────────┴────────┴────────────┘


# suporte@vimbo.com.br
# ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
# ┃ ID                                   ┃ Title                                  ┃ Access ┃ Created    ┃
# ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
# │ d6b5295e-e808-45d2-aa67-315d94dfc6e3 │ Pratto Control                         │ Owner  │ 2026-07-15 │
# │ c14ec983-b856-4062-a556-da21fd588cfa │ Vimbo Atualizado                       │ Owner  │ 2026-10-02 │
# │ 2540dcae-d405-4469-8593-6b537c170572 │ Reforma Tributária                     │ Owner  │ 2026-03-27 │
# │ a1189726-1fe9-4e7e-a3d9-dfc226aa1b90 │ Sistema Vimbo - Versao Antiga          │ Owner  │ 2025-07-01 │
# │ fefc0dee-766c-430e-a173-9d6b388e3ce7 │ Sistema Control                        │ Viewer │ 2026-05-20 │
# │ 6c4af92f-8642-4b3e-93e2-6c7558367fac │ Arpag - Pagamento Integrado            │ Owner  │ 2026-04-08 │
# │ a219f160-0fa2-4393-9b93-d23fa4211486 │ Assistente Aplicativo Ordem de Serviço │ Owner  │ 2026-04-07 │
# │ 5a83e6e6-8105-4bc4-b371-6d38c59bcade │ Smart Força de Vendas                  │ Owner  │ 2026-03-19 │
# │ b81a4b19-848f-49b0-97fc-525c12576ab3 │                                        │ Owner  │ 2026-09-11 │
# │ 40be27a2-88a1-46b2-8c83-dc1d84b6bb77 │ Vimbo e funcionalidades                │ Viewer │ 2025-08-26 │
# │ 60d615c1-4043-4fb7-936c-be3ac17f6468 │ Inativado - Sistema Control            │ Owner  │ 2026-03-27 │
# │ 62d4ea93-e793-4164-9b9e-aa59bdbd162a │ Rejeições de notas                     │ Owner  │ 2026-04-22 │
# │ dbf30fac-83b6-4b8f-947e-da858fe679e1 │ Rejeições NFS-e                        │ Viewer │ 2026-01-20 │
# └──────────────────────────────────────┴────────────────────────────────────────┴────────┴────────────┘

# DEFAULT_NOTEBOOK_ID = "31309341-208e-47b3-b853-6f30f250c3ad" #doccontrol
# DEFAULT_NOTEBOOK_ID = "fefc0dee-766c-430e-a173-9d6b388e3ce7" #control oficial
DEFAULT_NOTEBOOK_ID = "2540dcae-d405-4469-8593-6b537c170572" #reforma

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
