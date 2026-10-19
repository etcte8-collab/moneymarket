## 2. start the service on 0.0.0.0:9100
systemctl --user daemon-reload && systemctl --user enable --now moneymarket-web
curl -s localhost:9100/healthz && curl -s localhost:9100/status | head -20

