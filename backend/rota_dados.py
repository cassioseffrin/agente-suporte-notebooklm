"""
Roteamento por LLM (tool calling) entre RAG e API de dados do Vimbo.

Usado quando o widget envia o token do usuário logado (header X-User-Token):
  - 'rag'   -> dúvida de uso do sistema: segue o fluxo normal (NotebookLM).
  - 'dados' -> pedido que precisa de dados reais da empresa: consulta a API de dados
               do Vimbo repassando o token do usuário como Bearer.

Estado por thread (em memória, como `sessions` no main):
  - conversa_id da API de dados, para manter o contexto entre perguntas.
  - última rota escolhida, para tratar confirmações curtas ("sim", "confirmo").
"""

import os
import re
import json
from typing import Optional, Dict, Any, List

import httpx
from openai import AsyncOpenAI

ROTEADOR_LLM_API_BASE = os.environ.get("ROTEADOR_LLM_API_BASE", os.environ.get("LITELLM_API_BASE", "https://apiai.arpasistemas.com.br/v1"))
ROTEADOR_LLM_API_KEY  = os.environ.get("ROTEADOR_LLM_API_KEY", "")
ROTEADOR_LLM_MODEL    = os.environ.get("ROTEADOR_LLM_MODEL", "qwen3-8b")
DADOS_API_URL         = os.environ.get("DADOS_API_URL", "https://api-teste.smartweb.inf.br/v1/chat-agent/chat")
DADOS_API_TIMEOUT     = float(os.environ.get("DADOS_API_TIMEOUT", "120"))

ROTEADOR_HISTORICO = 4  # últimas N mensagens da thread enviadas como contexto ao roteador

SYSTEM_PROMPT = (
    "Você é o roteador do assistente do ERP Vimbo. Sua única tarefa é decidir para onde a pergunta do usuário vai, "
    "chamando a função escolher_rota. Não responda a pergunta.\n\n"
    "ROTAS:\n"
    "- rag: dúvidas sobre COMO usar o Vimbo. Telas, menus, passo a passo, configurações, onde encontrar um relatório, "
    "o que significa um campo, regras do sistema.\n"
    "- dados: pedidos que precisam de dados reais da empresa no banco. Relatórios, listas, totais, saldos, "
    "contas a pagar/receber, vendas, clientes, além de criar ou cadastrar registros.\n\n"
    "REGRAS:\n"
    "- Se a pergunta pede números, valores, listas ou registros da empresa, é dados.\n"
    "- Se pede explicação, procedimento ou localização no sistema, é rag.\n"
    "- Pergunta ambígua como \"como ver minhas vendas?\": escolha rag.\n"
    "- As mensagens anteriores são só contexto. Classifique a ÚLTIMA mensagem do usuário; se ela continua "
    "uma consulta de dados (ex.: \"e no mês passado?\"), é dados.\n"
    "- Em pergunta_reescrita, reescreva a pergunta de forma objetiva e completa, sem saudações."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "escolher_rota",
            "description": "Define se a pergunta vai para o RAG (documentação do Vimbo) ou para a API Laravel (dados da empresa).",
            "parameters": {
                "type": "object",
                "properties": {
                    "rota": {
                        "type": "string",
                        "enum": ["rag", "dados"],
                        "description": "rag = como usar o sistema; dados = consulta ou gravação na base da empresa.",
                    },
                    "pergunta_reescrita": {
                        "type": "string",
                        "description": "Pergunta do usuário reescrita de forma objetiva.",
                    },
                    "motivo": {
                        "type": "string",
                        "description": "Justificativa curta da escolha, em até 15 palavras.",
                    },
                },
                "required": ["rota", "pergunta_reescrita", "motivo"],
            },
        },
    }
]

RE_CONFIRMACAO = re.compile(
    r"^(sim|s|ok|okay|pode|pode sim|pode (fazer|seguir|gravar|criar|lancar|lançar)|confirmo|confirmado|confirma|"
    r"isso|isso mesmo|correto|certo|manda|vai|prossiga|prosseguir|pode prosseguir)[.! ]*$",
    re.IGNORECASE,
)

MSG_SESSAO_EXPIRADA = "Sua sessão no Vimbo expirou. Atualize a página e tente novamente."
MSG_ERRO_DADOS = "Não consegui consultar os dados da sua empresa agora. Tente novamente em instantes."

# Sem chave configurada o roteador fica desligado (tudo segue para o RAG) em vez de derrubar a API no import.
roteador_client = AsyncOpenAI(api_key=ROTEADOR_LLM_API_KEY, base_url=ROTEADOR_LLM_API_BASE) if ROTEADOR_LLM_API_KEY else None
if not roteador_client:
    print("[ROTA-LLM] ROTEADOR_LLM_API_KEY não configurada: roteamento para dados desativado")

dados_conversas: Dict[str, str] = {}  # thread_id -> conversa_id da API de dados
ultima_rota: Dict[str, str] = {}      # thread_id -> 'rag' | 'dados'


