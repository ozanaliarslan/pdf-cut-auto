#!/bin/bash
set -e

# Scriptin bulunduğu klasöre geçiş yap
cd "$(dirname "$0")"

echo "==========================================="
echo "PDF Cut Auto - Toplu Derleme ve Dağıtım"
echo "==========================================="

mkdir -p build_logs

# 1. Windows Paketi Derleme
echo -e "\n[1/4] Windows Paketi Derleniyor..."
if .venv/bin/python build_portable.py windows-x64 2>&1 | tee build_logs/build_windows.log; then
    echo "✓ Windows derlemesi başarılı."
else
    echo "✗ Windows derlemesi başarısız oldu! Günlük dosyasını kontrol edin: build_logs/build_windows.log"
    exit 1
fi

# 2. macOS Paketi Derleme
echo -e "\n[2/4] macOS Paketi Derleniyor..."
if .venv/bin/python build_desktop.py 2>&1 | tee build_logs/build_macos.log; then
    echo "✓ macOS derlemesi başarılı."
else
    echo "✗ macOS derlemesi başarısız oldu! Günlük dosyasını kontrol edin: build_logs/build_macos.log"
    exit 1
fi

# 3. Linux Paketi Derleme
echo -e "\n[3/4] Linux Paketi Derleniyor..."
if .venv/bin/python build_portable.py linux-x64 2>&1 | tee build_logs/build_linux.log; then
    echo "✓ Linux derlemesi başarılı."
else
    echo "✗ Linux derlemesi başarısız oldu! Günlük dosyasını kontrol edin: build_logs/build_linux.log"
    exit 1
fi

# 4. Sunucuya Gönderme (Deploy)
echo -e "\n[4/4] Sunucuya Gönderiliyor (Deploy)..."
if bash deploy/deploy_vps.sh 2>&1 | tee build_logs/deploy.log; then
    echo "✓ Sunucu dağıtımı başarıyla tamamlandı!"
else
    echo "✗ Sunucu dağıtımı başarısız oldu! Günlük dosyasını kontrol edin: build_logs/deploy.log"
    exit 1
fi

echo -e "\n==========================================="
echo "TEBRİKLER! Tüm derlemeler ve dağıtım başarılı."
echo "==========================================="
