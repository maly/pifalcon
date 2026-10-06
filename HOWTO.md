# Raspberry Pi jako síťový bridge pro LightBurn

Tento návod je určený pro Raspberry Pi OS Lite bez desktopu. Po dokončení bude Raspberry Pi poskytovat:

- laserový TCP → serial bridge na portu `23`;
- MJPEG stream USB kamery na portu `8080`;
- webové nastavení V4L2 parametrů kamery na portu `8081`.

V příkladech má Raspberry Pi adresu `192.168.0.99`. Nahraďte ji skutečnou adresou svého zařízení. Stejně tak vždy nejprve zjistěte vlastní stabilní cesty laseru a kamery; názvy zařízení uvedené v příkazech nejsou předpokládány.

## Co udělat

1. Připojit Raspberry Pi k LAN, ideálně Ethernetem, a přihlásit se přes SSH.
2. Aktualizovat Raspberry Pi OS Lite a nainstalovat `ser2net`, `ustreamer`, V4L2 nástroje, `nftables`, Flask a Gunicorn.
3. Připojit laser a kameru a zjistit jejich stabilní cesty v `/dev/serial/by-id/` a `/dev/v4l/by-id/`.
4. Vytvořit `laser-bridge.service`, který zpřístupní GRBL sériový port na TCP portu `23` (běží pod neprivilegovaným uživatelem, druhé spojení odmítne).
5. Vytvořit `camera-stream.service`, který zpřístupní existující UVC kameru jako MJPEG stream na portu `8080` (také bez roota).
6. Zkopírovat adresář `camera` do `/opt/camera-controls`, doplnit zjištěnou cestu kamery a aktivovat `camera-controls.service` na portu `8081`.
7. **Povinně** omezit přístup k portům `22`, `23`, `8080` a `8081` firewallem (`nftables`) jen na vlastní LAN.
8. Ověřit služby, naslouchající porty, firewall, neprivilegované uživatele služeb, HTTP endpointy a komunikaci s laserem.
9. V routeru je vhodné vytvořit DHCP rezervaci, například `192.168.0.99`. Porty `23`, `8080` a `8081` nepřesměrovávat do internetu.

## Jak jsem postupoval

### 1. Připojení a instalace balíčků

Z klientského počítače se připojte pomocí hostname nebo aktuální IP adresy Raspberry Pi:

```powershell
ssh pi@192.168.0.99
```

