# NYC Subway Dashboard

This self-hosted dashboard shows arrival times for the NYC subway, LIRR, and PATH trains of your choosing.

![Dashboard photo](https://raw.githubusercontent.com/artnc/nyc-subway-dashboard/master/photo.jpg)

## Setup

1. Clone this repo onto a machine that has Docker
1. Customize this repo's `config.yaml` file to your liking
1. Run `docker compose up -d --build` in the repo directory

View the dashboard at http://localhost:10001

## Advanced

<details>
<summary>Create a wall display with Raspberry Pi</summary>
<br>

On a Raspberry Pi (or similar 16-bit framebuffer Linux device) that has cron and cURL but no desktop running:

1. Run `sudo crontab -e`, add these lines, and save. Replace `<server>` with the hostname or IP address of the machine that's running the dashboard, which may be the Raspberry Pi itself (i.e. `localhost`) if it runs a 64-bit OS, or another machine to which the Raspberry Pi has network access.
   ```
   # https://github.com/artnc/nyc-subway-dashboard
   @reboot TERM=linux setterm --blank 0 --powerdown 0 --cursor off > /dev/tty1 < /dev/tty1
   * * * * * [ "$(curl --compressed -so /dev/fb0 -w '\%{http_code}' "http://<server>:10001/frame?size=$(cat /sys/class/graphics/fb0/virtual_size)")" = 200 ]; echo $((4 * $?)) > /sys/class/graphics/fb0/blank
   ```
1. Connect a computer monitor or TV and reboot.

</details>

<details>
<summary>Show this as your xsecurelock screensaver</summary>
<br>

On a Linux desktop with Python 3 and [xsecurelock](https://github.com/google/xsecurelock):

1. Set these environment variables where you start xsecurelock, e.g. in `~/.xinitrc` before you start `xss-lock` and your window manager.
   ```
   # https://github.com/artnc/nyc-subway-dashboard
   export XSECURELOCK_BLANK_TIMEOUT=-1
   export XSECURELOCK_SAVER=/path/to/nyc-subway-dashboard/screensaver
   export XSECURELOCK_SAVER_STOP_ON_BLANK=0
   ```
1. Log out and log back in.

</details>
