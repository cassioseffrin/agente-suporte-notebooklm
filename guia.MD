
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



# Adicionar (use o python do venv, não do sistema):
0 6 * * * /opt/erp-agent/venv/bin/python /opt/erp-agent/auth_manager.py \
    --mac-host 192.168.1.50 \
    --mac-user seunome \
    >> /var/log/notebooklm_auth.log 2>&1