 O OpenAI serve como um "orquestrador de sessões + formatador", não como fonte de conhecimento. Aqui está o breakdown:

O que o NotebookLM faz
100% do RAG — busca real nos manuais do sistema
Retorna o conteúdo bruto/relevante para a pergunta


O que o OpenAI faz
Isolamento de sessão por thread — o sessions[thread_id] garante que N usuários simultâneos não se contaminam, pois o NotebookLM é stateless e não tem conceito de sessão
Ajuste de linguagem — formata e corrige a concordância do texto extraído
SYSTEM_PROMPT — aplica persona e guardrails (não inventar respostas)
Por que HISTORY_LIMIT = 0 é necessário
Um único notebook compartilhado entre múltiplos usuários simultâneos não pode receber histórico — causaria contaminação entre sessões. O histórico fica isolado no backend por UUID e injetado apenas no OpenAI.

IMPORTANT

Atenção colateral: Com HISTORY_LIMIT = 0, o histórico existe no backend mas não é enviado ao OpenAI. Na prática, cada pergunta é independente da anterior, mesmo dentro da mesma thread. O usuário não consegue fazer perguntas de acompanhamento como "pode detalhar o passo 2?". Se quiser suportar isso, precisaria aumentar para HISTORY_LIMIT = 10 (5 turnos).


É a técnica chamada Query Rewriting / Contextual Query Expansion — um padrão clássico de RAG avançado




scp ~/.notebooklm/storage_state.json postgres@192.168.50.21:. 


python3.11 -m venv venv
source venv/bin/activate
pip install "notebooklm-py[browser]"
python -m playwright install-deps chromium
python -m playwright install chromium
which notebooklm
/home/arpa/agente-suporte-notebooklm/venv/bin/notebooklm

mkdir -p ~/.notebooklm
mv /home/postgres/storage_state.json ~/.notebooklm/



source venv/bin/activate
cd backend
export $(cat .env | xargs)
uvicorn main:app --host 0.0.0.0 --port 8000



journalctl -u agente-suporte -n 50 


kill -9 $(lsof -t -i :8000)
# Adicionar (use o python do venv, não do sistema):
0 6 * * * /opt/erp-agent/venv/bin/python /opt/erp-agent/auth_manager.py \
    --mac-host 192.168.1.50 \
    --mac-user seunome \
    >> /var/log/notebooklm_auth.log 2>&1