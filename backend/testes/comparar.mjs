// Compara REGRAS x LAYA (3 variantes de prompt) no seu log de perguntas reais.
//
// Uso:  LAYA_KEY=sua_chave node comparar.mjs perguntas.txt [./roteador.mjs]
// Opcionais: LAYA_URL, LAYA_LIMIT=30 (testa com poucas perguntas), LAYA_CONC=4 (chamadas em paralelo),
//            LAYA_CACHE=./cache-laya.json (as respostas ficam salvas; rodar de novo não repete chamadas).
//
// Variantes do Laya:
//   A = prompt anterior (com os nomes de rótulo originais)
//   B = prompt "padrão é manual" (nomes originais)
//   C = igual ao B, mas com rótulos simples: "manual" e "direto"  (testa se o NOME dos rótulos atrapalha)
//
// Métricas, contra o gabarito de "direto" (o mesmo do avaliar-log.mjs, julgamento meu):
//   direto pego = quanto do direto esperado foi para resposta direta
//   falso direto = perguntas que NÃO eram direto e foram para resposta direta (o erro caro)
//   precisão = direto pego / (direto pego + falso direto)

import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';

const arquivo = process.argv[2];
if (!arquivo) throw new Error('Uso: LAYA_KEY=... node comparar.mjs perguntas.txt [roteador.mjs]');
// const KEY = process.env.LAYA_KEY;
const KEY = 'sk-293480lakjfgdlqkj459021laksdfgjnkljhwerlk234';
if (!KEY) throw new Error('Defina LAYA_KEY');
const ENDPOINT = process.env.LAYA_URL ?? 'http://192.168.50.135:8007/v1/systemone';
const CONC = Number(process.env.LAYA_CONC ?? 4);
const LIMIT = Number(process.env.LAYA_LIMIT ?? 0);
const CACHE = process.env.LAYA_CACHE ?? './cache-laya.json';

const { rotear, norm } = await import(pathToFileURL(path.resolve(process.argv[3] ?? './roteador.mjs')).href);

// ---------- Leitura do log ----------
function lerPerguntas(txt) {
  const out = [];
  const linhas = txt.split(/\r?\n/);
  for (let i = 0; i < linhas.length; i++) {
    const l = linhas[i];
    if (!l.trim()) continue;
    if (l.startsWith('"') && !(l.length > 1 && l.trimEnd().endsWith('"'))) {
      const partes = [l.slice(1)];
      while (++i < linhas.length) {
        const m = linhas[i];
        if (m.trimEnd().endsWith('"')) {
          partes.push(m.trimEnd().slice(0, -1));
          break;
        }
        partes.push(m);
      }
      out.push(partes.join(' ').replace(/\s+/g, ' ').trim());
    } else {
      out.push(l.replace(/^"|"$/g, '').trim());
    }
  }
  return out;
}

// Gabarito de "direto" (igual ao avaliar-log.mjs): [categoria, texto normalizado, 'exato' | 'contem']
const GABARITO = [
  ['conversa/lixo', '', 'exato'],
  ['conversa/lixo', 'oi', 'exato'],
  ['conversa/lixo', 'ola', 'exato'],
  ['conversa/lixo', 'bom dia', 'exato'],
  ['conversa/lixo', 'obrigado', 'exato'],
  ['conversa/lixo', 'ogi9', 'exato'],
  ['conversa/lixo', 'cleint', 'exato'],
  ['conversa/lixo', 'quero explicacao', 'exato'],
  ['conversa/lixo', 'minha ultima duvida', 'exato'],
  ['conversa/lixo', 'estava apenas testando aqui', 'exato'],
  ['conversa/lixo', 'opa charles tudo bem', 'exato'],
  ['dado do cliente', 'qual produto eu mais tenho girando', 'contem'],
  ['dado do cliente', 'quantas licencas eu tenho', 'contem'],
  ['dado do cliente', 'se tenhos mais de uma licenca', 'contem'],
  ['fora do escopo', 'palavra passe do mes', 'contem'],
  ['fora do escopo', 'bomba periferica', 'contem'],
  ['fora do escopo', 'consegue fazer um orcamento destes materiais', 'contem'],
  ['fora do escopo', 'filtro hidraulico mb 1113', 'exato'],
  ['fora do escopo', 'lentes ar', 'exato'],
  ['fora do escopo', 'kit de filtro mb608', 'contem'],
];
const casa = (n, [, txt, modo]) => (modo === 'exato' ? n === txt : n.includes(txt));

