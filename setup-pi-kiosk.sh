#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Bambu A1 Monitor — Raspberry Pi Kiosk Setup
#  Target: Raspberry Pi 3 B+ / Raspberry Pi OS Lite (64-bit)
#  Repo:   https://github.com/MoistLad/Local-Printer-Monitor
#
#  What this does:
#    • Installs X11, Openbox, xfce4-terminal
#    • Installs JetBrains Mono + Noto Emoji (monochrome) fonts
#    • Configures Everforest Dark colour scheme
#    • Sets up Python venv + dependencies
#    • Configures auto-boot into fullscreen monitor
#
#  Usage:
#    chmod +x setup-pi-kiosk.sh && ./setup-pi-kiosk.sh
#    sudo reboot
#    (Enter Bambu credentials on first boot)
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

REPO_URL="https://github.com/MoistLad/Local-Printer-Monitor.git"
APP_DIR="$HOME/app"
REPO_DIR="$APP_DIR/repo"
VENV_DIR="$APP_DIR/venv"
FONT_DIR="$HOME/.local/share/fonts"
JBM_VERSION="2.304"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1;34m'; NC='\033[0m'
info()  { echo -e "${B}[INFO]${NC}  $*"; }
ok()    { echo -e "${G}[ OK ]${NC}  $*"; }
warn()  { echo -e "${Y}[WARN]${NC}  $*"; }
fail()  { echo -e "${R}[FAIL]${NC}  $*"; exit 1; }

echo -e "\n${G}════════════════════════════════════════════════${NC}"
echo -e "${G}  Bambu A1 Monitor — Pi Kiosk Setup${NC}"
echo -e "${G}════════════════════════════════════════════════${NC}\n"

# ── 1. Fix any interrupted dpkg ────────────────────────────────
sudo dpkg --configure -a 2>/dev/null || true

# ── 2. System packages ─────────────────────────────────────────
info "Installing system packages..."
sudo apt update -qq
sudo apt install -y \
    xserver-xorg xinit openbox \
    xfce4-terminal \
    unclutter \
    python3-full python3-venv \
    git curl unzip fontconfig
ok "System packages installed"

# ── 3. JetBrains Mono font ─────────────────────────────────────
info "Installing JetBrains Mono..."
mkdir -p "$FONT_DIR/JetBrainsMono"

JBM_URL="https://github.com/JetBrains/JetBrainsMono/releases/download/v${JBM_VERSION}/JetBrainsMono-${JBM_VERSION}.zip"
curl -sL "$JBM_URL" -o /tmp/JBM.zip

if [ -s /tmp/JBM.zip ]; then
    unzip -o /tmp/JBM.zip -d /tmp/JBM 2>/dev/null
    cp /tmp/JBM/fonts/ttf/JetBrainsMono-*.ttf "$FONT_DIR/JetBrainsMono/" 2>/dev/null || \
    cp /tmp/JBM/ttf/*.ttf "$FONT_DIR/JetBrainsMono/" 2>/dev/null || true
    rm -rf /tmp/JBM /tmp/JBM.zip
    ok "JetBrains Mono installed"
else
    warn "JetBrains Mono download failed — check URL manually"
    warn "https://github.com/JetBrains/JetBrainsMono/releases"
fi

# ── 4. Noto Emoji (monochrome) font ────────────────────────────
info "Installing Noto Emoji (monochrome)..."
EMOJI_URL=$(curl -s "https://fonts.googleapis.com/css2?family=Noto+Emoji" \
    | sed -n 's/.*url(\([^)]*\)).*/\1/p' | head -1)

if [ -n "$EMOJI_URL" ]; then
    curl -sL "$EMOJI_URL" -o "$FONT_DIR/NotoEmoji-Regular.ttf"
    if [ -s "$FONT_DIR/NotoEmoji-Regular.ttf" ]; then
        ok "Noto Emoji installed"
    else
        warn "Noto Emoji download produced an empty file"
    fi
else
    warn "Could not resolve Noto Emoji URL from Google Fonts API"
fi

# ── 5. Rebuild font cache ──────────────────────────────────────
fc-cache -f 2>/dev/null
ok "Font cache rebuilt"

# Verify
fc-list | grep -q "JetBrains Mono" && ok "JetBrains Mono verified" || warn "JetBrains Mono NOT found in fc-list"
fc-list | grep -q "Noto Emoji"     && ok "Noto Emoji verified"     || warn "Noto Emoji NOT found in fc-list"

# ── 6. Fontconfig — prefer monochrome emoji ─────────────────────
info "Configuring fontconfig..."
mkdir -p "$HOME/.config/fontconfig/conf.d"
cat > "$HOME/.config/fontconfig/conf.d/99-emoji.conf" << 'FCEOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <alias>
    <family>emoji</family>
    <prefer><family>Noto Emoji</family></prefer>
  </alias>
  <selectfont>
    <rejectfont>
      <pattern>
        <patelt name="family"><string>Noto Color Emoji</string></patelt>
      </pattern>
    </rejectfont>
  </selectfont>
</fontconfig>
FCEOF
ok "Fontconfig: monochrome Noto Emoji preferred, colour emoji blocked"

# ── 7. xfce4-terminal config (Everforest Dark) ──────────────────
info "Configuring terminal (Everforest Dark)..."
mkdir -p "$HOME/.config/xfce4/terminal"
cat > "$HOME/.config/xfce4/terminal/terminalrc" << 'TEOF'
[Configuration]
FontName=JetBrains Mono 14
MiscAlwaysShowTabs=FALSE
MiscBell=FALSE
MiscBellUrgent=FALSE
MiscBordersDefault=FALSE
MiscCursorBlinks=FALSE
MiscCursorShape=TERMINAL_CURSOR_SHAPE_BLOCK
MiscDefaultGeometry=120x40
MiscInheritGeometry=FALSE
MiscMenubarDefault=FALSE
MiscMouseAutohide=TRUE
MiscToolbarDefault=FALSE
MiscConfirmClose=FALSE
MiscShowRelaunchDialog=FALSE
MiscRewrapOnResize=TRUE
ScrollingBar=TERMINAL_SCROLLBAR_NONE
ScrollingUnlimited=TRUE
ColorForeground=#d3c6aa
ColorBackground=#2d353b
ColorCursorForeground=#2d353b
ColorCursorUseDefault=FALSE
ColorCursor=#d3c6aa
ColorPalette=#475258;#e67e80;#a7c080;#dbbc7f;#7fbbb3;#d699b6;#83c092;#d3c6aa;#475258;#e67e80;#a7c080;#dbbc7f;#7fbbb3;#d699b6;#83c092;#d3c6aa
ColorBoldUseDefault=FALSE
ColorBoldIsBright=TRUE
TEOF
ok "Terminal configured"

# ── 8. Clone repo + Python venv ──────────────────────────────────
info "Setting up application..."
mkdir -p "$APP_DIR"

if [ -d "$REPO_DIR/.git" ]; then
    cd "$REPO_DIR" && git pull --ff-only 2>/dev/null || true
    ok "Repository updated"
else
    git clone "$REPO_URL" "$REPO_DIR"
    ok "Repository cloned"
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" -q
ok "Python venv + dependencies installed"

# Verify imports
"$VENV_DIR/bin/python3" -c "import paho.mqtt.client; import rich; from bambulab import BambuAuthenticator; print('All imports OK')" \
    && ok "Python imports verified" \
    || fail "Python import check failed"

# ── 9. Run script ────────────────────────────────────────────────
info "Creating run script..."
cat > "$APP_DIR/run.sh" << 'REOF'
#!/bin/bash
APP_DIR="$HOME/app"
REPO_DIR="$APP_DIR/repo"
VENV_DIR="$APP_DIR/venv"

export COLORTERM=truecolor
export TERM=xterm-256color

cd "$REPO_DIR"
git pull --ff-only 2>/dev/null || true
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt" 2>/dev/null || true

while true; do
    cd "$REPO_DIR"
    "$VENV_DIR/bin/python3" bambu_monitor.py || true
    echo ""
    echo "⚠️  Monitor exited. Restarting in 5 seconds..."
    sleep 5
done
REOF
chmod +x "$APP_DIR/run.sh"
ok "Run script created"

# ── 10. Auto-login ────────────────────────────────────────────────
info "Configuring auto-login on tty1..."
sudo raspi-config nonint do_boot_behaviour B2 2>/dev/null || {
    sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
    printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin %s --noclear %%I $TERM\n' "$USER" \
        | sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf > /dev/null
    sudo systemctl daemon-reload
}
ok "Auto-login configured"

# ── 11. .bash_profile → startx on tty1 ───────────────────────────
if ! grep -q "startx" "$HOME/.bash_profile" 2>/dev/null; then
    cat >> "$HOME/.bash_profile" << 'BPEOF'

# Auto-start X11 on tty1
if [[ -z "${DISPLAY:-}" ]] && [[ "$(tty)" == "/dev/tty1" ]]; then
    exec startx -- -nocursor 2>/dev/null
fi
BPEOF
fi
ok ".bash_profile configured"

# ── 12. .xinitrc ──────────────────────────────────────────────────
cat > "$HOME/.xinitrc" << 'XEOF'
#!/bin/bash
xset s off
xset s noblank
xset -dpms
exec openbox-session
XEOF
chmod +x "$HOME/.xinitrc"
ok ".xinitrc configured"

# ── 13. Openbox autostart ─────────────────────────────────────────
mkdir -p "$HOME/.config/openbox"
cat > "$HOME/.config/openbox/autostart" << OBEOF
unclutter -idle 0.5 -root &
sleep 1
xfce4-terminal --fullscreen --command="$HOME/app/run.sh" &
OBEOF
ok "Openbox autostart configured"

# ── 14. Disable all screen sleep/blanking ──────────────────────────
info "Disabling screen blanking..."

# Kernel console blanking
CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    if ! grep -q "consoleblank=0" "$CMDLINE"; then
        sudo sed -i 's/$/ consoleblank=0/' "$CMDLINE"
        ok "Kernel console blanking disabled (cmdline.txt)"
    else
        ok "Kernel console blanking already disabled"
    fi
else
    warn "$CMDLINE not found — add 'consoleblank=0' to your boot cmdline manually"
fi

# HDMI blanking
CONFIG="/boot/firmware/config.txt"
if [ -f "$CONFIG" ]; then
    if ! grep -q "hdmi_blanking=0" "$CONFIG"; then
        echo -e "\n# Keep HDMI signal alive\nhdmi_blanking=0" | sudo tee -a "$CONFIG" > /dev/null
        ok "HDMI blanking disabled (config.txt)"
    else
        ok "HDMI blanking already disabled"
    fi
fi

ok "Screen will stay on permanently"

# ── Done ──────────────────────────────────────────────────────────
echo ""
echo -e "${G}════════════════════════════════════════════════${NC}"
echo -e "${G}  ✅  Setup complete!${NC}"
echo -e "${G}════════════════════════════════════════════════${NC}"
echo ""
echo "  Next steps:"
echo "  ─────────────────────────────────────────────"
echo "  1.  sudo reboot"
echo "  2.  On first boot enter your Bambu Lab credentials"
echo "      (email, password, printer serial, region)"
echo "  3.  If asked for a verification code, check your email"
echo "  4.  Every boot after that is fully automatic"
echo ""
echo -e "  ${Y}⚠  IMPORTANT: Close any other Bambu monitor${NC}"
echo -e "  ${Y}   instances (Windows, phone, etc.) BEFORE${NC}"
echo -e "  ${Y}   testing. Only one can connect at a time.${NC}"
echo ""
echo "  Useful commands:"
echo "    Test now (SSH):        cd ~/app/repo && ~/app/venv/bin/python3 bambu_monitor.py"
echo "    Re-enter credentials:  rm ~/app/repo/credentials.json"
echo "    Change font size:      nano ~/.config/xfce4/terminal/terminalrc"
echo ""