Aktualizujte systém a nainstalujte potřebné balíčky:

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ser2net ustreamer v4l-utils usbutils curl netcat-openbsd nftables
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-flask python3-pytest python3-gunicorn
sudo systemctl reboot
```

Po restartu se znovu připojte přes SSH.

### 2. Zjištění zařízení

Připojte laser i USB UVC kameru a vypište dostupná zařízení:

```bash
lsusb
ls -l /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/* 2>/dev/null
v4l2-ctl --list-devices
find /dev/v4l/by-id -maxdepth 1 -type l -ls 2>/dev/null
```

Pro laser preferujte cestu `/dev/serial/by-id/...`; pro kameru cestu končící `-video-index0` v `/dev/v4l/by-id/...`. Uložte si skutečné hodnoty do proměnných jen pro pohodlné ověření:

```bash
LASER_DEVICE=/dev/serial/by-id/SEM_DOPLNTE_SKUTECNOU_CESTU
CAMERA_DEVICE=/dev/v4l/by-id/SEM_DOPLNTE_SKUTECNOU_CESTU-video-index0
readlink -f "$LASER_DEVICE"
readlink -f "$CAMERA_DEVICE"
```

Zjistěte formáty, rozlišení, FPS a dostupné ovládací prvky konkrétní kamery:

```bash
v4l2-ctl --device="$CAMERA_DEVICE" --list-formats-ext
v4l2-ctl --device="$CAMERA_DEVICE" --list-ctrls-menus
v4l2-ctl --device="$CAMERA_DEVICE" --all
```

Pro běžný GRBL/Falcon řadič se používá `115200` baud, 8 datových bitů, bez parity a jeden stop bit. Pokud dokumentace řadiče uvádí jinou rychlost, použijte její hodnotu.

### 3. Laserový TCP bridge

Vypněte distribuční unit. Program `ser2net` bude spouštět vlastní služba, takže nesmí současně běžet druhá instance:

```bash
sudo systemctl disable --now ser2net.service
```

Služba `laser-bridge` běží pod dočasným neprivilegovaným uživatelem (`DynamicUser=yes`) jen s přístupem ke skupině `dialout` (sériové porty) a s jedinou capability `CAP_NET_BIND_SERVICE` pro port `23`. Konfiguraci `ser2net` proto negeneruje do `/etc/ser2net.yaml`, ale do `/run/laser-bridge/ser2net.yaml` (adresář vytváří systemd přes `RuntimeDirectory=`). Distribuční `/etc/ser2net.yaml` zůstává nedotčený.

> Aktualizujete-li instalaci podle starší verze tohoto návodu, kde skript přepisoval `/etc/ser2net.yaml` a běžel jako root: nahraďte skript i unit níže uvedenými verzemi, případně obnovte původní konfiguraci (`sudo mv /etc/ser2net.yaml.pre-lightburn /etc/ser2net.yaml`), smažte starý PID soubor (`sudo rm -f /run/laser-bridge.pid`) a spusťte `sudo systemctl daemon-reload && sudo systemctl restart laser-bridge.service`.

Do `/etc/default/laser-bridge` vložte skutečnou stabilní cestu laseru:

```bash
sudoedit /etc/default/laser-bridge
```

```bash
LASER_DEVICE=/dev/serial/by-id/SEM_DOPLNTE_SKUTECNOU_CESTU
LASER_BAUD=115200
LASER_PORT=23
LISTEN_ADDRESS=0.0.0.0
WAIT_SECONDS=5
```

Vytvořte `/usr/local/sbin/laser-bridge`:

```bash
sudoedit /usr/local/sbin/laser-bridge
```

```sh
#!/bin/sh
set -eu

DEFAULTS=/etc/default/laser-bridge
LASER_DEVICE=auto
LASER_BAUD=115200
LASER_PORT=23
LISTEN_ADDRESS=0.0.0.0
WAIT_SECONDS=5

[ ! -r "$DEFAULTS" ] || . "$DEFAULTS"

# Adresář vytváří systemd (RuntimeDirectory=laser-bridge) a předává ho v $RUNTIME_DIRECTORY.
RUNTIME_DIR="${RUNTIME_DIRECTORY:-/run/laser-bridge}"
CONFIG="$RUNTIME_DIR/ser2net.yaml"
PIDFILE="$RUNTIME_DIR/ser2net.pid"

find_laser_device() {
    if [ "$LASER_DEVICE" != auto ]; then
        [ -e "$LASER_DEVICE" ] && printf '%s\n' "$LASER_DEVICE" && return 0
        return 1
    fi
    for candidate in /dev/serial/by-id/* /dev/ttyACM* /dev/ttyUSB*; do
        [ -e "$candidate" ] && printf '%s\n' "$candidate" && return 0
    done
    return 1
}

device=
while [ -z "$device" ]; do
    device="$(find_laser_device || true)"
    [ -n "$device" ] || { echo "Laser není připojen; čekám ${WAIT_SECONDS} s"; sleep "$WAIT_SECONDS"; }
done

umask 077
tmp_config="$(mktemp "$RUNTIME_DIR/ser2net.yaml.XXXXXX")"
trap 'rm -f "$tmp_config"' EXIT HUP INT TERM
{
    printf '%%YAML 1.1\n---\n'
    printf 'connection: &laser\n'
    printf '    accepter: tcp,%s,%s\n' "$LISTEN_ADDRESS" "$LASER_PORT"
    printf '    enable: on\n'
    printf '    options:\n'
    printf '      kickolduser: false\n'
    printf '    connector: serialdev,\n'
    printf '              %s,\n' "$device"
    printf '              %sn81,local\n' "$LASER_BAUD"
} > "$tmp_config"
mv -f "$tmp_config" "$CONFIG"
trap - EXIT HUP INT TERM

exec /usr/sbin/ser2net -n -c "$CONFIG" -P "$PIDFILE"
```

Volba `kickolduser: false` (výchozí chování `ser2net`) znamená, že dokud je k laseru připojený jeden klient (typicky LightBurn), každé další TCP spojení na port `23` je okamžitě odmítnuto a běžící spojení zůstává nedotčené. Původní `kickolduser: true` by naopak běžícího klienta bez varování odpojilo – i uprostřed gravírování – kdykoli by se připojil kdokoli jiný (nebo omylem druhá instance LightBurnu).

Pokud spojení „uvízne“ (např. klientský počítač spadl nebo se odpojil od sítě, aniž by TCP spojení korektně ukončil) a LightBurn se nemůže znovu připojit, ověřte, že k laseru nikdo jiný připojený není, a službu restartujte – tím se všechna spojení ukončí:

```bash
sudo ss -tnp state established '( sport = :23 )'
sudo systemctl restart laser-bridge.service
```

`ser2net` zamyká sériový port UUCP zámkem v `/run/lock` (na Raspberry Pi OS je to `tmpfs` s právy `1777`), proto unit níže tento jediný adresář zpřístupňuje pro zápis (`ReadWritePaths=/run/lock`), přestože jinak je celý systém pro službu jen ke čtení (`ProtectSystem=strict`). Pokud by journal služby hlásil `Error accessing locks` nebo `Port in use`, ověřte, že laser nepoužívá jiný program, a smažte osiřelý zámek `sudo rm -f /run/lock/LCK..ttyUSB0` (resp. `LCK..ttyACM0`).

Nastavte spustitelnost:

```bash
sudo chmod 0755 /usr/local/sbin/laser-bridge
```

Vytvořte `/etc/systemd/system/laser-bridge.service`:

```ini
[Unit]
Description=LightBurn laser serial-to-TCP bridge
Documentation=man:ser2net(8)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStart=/usr/local/sbin/laser-bridge
Restart=always
RestartSec=3
# ser2net (síťová služba bez autentizace) neběží jako root:
DynamicUser=yes
SupplementaryGroups=dialout
RuntimeDirectory=laser-bridge
RuntimeDirectoryMode=0750
# Port 23 je < 1024, proto jediná povolená capability:
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
# Kromě RuntimeDirectory jediná zapisovatelná cesta: UUCP zámek sériového portu (/run/lock/LCK..ttyUSB0)
ReadWritePaths=/run/lock
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes

[Install]
WantedBy=multi-user.target
```

`PrivateDevices=` nenastavujte – služba potřebuje přístup ke skutečnému `/dev/ttyUSB*`/`/dev/ttyACM*`. Aktivujte službu:

```bash
sudo systemd-analyze verify /etc/systemd/system/laser-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now laser-bridge.service
```

V LightBurnu potom použijte host `192.168.0.99` a port `23`.

### 4. MJPEG stream kamery

Do `/etc/default/camera-stream` vložte skutečnou stabilní cestu kamery:

```bash
sudoedit /etc/default/camera-stream
```

```bash
CAMERA_DEVICE=/dev/v4l/by-id/SEM_DOPLNTE_SKUTECNOU_CESTU-video-index0
CAMERA_RESOLUTION=auto
CAMERA_FORMAT=auto
CAMERA_FPS=15
CAMERA_PORT=8080
LISTEN_ADDRESS=0.0.0.0
WAIT_SECONDS=5
```

Vytvořte `/usr/local/sbin/camera-stream`. Wrapper vyhledá USB zařízení s capability `Video Capture`, preferuje `/dev/v4l/by-id/*-video-index0` a zvolí největší dostupný MJPEG režim nejvýše 1920×1080:

```bash
sudoedit /usr/local/sbin/camera-stream
```

```sh
#!/bin/sh
set -eu

DEFAULTS=/etc/default/camera-stream
CAMERA_DEVICE=auto
CAMERA_RESOLUTION=auto
CAMERA_FORMAT=auto
CAMERA_FPS=15
CAMERA_PORT=8080
LISTEN_ADDRESS=0.0.0.0
WAIT_SECONDS=5

[ ! -r "$DEFAULTS" ] || . "$DEFAULTS"

is_usb_capture_device() {
    candidate="$1"
    udevadm info --query=property --name="$candidate" 2>/dev/null | grep -q '^ID_BUS=usb$' || return 1
    v4l2-ctl --device="$candidate" --all 2>/dev/null | grep -q 'Video Capture' || return 1
}

find_camera_device() {
    if [ "$CAMERA_DEVICE" != auto ]; then
        [ -e "$CAMERA_DEVICE" ] && printf '%s\n' "$CAMERA_DEVICE" && return 0
        return 1
    fi
    for candidate in /dev/v4l/by-id/*-video-index0; do
        [ -e "$candidate" ] && is_usb_capture_device "$candidate" && printf '%s\n' "$candidate" && return 0
    done
    for candidate in /dev/video*; do
        [ -e "$candidate" ] && is_usb_capture_device "$candidate" && printf '%s\n' "$candidate" && return 0
    done
    return 1
}

device=
while [ -z "$device" ]; do
    device="$(find_camera_device || true)"
    [ -n "$device" ] || { echo "USB kamera není připojena; čekám ${WAIT_SECONDS} s"; sleep "$WAIT_SECONDS"; }
done

modes="$(v4l2-ctl --device="$device" --list-formats-ext)"
printf '%s\n' "$modes"

best_mode_for_format() {
    requested_format="$1"
    printf '%s\n' "$modes" | awk -v fmt="$requested_format" '
        /^[[:space:]]*\[[0-9]+\]:/ { active = index($0, "\047" fmt "\047") > 0 }
        active && /Size: Discrete/ {
            split($3, dimensions, "x")
            if (dimensions[1] <= 1920 && dimensions[2] <= 1080) {
                print dimensions[1] * dimensions[2], $3
            }
        }
    ' | sort -nr | sed -n '1s/^[0-9][0-9]* //p'
}

format="$CAMERA_FORMAT"
resolution="$CAMERA_RESOLUTION"
if [ "$format" = auto ]; then
    resolution="$(best_mode_for_format MJPG)"
    if [ -n "$resolution" ]; then
        format=MJPEG
    else
        resolution="$(best_mode_for_format JPEG)"
        if [ -n "$resolution" ]; then
            format=JPEG
        else
            format=YUYV
            resolution="$(best_mode_for_format YUYV)"
        fi
    fi
fi

if [ "$resolution" = auto ] || [ -z "$resolution" ]; then
    [ "$format" = YUYV ] && resolution=640x480 || resolution=1280x720
fi

encoder=CPU
if [ "$format" = MJPEG ] || [ "$format" = JPEG ]; then
    encoder=HW
fi

exec /usr/bin/ustreamer \
    --device "$device" \
    --resolution "$resolution" \
    --format "$format" \
    --desired-fps "$CAMERA_FPS" \
    --encoder "$encoder" \
    --host "$LISTEN_ADDRESS" \
    --port "$CAMERA_PORT" \
    --buffers 3 \
    --workers 2 \
    --persistent \
    --device-timeout 5 \
    --device-error-delay 3
```

Nastavte spustitelnost:

```bash
sudo chmod 0755 /usr/local/sbin/camera-stream
```

Vytvořte `/etc/systemd/system/camera-stream.service`:

```ini
[Unit]
Description=LightBurn USB camera MJPEG stream
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStart=/usr/local/sbin/camera-stream
Restart=always
RestartSec=3
# ustreamer neběží jako root; přístup ke kameře přes skupinu video:
DynamicUser=yes
SupplementaryGroups=video
CapabilityBoundingSet=
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes

[Install]
WantedBy=multi-user.target
```

Port `8080` je nad 1024, takže služba nepotřebuje žádnou capability. Aktivujte službu:

```bash
sudo systemd-analyze verify /etc/systemd/system/camera-stream.service
sudo systemctl daemon-reload
sudo systemctl enable --now camera-stream.service
```

Stream bude dostupný na:

```text
http://192.168.0.99:8080/stream
```

Na Raspberry Pi 3 je vhodné začít s 1920×1080 MJPEG při 10–15 FPS. Jestli kamera takový režim nepodporuje nebo je provoz nestabilní, snižte rozlišení na 1280×720 nebo FPS.

### 5. Webové ovládání parametrů kamery

Adresář [`camera`](camera/) leží vedle tohoto dokumentu. Z klientského počítače jej zkopírujte na Raspberry Pi:

```powershell
scp -r .\camera pi@192.168.0.99:/tmp/camera-controls
```

Na Raspberry Pi vytvořte systémového uživatele a nainstalujte aplikaci:

```bash
sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin camera-controls
sudo install -d -o root -g root -m 0755 /opt/camera-controls /opt/camera-controls/templates /opt/camera-controls/static /opt/camera-controls/tests
sudo install -o root -g root -m 0644 /tmp/camera-controls/camera_controls.py /opt/camera-controls/camera_controls.py
sudo install -o root -g root -m 0644 /tmp/camera-controls/templates/index.html /opt/camera-controls/templates/index.html
sudo install -o root -g root -m 0644 /tmp/camera-controls/static/app.js /opt/camera-controls/static/app.js
sudo install -o root -g root -m 0644 /tmp/camera-controls/static/style.css /opt/camera-controls/static/style.css
sudo install -o root -g root -m 0644 /tmp/camera-controls/tests/test_camera_controls.py /opt/camera-controls/tests/test_camera_controls.py
sudo install -o root -g root -m 0644 /tmp/camera-controls/camera-controls.service /etc/systemd/system/camera-controls.service
sudo install -o root -g root -m 0644 /tmp/camera-controls/camera-controls.default /etc/default/camera-controls
```

Upravte `/etc/default/camera-controls`. Znovu použijte skutečnou stabilní cestu kamery:

```bash
sudoedit /etc/default/camera-controls
```

```bash
CAMERA_DEVICE=/dev/v4l/by-id/SEM_DOPLNTE_SKUTECNOU_CESTU-video-index0
CAMERA_STREAM_URL=http://192.168.0.99:8080/stream
ALLOWED_NETWORKS=127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
# Volitelné: povolené hodnoty hlavičky Host (ochrana proti DNS rebindingu).
# Výchozí: hostitel z CAMERA_STREAM_URL, localhost a 127.0.0.1; IP adresy z ALLOWED_NETWORKS se akceptují vždy.
#ALLOWED_HOSTS=gravipi.local,localhost,127.0.0.1
```

Aplikace dynamicky načítá jen controls, které kamera skutečně podporuje. Číselné hodnoty zobrazuje jako slidery, boolean hodnoty jako checkboxy a V4L2 menu jako výběrové seznamy. Změny provádí přes `v4l2-ctl` a po každé změně znovu načte skutečný stav kamery.

Před spuštěním ověřte testy a systemd unit:

```bash
cd /opt/camera-controls
sudo runuser -u camera-controls -- python3 -m pytest -q -p no:cacheprovider
sudo systemd-analyze verify /etc/systemd/system/camera-controls.service
sudo systemctl daemon-reload
sudo systemctl enable --now camera-controls.service
```

Webové rozhraní bude dostupné na:

```text
http://192.168.0.99:8081/
```

### 6. Síť

Preferujte kabelový Ethernet. Pokud Raspberry Pi současně používá Ethernet i Wi-Fi pod správou NetworkManageru, zjistěte názvy profilů a Ethernetu nastavte nižší route metric:

```bash
nmcli connection show
sudo nmcli connection modify "NAZEV_ETHERNET_PROFILU" ipv4.route-metric 100 ipv6.route-metric 100
sudo nmcli connection modify "NAZEV_WIFI_PROFILU" ipv4.route-metric 600 ipv6.route-metric 600
```

Vlastní aplikace omezuje přístup k ovládání na loopback a privátní LAN rozsahy přes `ALLOWED_NETWORKS`, kontroluje hlavičku `Host` (`ALLOWED_HOSTS`) a u měnících požadavků hlavičku `Origin` (ochrana proti CSRF a DNS rebindingu). Laserový bridge (`ser2net`) ani MJPEG stream (`ustreamer`) žádnou kontrolu klientů nemají, proto je následující firewall povinný. Na routeru nevytvářejte port forwarding.

### 7. Omezení přístupu k portům (povinné)

Surový GRBL port `23` umí zapnout laser, pohybovat s ním i měnit `$`-nastavení řadiče. Omezení přímo na Pi nezávisí na tom, jak je nastavený router, a chrání i před hosty na Wi-Fi, IoT zařízeními nebo VPN. Následující pravidla `nftables` povolí SSH (`22`), laser (`23`), stream (`8080`) a ovládání kamery (`8081`) jen z loopbacku a privátních LAN rozsahů; ostatní spojení na tyto porty zahodí. IPv4 rozsahy odpovídají výchozímu `ALLOWED_NETWORKS` webového ovládání (`127.0.0.0/8` pokrývá pravidlo `iif lo accept`), z IPv6 jsou povoleny jen link-local `fe80::/10` a ULA `fc00::/7`.

Vytvořte `/etc/nftables.d/pifalcon.nft`:

```bash
sudo install -d -o root -g root -m 0755 /etc/nftables.d
sudoedit /etc/nftables.d/pifalcon.nft
```

```nft
#!/usr/sbin/nft -f
# PiFalcon: SSH, laser (ser2net), MJPEG stream a ovládání kamery jen z vlastní LAN.
# IPv4 rozsahy drž v souladu s ALLOWED_NETWORKS v /etc/default/camera-controls.

# Opakované načtení souboru nahradí tabulku, místo aby pravidla zdvojilo.
table inet pifalcon
delete table inet pifalcon

table inet pifalcon {
    chain input {
        type filter hook input priority 0; policy accept;
        iif lo accept
        tcp dport { 22, 23, 8080, 8081 } ip saddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } accept
        tcp dport { 22, 23, 8080, 8081 } ip6 saddr { fe80::/10, fc00::/7 } accept
        tcp dport { 22, 23, 8080, 8081 } counter drop
    }
}
```

Pokud se k Pi připojujete přes SSH odjinud (např. VPN s rozsahem mimo uvedené sítě) nebo SSH běží na jiném portu, upravte rozsahy či porty **před** aktivací, jinak se k Pi přes SSH nepřipojíte. Upravíte-li rozsahy, upravte stejně i `ALLOWED_NETWORKS` v `/etc/default/camera-controls`.

Distribuční `/etc/nftables.conf` začíná `flush ruleset` a soubory z `/etc/nftables.d/` sám nenačítá. Doplňte do něj `include`, zkontrolujte syntaxi a firewall aktivujte (i po restartu):

```bash
grep -qxF 'include "/etc/nftables.d/*.nft"' /etc/nftables.conf || echo 'include "/etc/nftables.d/*.nft"' | sudo tee -a /etc/nftables.conf
sudo nft -c -f /etc/nftables.conf
sudo systemctl enable nftables.service
sudo systemctl restart nftables.service
sudo nft list table inet pifalcon
```

Již navázané SSH spojení z LAN zůstane zachováno. Používáte-li na Pi i jiný nástroj spravující firewall (Docker, `ufw`, `firewalld`), pamatujte, že `flush ruleset` v `/etc/nftables.conf` při restartu `nftables.service` smaže i jejich pravidla; v takovém případě vložte tabulku `pifalcon` do jejich konfigurace.

### 8. Ověření výsledku

Zkontrolujte automatický start a běh všech služeb:

```bash
systemctl is-enabled laser-bridge.service camera-stream.service camera-controls.service
systemctl is-active laser-bridge.service camera-stream.service camera-controls.service
sudo systemctl status laser-bridge.service
sudo systemctl status camera-stream.service
sudo systemctl status camera-controls.service
sudo ss -lntp | grep -E ':(23|8080|8081)[[:space:]]'
```

Ověřte, že firewall je aktivní a služby neběží jako root:

```bash
sudo nft list ruleset | grep -E 'dport'
systemctl show -p User,DynamicUser laser-bridge.service camera-stream.service camera-controls.service
ps -o user=,group=,supgrp=,cmd= -C ser2net,ustreamer
ls -l /run/laser-bridge/
```

Výpis `nft` musí obsahovat tři pravidla `tcp dport { 22, 23, 8080, 8081 }` (dvě `accept`, jedno `drop`). `laser-bridge` a `camera-stream` mají mít `DynamicUser=yes` a procesy `ser2net` a `ustreamer` nesmí běžet jako `root` (jejich uživatel se jmenuje stejně jako služba). V `/run/laser-bridge/` je vygenerovaný `ser2net.yaml`.

Ověřte HTTP rozhraní a kameru lokálně na Raspberry Pi:

```bash
curl -fsS --max-time 8 http://127.0.0.1:8081/api/controls
curl -fsS --max-time 10 -o /tmp/lightburn-camera-test.jpg http://127.0.0.1:8080/snapshot
curl -sS --max-time 2 -D - -o /dev/null http://127.0.0.1:8080/stream
file /tmp/lightburn-camera-test.jpg
```

Z jiného počítače v LAN ověřte port laseru a webová rozhraní:

```bash
nc -vz 192.168.0.99 23
curl -I http://192.168.0.99:8080/stream
curl -I http://192.168.0.99:8081/
```

Přímý dotaz na GRBL posílejte jen tehdy, když k laseru není současně připojen LightBurn. Dotaz `?` má vrátit stav řadiče a `$I` jeho identifikaci.

Ověřte chování při druhém spojení: připojte LightBurn k laseru a z jiného počítače v LAN zkuste druhé spojení. To musí být odmítnuto (`ser2net` vypíše hlášku o obsazeném portu a spojení ukončí) a LightBurn musí zůstat připojený a ovládat laser:

```bash
nc 192.168.0.99 23 </dev/null
```

Pokud máte možnost, ověřte i zařízení mimo povolené rozsahy (např. počítač za VPN nebo v jiné síti): `nc -vz -w 3 192.168.0.99 23` musí skončit timeoutem a čítač pravidla `drop` v `sudo nft list table inet pifalcon` se zvýší.

Nakonec Raspberry Pi restartujte a kontroly `is-enabled`, `is-active`, `ss`, `nft` a HTTP testy zopakujte:

```bash
sudo systemctl reboot
```

Logy jednotlivých služeb lze sledovat těmito příkazy:

```bash
sudo journalctl -u laser-bridge.service -f
sudo journalctl -u camera-stream.service -f
sudo journalctl -u camera-controls.service -f
```

Výsledná konfigurace pro LightBurn používá laser na `192.168.0.99:23` a kamerový stream `http://192.168.0.99:8080/stream`. Webové nastavení kamery je na `http://192.168.0.99:8081/`.
