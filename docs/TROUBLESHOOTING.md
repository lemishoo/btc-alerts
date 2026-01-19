# Troubleshooting

## No Telegram messages
- Check .env variables loaded by run.sh (source .env)
- Tail run.log, search for telegram errors

## Duplicate alerts.py processes
- stop.sh kills pidfile and pkill -f alerts.py
- verify with ps aux

## Paper report not sent
- journalctl --user -u paper-report.service
- run manually: systemctl --user start paper-report.service
