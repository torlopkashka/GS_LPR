#!/bin/sh
# Установка агента на Linux (Debian/Ubuntu/Raspberry Pi OS). Запускать от root.
set -e
DIR=/opt/gate-agent
apt-get update && apt-get install -y python3 python3-venv
id gateagent >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -G dialout,plugdev gateagent
mkdir -p "$DIR"
cp gate_agent.py requirements.txt gate-agent.service "$DIR"/
[ -f "$DIR/agent.yaml" ] || cp agent.example.yaml "$DIR/agent.yaml"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"
# Доступ к USB HID-реле без root
cat > /etc/udev/rules.d/50-usb-relay.rules <<'RULES'
SUBSYSTEM=="usb", ATTR{idVendor}=="16c0", ATTR{idProduct}=="05df", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="16c0", ATTRS{idProduct}=="05df", MODE="0660", GROUP="plugdev"
RULES
udevadm control --reload-rules || true
chown -R gateagent "$DIR"
chmod 600 "$DIR/agent.yaml"
cp "$DIR/gate-agent.service" /etc/systemd/system/
systemctl daemon-reload
echo
echo "Отредактируйте $DIR/agent.yaml, проверьте реле:"
echo "  sudo -u gateagent $DIR/venv/bin/python $DIR/gate_agent.py -c $DIR/agent.yaml --test"
echo "и запустите службу:"
echo "  systemctl enable --now gate-agent && journalctl -u gate-agent -f"
