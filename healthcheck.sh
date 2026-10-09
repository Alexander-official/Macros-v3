#!/bin/bash
while true; do
    sleep 30
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 10 http://127.0.0.1:8080/healthcheck)
    if [ "$HTTP_CODE" != "200" ]; then
        echo "$(date): Sistem yanit vermiyor. Macros2 olduruluyor..." >> "$(dirname "$0")/healthcheck.log"
        systemctl kill -s SIGKILL macros2
        sleep 2
        systemctl start macros2
        sleep 15
    fi
done
