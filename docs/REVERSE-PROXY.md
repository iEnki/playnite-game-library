# Reverse Proxy / eigene Domain

Beispiel mit **Nginx Proxy Manager** (NPM) für `games.example.com`:

1. NPM → *Hosts* → *Proxy Hosts* → *Add Proxy Host*
   - *Domain Names*: `games.example.com`
   - *Scheme*: `http` · *Forward Hostname/IP*: IP des Docker-Hosts · *Forward Port*: `8095`
   - *Block Common Exploits*: an · *Websockets Support*: nicht nötig
2. Tab *SSL*: Let's-Encrypt-Zertifikat anfordern, *Force SSL* und *HTTP/2* aktivieren.
3. Tab *Advanced* → *Custom Nginx Configuration* (wichtig für den Import großer Exporte):
   ```nginx
   client_max_body_size 2g;
   proxy_request_buffering off;
   proxy_read_timeout 900s;
   proxy_send_timeout 900s;
   ```
4. DNS-Eintrag (A/CNAME) für die Domain auf deine öffentliche IP bzw. den NPM-Host zeigen lassen.

## Sicherheit

- Lesen der Seite ist ohne Login möglich. Alle ändernden Funktionen (Import, KI-Erzeugung, KI-Einstellungen) erfordern das `UPDATE_TOKEN`.
- Wer die Sammlung nicht öffentlich zeigen möchte: in NPM eine **Access List** (Basic Auth und/oder IP-Bereich) auf den Proxy Host legen, oder die Seite nur im LAN/VPN betreiben.
- Ein langes, zufälliges Token verwenden (`openssl rand -base64 24`) und nicht ins Repo committen.

## Andere Proxys

Traefik, Caddy oder ein eigener nginx funktionieren genauso: alles an `http://<docker-host>:8095` weiterleiten und das Upload-Limit erhöhen (Caddy: kein Limit nötig; Traefik: `buffering`-Middleware nicht aktivieren oder `maxRequestBodyBytes` ausreichend groß setzen).
