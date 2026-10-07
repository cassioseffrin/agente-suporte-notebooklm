// Uso: node avaliar-log.mjs perguntas.txt [caminho/do/roteador.mjs]
// Lê o log (uma pergunta por linha; texto entre aspas pode ocupar várias linhas),
// roda o roteador e mostra: distribuição, TUDO que foi para "direto" (revise a mão:
// é o único erro caro) e, se houver gabarito, quanto do "direto" esperado foi pego.
// Grava saida-roteador.csv com destino, motivo e texto de cada pergunta.

import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';

const arquivo = process.argv[2];
if (!arquivo) throw new Error('Uso: node avaliar-log.mjs perguntas.txt [roteador.mjs]');
const { rotear, norm } = await import(pathToFileURL(path.resolve(process.argv[3] ?? './roteador.mjs')).href);

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

// Gabarito de "direto" montado por leitura do log (julgamento meu, não verdade absoluta).
// [categoria, texto normalizado, 'exato' | 'contem']
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

const perguntas = lerPerguntas(fs.readFileSync(arquivo, 'utf8'));
const res = perguntas.map((t) => ({ t, n: norm(t), ...rotear(t) }));

const cont = { manual: 0, direto: 0, indefinido: 0 };
for (const r of res) cont[r.destino]++;
console.log(`Perguntas lidas: ${res.length}`);
console.log(`  manual: ${cont.manual} | indefinido (vai pro RAG): ${cont.indefinido} | direto: ${cont.direto}`);
console.log(`  RAG evitado: ${((cont.direto / res.length) * 100).toFixed(1)}% das perguntas`);

const casa = (r, [, txt, modo]) => (modo === 'exato' ? r.n === txt : r.n.includes(txt));
const noGabarito = (r) => GABARITO.some((g) => casa(r, g));

console.log('\n--- Tudo que foi para DIRETO (revise: item sem [ok] pode ser erro caro) ---');
for (const r of res.filter((x) => x.destino === 'direto'))
  console.log(`  ${noGabarito(r) ? '[ok]' : '[??]'} (${r.motivo}) ${r.t.slice(0, 90)}`);

console.log('\n--- Gabarito: quanto do esperado foi para direto ---');
const porCat = {};
for (const g of GABARITO) {
  const achados = res.filter((r) => casa(r, g));
  if (!achados.length) continue;
  const c = (porCat[g[0]] ??= { total: 0, pegos: 0, perdidos: [] });
  for (const r of achados) {
    c.total++;
    if (r.destino === 'direto') c.pegos++;
    else c.perdidos.push(r.t.slice(0, 60));
  }
}
for (const [cat, c] of Object.entries(porCat)) {
  console.log(`  ${cat}: ${c.pegos}/${c.total}`);
  for (const p of [...new Set(c.perdidos)]) console.log(`      perdido: ${p}`);
}

const csv = ['destino,motivo,texto', ...res.map((r) => `${r.destino},${r.motivo},"${r.t.replace(/"/g, '""')}"`)];
fs.writeFileSync('saida-roteador.csv', csv.join('\n'));
console.log('\nGravado: saida-roteador.csv');
