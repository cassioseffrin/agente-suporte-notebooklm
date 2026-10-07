// Uso: LAYA_KEY=sua_chave node test-laya.mjs   (Node 18+)
// Compara o prompt atual (A) com o novo (B) e mostra o melhor limiar para
// mandar para "resposta direta" sem perder perguntas de manual.

const URL = process.env.LAYA_URL ?? 'http://192.168.50.135:8007/v1/systemone';
const KEY = process.env.LAYA_KEY;
if (!KEY) throw new Error('Defina LAYA_KEY');

const MANUAL = 'notebook_manual_sistema';
const DIRETO = 'nao_buscar_buscar_notebook';

// ---------- Casos de teste ----------
// m = deve consultar o manual (RAG) | d = resposta direta, sem RAG
const reais = [
  'como cancelar nota fiscal emitida ha mais de 1 dia',
  'B OM DIA GOSTARFIA DE SABER QUAL A OBÇAO QUE EU VOU PARA VER O QUE EU VENDI DE FIADO PARA MEUS CLIENTES DURANTE O MES  01 ATE 30',
  'qual é o cfp que devo colocar nas configuraçõe do sistema para autorizar baixar o xml, já ccoloquei o cnpj da emprera eo meu cfp e fica com invalido',
  'preciso fazer uma nota fiscal eletrônica de remessa, então essa nota não é de venda não tem ligação com um pedido. Qual o menu que preciso acessar para emitir essa nota',
  'nota de complemento de tributo',
  'Como gerar os XMLs do mês?',
  'Importar nota de compra',
  'ALTERAR ALIQUOTA DE ICMS POR CFOP',
  'como lançar nota fiscal de serviço passo a passo',
  'ALIQUOTA ICMS',
  'Como emitir um MDF-e?',
  'Qual é o prazo legal para cancelar uma NFC-e?',
  'Ajuste de estoque',
  'Como emitir nota de complemento de ICMS?',
  'Devolução de venda, como realizar?',
  'Carta de correção',
  'como ohar o faturamrnto do mes',
  'Como cancelar uma venda no pdv e fazer ela outra vez',
  'como criar ou gerar etiquetas',
  'como importar xmls de entrada no sistema através de arquivos?',
  'Finalizei uma venda no pdv mais a forma de pagamento está errado como é altero essa forma de pagamento',
  'Como alterar forma de pagamento quitada',
  'Como faço um relatório de extrato das contas bancarias?',
  'como cadastrar as alíquotas na tabela de redução de ICMS',
  'o que significa CFOP não cadastrado',
  'como lançar condicionais pelo sistema?',
  'porque esta bloqueado o campo Cliente no Pedido de Venda? ou seja, não consigo iniciar a venda, estou logado inclusive como administrador',
  'relatorio de ultima compra e data que foi comprado por cliente',
  'COMO DAR BAIXA EM CHEQUE',
  'Como utilizar o módulo de aniversariantes?',
  'qual link para baixar  instalador em 32bits',
  'Como gerar sped fiscal?',
  'ATENCAO: NO PROCESSO DE EMISSAO DO DOCUMENTO FISCAL 2829 SERIA 1 LOTE 3 E PEIDDO 2, FORAM  ENCONTRADAS AS SEGUINTES INCONSISTENCIAS REJEIICAO CPF AUTOTIZADO PARA DOWNLOAD INVALIDO',
  'é possível atualizar diretamente pelo programa os códigos NCM?',
  'preciso mudar todos produtos para o CFOP 5102 como fazer isso em lote',
  'Como funciona o pós-venda no sistema?',
  'o cliente e um bar e compra coxa e sobrecoxa congelada para vender no bar e esta registrando o cfop 1101. qual o cfop que seria o correto',
  'onde eu cadastro cfop',
  'vou emitir minha primeira nota no control, eu ja usava outro sistema. Como alterar a sequencia de numeração da nfe',
  'no cadastro de produto, quando vou finalizar aparece uma mensagem assim: TABELA DE SUBSTIIÇÃO TRIBUTARIA NÃO ENCONTRADA POR FAVOR VERIFICAR',
].map((t) => ({ t, esperado: 'm' }));

// Só "quero explicação" veio das suas perguntas reais como vaga.
// Os demais "diretos" abaixo são EXEMPLOS INVENTADOS: troque por perguntas reais suas.
const diretos = [
  'quero explicação',
  'qual produto tem maior valor',
  'qual o produto mais vendido',
  'quanto faturei no mês',
  'quantos clientes tenho',
  'qual cliente mais comprou',
  'estoque do produto arroz',
  'total de contas a receber',
  'oi',
  'obrigado',
  'me ajuda',
  'ogi9',
  'asdf',
  'teste',
].map((t) => ({ t, esperado: 'd' }));

