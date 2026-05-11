#!/bin/bash

set -e

SERVICE_NAME="homelab-monitor"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Usuário que executou o script.
# Se o script for chamado com sudo, usa SUDO_USER.
# Se for chamado sem sudo, usa USER.
INSTALL_USER="${SUDO_USER:-$USER}"

# Grupo principal do usuário.
GROUP_NAME="$(id -gn "$INSTALL_USER")"

# Home real do usuário.
USER_HOME="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"

# Diretório onde este script está localizado.
# Mais seguro do que usar pwd, porque funciona mesmo se você chamar o script de outro diretório.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Arquivos do projeto.
SCRIPT_PATH="${PROJECT_DIR}/monitor.py"
INSTALL_SCRIPT="${PROJECT_DIR}/install_service.sh"

# Ambiente Python padrão.
VENV_DIR="${USER_HOME}/enviroment"
PYTHON_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"

echo "=== Instalando serviço: ${SERVICE_NAME} ==="
echo
echo "Usuário:       ${INSTALL_USER}"
echo "Grupo:         ${GROUP_NAME}"
echo "Home:          ${USER_HOME}"
echo "Projeto:       ${PROJECT_DIR}"
echo "Python venv:   ${PYTHON_BIN}"
echo "Script:        ${SCRIPT_PATH}"
echo

echo "=== Instalando dependências do sistema ==="
sudo apt update
sudo apt install -y python3-venv python3-pip lm-sensors wireless-tools iproute2

echo
echo "=== Verificando projeto ==="

if [ ! -f "$SCRIPT_PATH" ]; then
  echo "ERRO: monitor.py não encontrado em:"
  echo "$SCRIPT_PATH"
  exit 1
fi

echo
echo "=== Garantindo ambiente virtual Python ==="

if [ ! -f "$PYTHON_BIN" ]; then
  echo "Ambiente virtual não encontrado em:"
  echo "$VENV_DIR"
  echo
  echo "Criando ambiente virtual..."
  python3 -m venv "$VENV_DIR"
fi

if [ ! -f "$PIP_BIN" ]; then
  echo "ERRO: pip do ambiente não encontrado em:"
  echo "$PIP_BIN"
  exit 1
fi

echo
echo "=== Instalando dependências Python ==="
"$PIP_BIN" install --upgrade pip
"$PIP_BIN" install flask psutil requests

echo
echo "=== Garantindo permissão de execução do instalador ==="
chmod +x "$INSTALL_SCRIPT"

echo
echo "=== Criando arquivo systemd ==="
echo "$SERVICE_FILE"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Homelab Monitor Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${INSTALL_USER}
Group=${GROUP_NAME}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PYTHON_BIN} ${SCRIPT_PATH}

Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo
echo "=== Recarregando systemd ==="
sudo systemctl daemon-reload

echo
echo "=== Habilitando serviço para iniciar com o sistema ==="
sudo systemctl enable "${SERVICE_NAME}.service"

echo
echo "=== Reiniciando serviço ==="
sudo systemctl restart "${SERVICE_NAME}.service"

echo
echo "=== Status do serviço ==="
systemctl status "${SERVICE_NAME}.service" --no-pager

echo
echo "Instalação concluída."
echo
echo "Dashboard:"
echo "  http://127.0.0.1:8090"
echo "  http://192.168.68.125:8090"
echo
echo "Logs:"
echo "  journalctl -u ${SERVICE_NAME}.service -f"