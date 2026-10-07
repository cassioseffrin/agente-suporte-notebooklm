// Pré-roteador por regras. Roda ANTES do RAG.
//   'manual'      -> claramente dúvida de uso: vai pro RAG
//   'direto'      -> saudação, vago, texto sem sentido, dado do negócio ou fora de escopo conhecido:
//                    resposta direta, sem RAG
//   'indefinido'  -> sem regra: segue o fluxo normal (RAG)
//
// Princípio: "direto" é uma classe estreita e só é escolhida por regra explícita.
// Qualquer dúvida cai no RAG, porque errar para esse lado custa só uma consulta a mais,
// enquanto errar para "direto" deixa um cliente com dúvida real sem resposta.
//
// O QUE ESTAS REGRAS NÃO PEGAM: perguntas fora do escopo do ERP escritas em linguagem
// normal (ex.: "o que é uma bomba periférica de 1 cv"). Para isso use o score de
// similaridade do próprio RAG: se o melhor trecho do manual for fraco, responda direto.

export const norm = (s) =>
  s
    .toLowerCase()
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

// Pergunta de procedimento: sempre manual, avaliada primeiro.
const PROCEDIMENTO =
  /^(como|onde|porque|por que|o que significa|preciso|e possivel|e como|qual (e )?(o )?(menu|caminho|opcao|cfop|cfp|link))\b/;

// Pedido de VALOR de um dado do negócio (consulta ao banco do cliente).
const DADOS = [
  /\b(mais|menos) vendid[oa]s?\b/,
  /\bquantos? (clientes|produtos|pedidos|vendas|fornecedores)\b/,
  /\bquanto (eu )?(faturei|vendi|recebi|gastei|comprei)\b/,
  /\b(maior|menor) (valor|preco)\b/,
  /\b(total|soma) d[eoa]s? (contas|vendas|faturamento|pedidos|compras)\b/,
  /\bestoque d[oae] (produto|item)\b/,
  /\bqual (cliente|produto|vendedor|fornecedor) (mais|menos|tem (maior|menor))\b/,
  /\bqual (cliente|produto|vendedor|fornecedor)\b.*\b(mais|menos)\b/,
  /\b(quantas? licencas?|mais de uma licenca|licencas? (eu )?(tenho|temos))\b/,
];

// Assuntos que o manual não deve responder.
const FORA_ESCOPO = [/\bpalavra passe\b/];

// Saudação: a frase inteira só tem palavras sociais.
const SOCIAL = new Set(
  'oi ola opa ai e bom boa dia tarde noite tudo bem certo obrigado obrigada valeu ok blz'.split(' '),
);

// Pedido vago, sem assunto (a frase inteira precisa ser isso).
const VAGO =
  /^(quero explicacao|me explica|me explique|me ajuda|ajuda|preciso de ajuda|quero ajuda|minha ultima duvida|estava apenas testando aqui|estou testando|testando|teste)$/;

const TERMO_ERP =
  /\b(cfop|cfp|ncm|cst|cest|icms|ipi|pis|cofins|nfe|nfce|nfse|mdfe|cte|sped|xml|pdv|tef|sat|gtin|ean|cnpj|cpf|boleto|cheque|aliquota|danfe|nota|estoque|pedido|orcamento|caixa|etiqueta|relatorio|cliente|produto|venda|compra|backup|sangria)\b/;
const PREFIXO_ERP = /^(cfop|ncm|cst|icms|nfe|nfce|nfse|mdfe|sped|xml|pdv|gtin|ean)/;

// Palavra solta que parece teclado aleatório ou código sem sentido.
function pareceLixo(t) {
  if (TERMO_ERP.test(t) || PREFIXO_ERP.test(t) || /^f\d{1,2}$/.test(t)) return false;
  return t.length <= 2 || (/\d/.test(t) && /[a-z]/.test(t)) || !/[aeiou]/.test(t) || /^(.)\1{2,}$/.test(t);
}

export function rotear(pergunta) {
  const t = norm(pergunta ?? '');
  if (!t) return { destino: 'direto', motivo: 'vazio' };
  if (FORA_ESCOPO.some((r) => r.test(t))) return { destino: 'direto', motivo: 'fora do escopo' };
  if (PROCEDIMENTO.test(t)) return { destino: 'manual', motivo: 'procedimento' };
  if (DADOS.some((r) => r.test(t))) return { destino: 'direto', motivo: 'dado do negocio' };
  const tokens = t.split(' ');
  if (tokens.length <= 6 && tokens.every((w) => SOCIAL.has(w))) return { destino: 'direto', motivo: 'saudacao' };
  if (VAGO.test(t)) return { destino: 'direto', motivo: 'vago' };
  if (tokens.length === 1 && pareceLixo(t)) return { destino: 'direto', motivo: 'sem sentido' };
  return { destino: 'indefinido', motivo: 'sem regra' };
}