const casos = [...reais, ...diretos];

// ---------- Variantes de prompt ----------
const A = {
  instructions:
    'Classifique a pergunta do usuário. Se ela pede um DADO, número, lista ou ranking do negócio do cliente, escolha nao_buscar_buscar_notebook. Só escolha notebook_manual_sistema se a pergunta pede COMO FAZER algo no sistema.',
  criteria: {
    [MANUAL]:
      'Pergunta de COMO FAZER uma ação ou configuração no sistema, em formato de passo a passo. Começa com como, onde fica, o que significa o campo, como configuro. Nunca pede números, valores ou rankings.',
    [DIRETO]:
      'Pergunta sobre os DADOS do cliente, que exige consultar o banco de dados e não o manual: números, valores, listas, rankings, totais, buscas. Também perguntas genéricas ou abertas, palavras soltas sem sentido e assuntos fora do ERP.',
  },
};

// B: o padrão é MANUAL (é a maioria real das perguntas). "Direto" é uma classe estreita.
const B = {
  instructions:
    'A maioria das perguntas é dúvida sobre o sistema e deve consultar o manual. Escolha nao_buscar_buscar_notebook SOMENTE quando a pessoa pede o VALOR de um dado do negócio (um número, total, ranking ou lista), ou quando a mensagem não tem assunto nenhum.',
  criteria: {
    [MANUAL]:
      'Dúvida sobre o sistema ERP, fiscal ou contábil: como fazer, onde fica o menu, o que significa um erro ou rejeição, qual configuração ou CFOP usar, como emitir, cancelar, importar, cadastrar, alterar, gerar relatório. Inclui assunto solto de módulo ou tema fiscal (aliquota icms, carta de correção, ajuste de estoque, nota de complemento, sped, xml, ncm, mdfe), mensagens de erro coladas e perguntas sobre onde ver ou como gerar um relatório.',
    [DIRETO]:
      'Pedido do VALOR de um dado do negócio, para consultar o banco de dados: qual produto mais vendido, qual produto tem maior valor, quanto faturei no mês, quantos clientes tenho, estoque do produto X, total a receber. Ou mensagem sem assunto: saudação (oi, obrigado), pedido vago (quero explicação, me ajuda) e texto sem sentido (asdf, ogi9, teste).',
  },
};

async function classificar(texto, v) {
  const r = await fetch(URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${KEY}` },
    body: JSON.stringify({
      state: { document: texto },
      model: 'multilingual',
      lang: 'pt',
      questions: { destino: { type: 'choice', ...v } },
    }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const j = await r.json();
  const p = j.answers.destino.probabilities;
  return { pDireto: p[DIRETO] ?? 0, pManual: p[MANUAL] ?? 0 };
}

async function rodar(nome, v) {
  const res = [];
  for (const c of casos) res.push({ ...c, ...(await classificar(c.t, v)) });

  const pelaEscolha = res.filter((x) => (x.pDireto > x.pManual ? 'd' : 'm') !== x.esperado);
  console.log(`\n===== Prompt ${nome} =====`);
  console.log(`Acertos pela escolha do modelo: ${res.length - pelaEscolha.length}/${res.length}`);
  for (const x of pelaEscolha)
    console.log(`  erro: esperado=${x.esperado} pDireto=${x.pDireto.toFixed(2)} | ${x.t.slice(0, 70)}`);

  console.log('\nLimiar para ir DIRETO (pDireto >= t)  |  manual perdido  |  direto pego  |  RAG evitado');
  const nM = res.filter((x) => x.esperado === 'm').length;
  const nD = res.length - nM;
  for (const t of [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]) {
    const vaoDireto = res.filter((x) => x.pDireto >= t);
    const manualPerdido = vaoDireto.filter((x) => x.esperado === 'm').length;
    const diretoPego = vaoDireto.filter((x) => x.esperado === 'd').length;
    console.log(
      `  t=${t.toFixed(1)}  |  ${manualPerdido}/${nM}  |  ${diretoPego}/${nD}  |  ${((diretoPego / res.length) * 100).toFixed(0)}% das chamadas`,
    );
  }
}

await rodar('A (atual)', A);
await rodar('B (novo)', B);