// ---------- Variantes de prompt do Laya ----------
const ORIG = { manual: 'notebook_manual_sistema', direto: 'nao_buscar_buscar_notebook' };
const SIMPLES = { manual: 'manual', direto: 'direto' };

const textoA = (L) => ({
  instructions: `Classifique a pergunta do usuário. Se ela pede um DADO, número, lista ou ranking do negócio do cliente, escolha ${L.direto}. Só escolha ${L.manual} se a pergunta pede COMO FAZER algo no sistema.`,
  manual:
    'Pergunta de COMO FAZER uma ação ou configuração no sistema, em formato de passo a passo. Começa com como, onde fica, o que significa o campo, como configuro. Nunca pede números, valores ou rankings.',
  direto:
    'Pergunta sobre os DADOS do cliente, que exige consultar o banco de dados e não o manual: números, valores, listas, rankings, totais, buscas. Também perguntas genéricas ou abertas, palavras soltas sem sentido e assuntos fora do ERP.',
});
const textoB = (L) => ({
  instructions: `A maioria das perguntas é dúvida sobre o sistema e deve consultar o manual. Escolha ${L.direto} SOMENTE quando a pessoa pede o VALOR de um dado do negócio (um número, total, ranking ou lista), ou quando a mensagem não tem assunto nenhum.`,
  manual:
    'Dúvida sobre o sistema ERP, fiscal ou contábil: como fazer, onde fica o menu, o que significa um erro ou rejeição, qual configuração ou CFOP usar, como emitir, cancelar, importar, cadastrar, alterar, gerar relatório. Inclui assunto solto de módulo ou tema fiscal (aliquota icms, carta de correção, ajuste de estoque, nota de complemento, sped, xml, ncm, mdfe), mensagens de erro coladas e perguntas sobre onde ver ou como gerar um relatório.',
  direto:
    'Pedido do VALOR de um dado do negócio, para consultar o banco de dados: qual produto mais vendido, qual produto tem maior valor, quanto faturei no mês, quantos clientes tenho, estoque do produto X, total a receber. Ou mensagem sem assunto: saudação (oi, obrigado), pedido vago (quero explicação, me ajuda) e texto sem sentido (asdf, ogi9, teste).',
});

const montar = (nome, L, t) => ({
  nome,
  labels: L,
  instructions: t.instructions,
  criteria: { [L.manual]: t.manual, [L.direto]: t.direto },
});
const VARIANTES = [
  montar('A', ORIG, textoA(ORIG)),
  montar('B', ORIG, textoB(ORIG)),
  montar('C', SIMPLES, textoB(SIMPLES)),
];

// ---------- Chamadas (com cache) ----------
let cache = {};
try {
  cache = JSON.parse(fs.readFileSync(CACHE, 'utf8'));
} catch { }
const salvar = () => fs.writeFileSync(CACHE, JSON.stringify(cache));

