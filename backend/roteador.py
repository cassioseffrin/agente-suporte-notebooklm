"""
Módulo de pré-roteamento de mensagens.
Avalia a mensagem antes de consumir o RAG pesado (NotebookLM / LLM).

Destinos:
  - 'manual'     -> Dúvida clara de procedimento/uso: vai para o RAG.
  - 'direto'     -> Saudação, vago, ruído/lixo, dados do negócio ou fora de escopo:
                   responde diretamente sem RAG.
  - 'indefinido' -> Sem regra explícita: segue para o RAG (fail-safe seguro).

Suporta customização via `roteador_config.json` ou recarregamento em tempo de execução.
"""

import re
import json
import unicodedata
from pathlib import Path
from typing import Dict, Any, Optional

CONFIG_FILE = Path(__file__).resolve().parent / "roteador_config.json"


def normalizar_texto(texto: Optional[str]) -> str:
    """Normaliza o texto removendo acentos, caracteres especiais e espaços extras."""
    if not texto:
        return ""
    # Remove acentos
    t = unicodedata.normalize("NFD", texto)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.lower()
    # Remove caracteres especiais
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    # Colapsa múltiplos espaços
    t = re.sub(r"\s+", " ", t).strip()
    return t


class Roteador:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or CONFIG_FILE
        self.carregar_config()

    def carregar_config(self):
        """Carrega e compila as regras do arquivo JSON."""
        data = {}
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[Roteador] Erro ao ler {self.config_path}: {e}")

        self.respostas_padrao = data.get("respostas_padrao", {
            "saudacao": "Olá! Como posso ajudar você hoje com as rotinas e dúvidas do sistema?",
            "dado_do_negocio": "Não tenho acesso direto ao banco de dados da sua empresa para consultar valores ou dados confidenciais. Posso te orientar sobre em quais telas e relatórios do sistema você encontra essas informações!",
            "vago": "Olá! Poderia detalhar melhor a sua dúvida sobre o sistema ou a operação que deseja realizar?",
            "sem_sentido": "Não consegui compreender a sua mensagem. Poderia formular sua dúvida sobre o sistema novamente?",
            "fora_do_escopo": "Essa informação não está disponível nas rotinas do sistema. Caso precise de suporte com as funções do ERP, estou à disposição!",
        })

        # Procedimentos
        prefixos_proc = data.get("procedimentos_prefixos", [
            "como", "onde", "porque", "por que", "o que significa", "preciso", "e possivel", "e como",
            "qual menu", "qual caminho", "qual opcao", "qual cfop", "qual cfp", "qual link"
        ])
        escaped_proc = [re.escape(p) for p in prefixos_proc]
        self.re_procedimento = re.compile(rf"^({'|'.join(escaped_proc)})\b", re.IGNORECASE)

        # Dados do negócio
        padroes_dados = data.get("dados_negocio_padroes", [
            r"\b(mais|menos) vendid[oa]s?\b",
            r"\bquantos? (clientes|produtos|pedidos|vendas|fornecedores)\b",
            r"\bquanto (eu )?(faturei|vendi|recebi|gastei|comprei)\b",
            r"\b(maior|menor) (valor|preco)\b",
            r"\b(total|soma) d[eoa]s? (contas|vendas|faturamento|pedidos|compras)\b",
            r"\bestoque d[oae] (produto|item)\b",
            r"\bqual (cliente|produto|vendedor|fornecedor) (mais|menos|tem (maior|menor))\b",
            r"\bqual (cliente|produto|vendedor|fornecedor)\b.*\b(mais|menos)\b",
            r"\b(quantas? licencas?|mais de uma licenca|licencas? (eu )?(tenho|temos))\b",
        ])
        self.re_dados = [re.compile(p, re.IGNORECASE) for p in padroes_dados]

        # Fora de escopo
        padroes_fora = data.get("fora_escopo_padroes", [r"\bpalavra[- ]?passe\b"])
        self.re_fora = [re.compile(p, re.IGNORECASE) for p in padroes_fora]

        # Social / Saudações
        self.social_palavras = set(data.get("social_palavras", [
            "oi", "ola", "opa", "ai", "e", "bom", "boa", "dia", "tarde", "noite",
            "tudo", "bem", "certo", "obrigado", "obrigada", "valeu", "ok", "blz",
            "como", "vai", "esta", "voce", "vc"
        ]))

        saudacao_padroes = data.get("saudacao_padroes", [
            r"^como (vai|esta|estao)( voce| vc)?$",
            r"^(oi|ola|opa|e ai|bom dia|boa tarde|boa noite)(,?[ ]+)?(como (vai|esta)|tudo bem)?$",
            r"^tudo (bem|bom|certo|tranquilo|joia|beleza)(\?)?$",
            r"^(oi|ola|opa|e ai|bom dia|boa tarde|boa noite)( [a-z]+)? tudo bem$"
        ])
        self.re_saudacao = [re.compile(p, re.IGNORECASE) for p in saudacao_padroes]

        # Perguntas vagas
        vago_padroes = data.get("vago_padroes", [
            r"^quero explicacao$", r"^me explica$", r"^me explique$", r"^me ajuda$",
            r"^ajuda$", r"^preciso de ajuda$", r"^quero ajuda$", r"^minha ultima duvida$",
            r"^estava apenas testando aqui$", r"^estou testando$", r"^testando$", r"^teste$"
        ])
        self.re_vago = re.compile(rf"({'|'.join(vago_padroes)})", re.IGNORECASE)

        # Termos ERP para proteger termos curtos
        termos_erp = data.get("termos_erp", [
            "cfop", "cfp", "ncm", "cst", "cest", "icms", "ipi", "pis", "cofins",
            "nfe", "nfce", "nfse", "mdfe", "cte", "sped", "xml", "pdv", "tef",
            "sat", "gtin", "ean", "cnpj", "cpf", "boleto", "cheque", "aliquota",
            "danfe", "nota", "estoque", "pedido", "orcamento", "caixa", "etiqueta",
            "relatorio", "cliente", "produto", "venda", "compra", "backup", "sangria"
        ])
        self.re_termo_erp = re.compile(rf"\b({'|'.join(termos_erp)})\b", re.IGNORECASE)

        prefixos_erp = data.get("prefixos_erp", [
            "cfop", "ncm", "cst", "icms", "nfe", "nfce", "nfse", "mdfe", "sped", "xml", "pdv", "gtin", "ean"
        ])
        self.re_prefixo_erp = re.compile(rf"^({'|'.join(prefixos_erp)})", re.IGNORECASE)

    def parece_lixo(self, t: str) -> bool:
        """Identifica mensagens soltas que parecem ruído ou digitação aleatória."""
        if self.re_termo_erp.search(t) or self.re_prefixo_erp.search(t) or re.match(r"^f\d{1,2}$", t):
            return False
        # Sequência repetitiva ex: "aaaa", "..."
        if re.search(r"(.)\1{2,}", t):
            return True
        # Muito curta (1 ou 2 caracteres e não é comando de ERP)
        if len(t) <= 2:
            return True
        # Alfanumérico misturado sem padrão (ex: ogi9)
        if any(c.isdigit() for c in t) and any(c.isalpha() for c in t):
            return True
        # Sem vogais
        if not re.search(r"[aeiou]", t):
            return True
        return False

    def rotear(self, pergunta: Optional[str], assistant_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Analisa a pergunta e retorna:
        {
          "destino": "manual" | "direto" | "indefinido",
          "motivo": str,
          "resposta_direta": Optional[str]
        }
        """
        t = normalizar_texto(pergunta)
        if not t:
            return {
                "destino": "direto",
                "motivo": "vazio",
                "resposta_direta": self.respostas_padrao.get("vago")
            }

        # Fora do escopo explícito
        # Exceção: O notebook do CONTROL possui a palavra-passe na base de conhecimento.
        # Portanto, se o assistente for CONTROL, deixa buscar no RAG (destino: manual).
        e_control = assistant_name and "control" in assistant_name.lower()
        if any(r.search(t) for r in self.re_fora):
            if e_control:
                return {
                    "destino": "manual",
                    "motivo": "procedimento control",
                    "resposta_direta": None
                }
            return {
                "destino": "direto",
                "motivo": "fora do escopo",
                "resposta_direta": self.respostas_padrao.get("fora_do_escopo")
            }

        # 1. Saudações sociais explícitas (ex.: "como vai?", "como está?", "bom dia", "tudo bem?")
        # Avaliadas antes de procedimento para evitar que "como vai" caia no regex genérico de "como"
        if any(r.match(t) for r in self.re_saudacao):
            return {
                "destino": "direto",
                "motivo": "saudacao",
                "resposta_direta": self.respostas_padrao.get("saudacao")
            }

        tokens = t.split(" ")

        # Saudação social pura (até 6 palavras sociais conhecidas)
        if len(tokens) <= 6 and all(w in self.social_palavras for w in tokens):
            return {
                "destino": "direto",
                "motivo": "saudacao",
                "resposta_direta": self.respostas_padrao.get("saudacao")
            }

        # 2. Pergunta clara de procedimento -> SEMPRE RAG
        if self.re_procedimento.search(t):
            return {
                "destino": "manual",
                "motivo": "procedimento",
                "resposta_direta": None
            }

        # 3. Pedido de dados do negócio (ex: "mais vendidos", "quanto faturei")
        # Se contiver a palavra "relatorio", usuário provavelmente quer saber onde emitir no sistema -> vai pro RAG!
        if not re.search(r"\brelatorios?\b", t) and any(r.search(t) for r in self.re_dados):
            return {
                "destino": "direto",
                "motivo": "dado do negocio",
                "resposta_direta": self.respostas_padrao.get("dado_do_negocio")
            }

        # Pergunta vaga sem assunto
        if self.re_vago.match(t):
            return {
                "destino": "direto",
                "motivo": "vago",
                "resposta_direta": self.respostas_padrao.get("vago")
            }

        # Texto isolado sem sentido / ruído
        if len(tokens) == 1 and self.parece_lixo(t):
            return {
                "destino": "direto",
                "motivo": "sem sentido",
                "resposta_direta": self.respostas_padrao.get("sem_sentido")
            }

        # Sem regra específica -> Segue seguro para o RAG
        return {
            "destino": "indefinido",
            "motivo": "sem regra",
            "resposta_direta": None
        }


# Instância singleton para uso em todo o backend
roteador_instancia = Roteador()


def rotear(pergunta: Optional[str], assistant_name: Optional[str] = None) -> Dict[str, Any]:
    """Helper global para roteamento."""
    return roteador_instancia.rotear(pergunta, assistant_name=assistant_name)