def _parse_argumentos(message) -> Optional[Dict[str, Any]]:
    """Extrai os argumentos de escolher_rota do tool call (ou de um JSON solto no content)."""
    raw = None
    if getattr(message, "tool_calls", None):
        raw = message.tool_calls[0].function.arguments
    elif message.content:
        m = re.search(r"\{.*\}", message.content, re.DOTALL)
        raw = m.group(0) if m else None
    if not raw:
        return None
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return args if args.get("rota") in ("rag", "dados") else None


async def decidir_rota(thread_id: str, mensagem: str, historico: List[Dict[str, str]]) -> Dict[str, Any]:
    """Decide a rota da mensagem. Em caso de falha do LLM, segue para o RAG (fail-safe)."""
    if not roteador_client:
        return {"rota": "rag", "pergunta_reescrita": mensagem, "motivo": "roteador desativado", "confirmado": False}

    # Confirmação curta logo após uma resposta da API de dados (ex.: "sim" para gravar uma venda)
    if ultima_rota.get(thread_id) == "dados" and RE_CONFIRMACAO.match(mensagem.strip()):
        print(f"[ROTA-LLM] Thread {thread_id} confirmação detectada -> dados (confirmado=true)")
        return {"rota": "dados", "pergunta_reescrita": mensagem, "motivo": "confirmação", "confirmado": True}

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in historico[-ROTEADOR_HISTORICO:]:
        messages.append({"role": h["role"], "content": (h.get("content") or "")[:500]})
    messages.append({"role": "user", "content": mensagem})

    rota = {"rota": "rag", "pergunta_reescrita": mensagem, "motivo": "fallback", "confirmado": False}
    try:
        resp = await roteador_client.chat.completions.create(
            model=ROTEADOR_LLM_MODEL,
            temperature=0,
            max_tokens=300,
            messages=messages,
            tools=TOOLS,
            tool_choice={"type": "function", "function": {"name": "escolher_rota"}},
            timeout=20,
        )
        args = _parse_argumentos(resp.choices[0].message)
        if args:
            rota.update(args)
            rota["pergunta_reescrita"] = (args.get("pergunta_reescrita") or "").strip() or mensagem
        else:
            print(f"[ROTA-LLM] Thread {thread_id} resposta sem tool call válida, seguindo para RAG")
    except Exception as e:
        print(f"[ROTA-LLM] Thread {thread_id} erro no roteador, seguindo para RAG: {e}")

    ultima_rota[thread_id] = rota["rota"]
    print(f"[ROTA-LLM] Thread {thread_id} rota={rota['rota']} motivo={rota.get('motivo')!r} reescrita={rota['pergunta_reescrita']!r}")
    return rota


def _formatar_arquivos(arquivos) -> str:
    """Formata a lista `arquivos` da API de dados como links markdown."""
    linhas = []
    for i, a in enumerate(arquivos or [], 1):
        if isinstance(a, str):
            url, nome = a, f"Arquivo {i}"
        elif isinstance(a, dict):
            url = a.get("url") or a.get("link") or a.get("path") or next(
                (v for v in a.values() if isinstance(v, str) and v.startswith("http")), None)
            nome = a.get("nome") or a.get("name") or a.get("titulo") or f"Arquivo {i}"
        else:
            continue
        if url:
            linhas.append(f"- [{nome}]({url})")
    return ("\n\n**Arquivos:**\n" + "\n".join(linhas)) if linhas else ""


async def responder_dados(thread_id: str, pergunta: str, user_token: str, confirmado: bool = False) -> str:
    """Consulta a API de dados com o token do usuário e devolve o texto da resposta (nunca lança)."""
    ultima_rota[thread_id] = "dados"
    body = {
        "mensagem": pergunta,
        "conversa_id": dados_conversas.get(thread_id),
        "confirmado": confirmado,
    }
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=DADOS_API_TIMEOUT) as client:
            r = await client.post(DADOS_API_URL, json=body, headers=headers)
        if r.status_code in (401, 403):
            print(f"[DADOS] Thread {thread_id} token recusado (HTTP {r.status_code})")
            return MSG_SESSAO_EXPIRADA
        payload = r.json()
        if r.status_code >= 400 or not payload.get("success"):
            print(f"[DADOS] Thread {thread_id} erro HTTP {r.status_code}: {str(payload.get('error'))[:300]}")
            return MSG_ERRO_DADOS
        data = payload.get("data") or {}
        if data.get("conversa_id"):
            dados_conversas[thread_id] = data["conversa_id"]
        tools = [t.get("tool") for t in data.get("tool_calls") or []]
        print(f"[DADOS] Thread {thread_id} conversa_id={data.get('conversa_id')} tools={tools}")
        if data.get("arquivos"):
            print(f"[DADOS] Thread {thread_id} arquivos={json.dumps(data['arquivos'], ensure_ascii=False)[:500]}")
        resposta = (data.get("resposta") or "").strip()
        return (resposta or MSG_ERRO_DADOS) + _formatar_arquivos(data.get("arquivos"))
    except Exception as e:
        print(f"[DADOS] Thread {thread_id} falha ao consultar API de dados: {e}")
        return MSG_ERRO_DADOS