async function chamar(texto, v) {
  for (let tentativa = 1; tentativa <= 2; tentativa++) {
    try {
      const r = await fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${KEY}` },
        body: JSON.stringify({
          state: { document: texto },
          model: 'multilingual',
          lang: 'pt',
          questions: { destino: { type: 'choice', instructions: v.instructions, criteria: v.criteria } },
        }),
        signal: AbortSignal.timeout(30000),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const p = (await r.json()).answers.destino.probabilities;
      return p[v.labels.direto] ?? 0;
    } catch (e) {
      if (tentativa === 2) throw new Error(`Falha em "${texto.slice(0, 40)}" (${v.nome}): ${e.message}`);
    }
  }
}

async function pool(itens, n, fn) {
  let i = 0;
  await Promise.all(
    Array.from({ length: n }, async () => {
      while (i < itens.length) await fn(itens[i++]);
    }),
  );
}

// ---------- Execução ----------
let perguntas = lerPerguntas(fs.readFileSync(arquivo, 'utf8'));
const unicas = [...new Set(perguntas)];
const alvo = LIMIT > 0 ? unicas.slice(0, LIMIT) : unicas;
console.log(`Perguntas no log: ${perguntas.length} | únicas: ${unicas.length} | usadas: ${alvo.length}`);

for (const v of VARIANTES) {
  const faltam = alvo.filter((t) => cache[`${v.nome}|${t}`] === undefined);
  console.log(`Laya ${v.nome}: ${alvo.length - faltam.length} em cache, ${faltam.length} a chamar`);
  let feitas = 0;
  await pool(faltam, CONC, async (t) => {
    cache[`${v.nome}|${t}`] = await chamar(t, v);
    if (++feitas % 25 === 0) {
      salvar();
      console.log(`  ${v.nome}: ${feitas}/${faltam.length}`);
    }
  });
  salvar();
}

const usadas = new Set(alvo);
const res = perguntas
  .filter((t) => usadas.has(t))
  .map((t) => {
    const n = norm(t);
    return {
      t,
      gold: GABARITO.some((g) => casa(n, g)),
      regra: rotear(t).destino,
      p: Object.fromEntries(VARIANTES.map((v) => [v.nome, cache[`${v.nome}|${t}`]])),
    };
  });

const pos = res.filter((r) => r.gold);
const neg = res.filter((r) => !r.gold);
console.log(`\nEsperado direto (gabarito): ${pos.length} | demais: ${neg.length}`);
console.log('(os "demais" incluem casos ambíguos, como perguntas de continuação, tratados como não-direto)\n');

const linha = (nome, pred) => {
  const tp = pos.filter(pred).length;
  const fp = neg.filter(pred).length;
  const prec = tp + fp ? ((tp / (tp + fp)) * 100).toFixed(0) + '%' : '-';
  console.log(`${nome.padEnd(26)} | ${String(tp).padStart(2)}/${pos.length} pegos | ${String(fp).padStart(3)}/${neg.length} falso direto | precisão ${prec}`);
};

console.log('método                     | direto pego | falso direto (erro caro) | precisão');
linha('regras', (r) => r.regra === 'direto');
for (const v of VARIANTES)
  for (const t of [0.3, 0.5, 0.7, 0.9]) linha(`laya ${v.nome} t=${t}`, (r) => r.p[v.nome] >= t);
for (const v of VARIANTES)
  for (const t of [0.5, 0.7, 0.9])
    linha(`regras + laya ${v.nome} t=${t}`, (r) => r.regra === 'direto' || (r.regra === 'indefinido' && r.p[v.nome] >= t));

for (const v of VARIANTES) {
  console.log(`\n--- Laya ${v.nome}: 5 piores falsos diretos (pDireto mais alto em pergunta que NÃO é direto) ---`);
  for (const r of [...neg].sort((a, b) => b.p[v.nome] - a.p[v.nome]).slice(0, 5))
    console.log(`  ${r.p[v.nome].toFixed(2)} | ${r.t.slice(0, 80)}`);
  console.log(`--- Laya ${v.nome}: 5 piores direto perdidos (pDireto mais baixo em pergunta que É direto) ---`);
  for (const r of [...pos].sort((a, b) => a.p[v.nome] - b.p[v.nome]).slice(0, 5))
    console.log(`  ${r.p[v.nome].toFixed(2)} | ${r.t.slice(0, 80)}`);
}

const csv = [
  'texto,gabarito_direto,regra,pDireto_A,pDireto_B,pDireto_C',
  ...res.map((r) => `"${r.t.replace(/"/g, '""')}",${r.gold},${r.regra},${r.p.A},${r.p.B},${r.p.C}`),
];
fs.writeFileSync('comparacao-laya.csv', csv.join('\n'));
console.log('\nGravado: comparacao-laya.csv');
