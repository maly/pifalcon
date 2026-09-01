# Raspberry Pi jako síťový bridge pro LightBurn

Tento návod je určený pro Raspberry Pi OS Lite bez desktopu. Po dokončení bude Raspberry Pi poskytovat:

- laserový TCP → serial bridge na portu `23`;
- MJPEG stream USB kamery na portu `8080`;
- webové nastavení V4L2 parametrů kamery na portu `8081`.

V příkladech má Raspberry Pi adresu `192.168.0.99`. Nahraďte ji skutečnou adresou svého zařízení. Stejně tak vždy nejprve zjistěte vlastní stabilní cesty laseru a kamery; názvy zařízení uvedené v příkazech nejsou předpokládány.

## Co udělat

1. Připojit Raspberry Pi k LAN, ideálně Ethernetem, a přihlásit se přes SSH.
2. Aktualizovat Raspberry Pi OS Lite a nainstalovat `ser2net`, `ustreamer`, V4L2 nástroje, Flask a Gunicorn.
3. Připojit laser a kameru a zjistit jejich stabilní cesty v `/dev/serial/by-id/` a `/dev/v4l/by-id/`.
4. Vytvořit `laser-bridge.service`, který zpřístupní GRBL sériový port na TCP portu `23`.
5. Vytvořit `camera-stream.service`, který zpřístupní existující UVC kameru jako MJPEG stream na portu `8080`.
6. Zkopírovat adresář `camera` do `/opt/camera-controls`, doplnit zjištěnou cestu kamery a aktivovat `camera-controls.service` na portu `8081`.
7. Ověřit služby, naslouchající porty, HTTP endpointy a komunikaci s laserem.
8. V routeru je vhodné vytvořit DHCP rezervaci, například `192.168.0.99`. Porty `23`, `8080` a `8081` nepřesměrovávat do internetu.

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
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ser2net ustreamer v4l-utils usbutils curl netcat-openbsd
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

Nejdříve zazálohujte případnou distribuční konfiguraci a vypněte distribuční unit. Program `ser2net` bude spouštět vlastní služba, takže nesmí současně běžet druhá instance:

```bash
sudo cp -a /etc/ser2net.yaml /etc/ser2net.yaml.pre-lightburn
sudo systemctl disable --now ser2net.service
```

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
CONFIG=/etc/ser2net.yaml
LASER_DEVICE=auto
LASER_BAUD=115200
LASER_PORT=23
LISTEN_ADDRESS=0.0.0.0
WAIT_SECONDS=5

[ ! -r "$DEFAULTS" ] || . "$DEFAULTS"

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

tmp_config="$(mktemp /run/laser-ser2net.XXXXXX.yaml)"
trap 'rm -f "$tmp_config"' EXIT HUP INT TERM
{
    printf '%%YAML 1.1\n---\n'
    printf 'connection: &laser\n'
    printf '    accepter: tcp,%s,%s\n' "$LISTEN_ADDRESS" "$LASER_PORT"
    printf '    enable: on\n'
    printf '    options:\n'
    printf '      kickolduser: true\n'
    printf '    connector: serialdev,\n'
    printf '              %s,\n' "$device"
    printf '              %sn81,local\n' "$LASER_BAUD"
} > "$tmp_config"
install -o root -g root -m 0644 "$tmp_config" "$CONFIG"

exec /usr/sbin/ser2net -n -c "$CONFIG" -P /run/laser-bridge.pid
```

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

[Install]
WantedBy=multi-user.target
```

Aktivujte službu:

```bash
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

[Install]
WantedBy=multi-user.target
```

Aktivujte službu:

```bash
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

Vlastní aplikace omezuje přístup k ovládání na loopback a privátní LAN rozsahy přes `ALLOWED_NETWORKS`. Na routeru nevytvářejte port forwarding. Pokud na Pi používáte firewall, povolte z vlastní LAN pouze SSH a TCP porty `23`, `8080` a `8081`.

### 7. Ověření výsledku

Zkontrolujte automatický start a běh všech služeb:

```bash
systemctl is-enabled laser-bridge.service camera-stream.service camera-controls.service
systemctl is-active laser-bridge.service camera-stream.service camera-controls.service
sudo systemctl status laser-bridge.service
sudo systemctl status camera-stream.service
sudo systemctl status camera-controls.service
sudo ss -lntp | grep -E ':(23|8080|8081)[[:space:]]'
```

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

Nakonec Raspberry Pi restartujte a kontroly `is-enabled`, `is-active`, `ss` a HTTP testy zopakujte:

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
